import AppKit
@preconcurrency import AVFoundation
import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum State: Equatable {
        case idle
        case recording(AudioClient)
        case starting
        case stopping
        case permissionRequired
        case checkingPermissions
        case error(String)
    }

    @Published private(set) var state: State = .checkingPermissions
    @Published private(set) var permissionMessage = ""
    @Published private(set) var startupReport: StartupCaptureReport?
    @Published private(set) var microphoneAccess = false
    @Published private(set) var systemAudioAccess: Bool?
    @Published private(set) var documentsAccess: Bool?
    @Published private(set) var accessibilityAccess: Bool?
    private var refreshingPermissions = false
    private let checkCapture: @MainActor () async throws -> StartupCaptureReport
    private var permissionCheckTask: Task<Void, Never>?
    @Published var lastManifest: URL?
    @Published var recoverableFiles: [URL] = []
    @Published private(set) var recoveryInProgress = false
    @Published private(set) var recoveryError: String?
    @Published private(set) var finalizationsInProgress = 0
    private let capture = CaptureCoordinator()
    @Published private(set) var captureStatus: CaptureStatus?
    private var activeCaptureID: UUID?
    private var backgroundTasks: [UUID: Task<Void, Never>] = [:]
    lazy var keepRunning = KeepRunningService()

    private let monitor: AudioActivityMonitor?
    private var stopTask: Task<Void, Never>?
    private var ignoredUntil: [String: Date] = [:]
    private var started = false
    private var recordingMethod: TriggerMethod = .audioProcess
    private var activeMicrophoneClients = Set<pid_t>()
    private var suppressedClients = Set<pid_t>()
    private var captureFailure: String?
    private var retryTask: Task<Void, Never>?

    init(checkCapture: @escaping @MainActor () async throws -> StartupCaptureReport = StartupCaptureCheck.run,
         monitor: AudioActivityMonitor? = AudioActivityMonitor()) {
        self.checkCapture = checkCapture
        self.monitor = monitor
        capture.onStatus = { [weak self] status in
            Task { @MainActor in
                guard let self, self.activeCaptureID == status.sessionID else { return }
                self.captureStatus = status
            }
        }
        capture.onFailure = { [weak self] id, message in
            Task { @MainActor in
                guard let self, self.activeCaptureID == id else { return }
                self.captureFailure = message
                if case .recording = self.state { self.stopRecording(suppressClients: false) }
            }
        }
    }

    var statusIcon: String {
        switch state {
        case .recording: "record.circle.fill"
        case .permissionRequired, .error: "exclamationmark.triangle.fill"
        default: "waveform"
        }
    }

    func start() {
        guard !started else { return }
        started = true
        Task { recoverableFiles = await capture.recoverableFiles() }
        checkPermissions()
    }

    func checkPermissions() {
        guard permissionCheckTask == nil else { return }
        guard Self.allowsPermissionCheck(in: state) else { return }
        retryTask?.cancel(); retryTask = nil
        monitor?.stop()
        startupReport = nil
        state = .checkingPermissions
        permissionMessage = "Checking microphone and system-audio recording…"
        CaptureDiagnostics.log("check.start")
        permissionCheckTask = Task {
            do {
                startupReport = try await checkCapture()
                CaptureDiagnostics.log("check.finished systemAudioObserved=\(startupReport?.systemAudioObserved == true)")
                systemAudioAccess = true
                startMonitoring()
            } catch {
                CaptureDiagnostics.error("check.failed \(CaptureError.details(error))")
                if case CaptureError.systemAudioCaptureFailure = error { systemAudioAccess = false }
                permissionMessage = error is CaptureError ? error.localizedDescription : CaptureError.details(error)
                state = .permissionRequired
            }
            permissionCheckTask = nil
        }
    }

    func manualStart() {
        let client = AudioClient(audioObjectID: 0, processID: 0, bundleID: nil, applicationName: "Manual recording", inputDevices: [])
        beginRecording(client, method: .manual)
    }

    func ignoreForHour() {
        guard case let .recording(client) = state, client.processID > 0 else { return }
        ignoredUntil[key(client)] = Date().addingTimeInterval(3600)
        stopRecording()
    }

    func neverRecordApplication() {
        guard case let .recording(client) = state, client.processID > 0 else { return }
        var values = Set(UserDefaults.standard.stringArray(forKey: "excludedBundleIDs") ?? [])
        values.insert(key(client))
        UserDefaults.standard.set(Array(values).sorted(), forKey: "excludedBundleIDs")
        stopRecording()
    }

    func stopRecording(suppressClients: Bool = true) {
        guard case let .recording(client) = state else { return }
        if suppressClients {
            recordingMethod = .audioProcess // Explicit Stop ends manual retry intent.
            if client.processID == 0 { suppressedClients.formUnion(activeMicrophoneClients) }
            else { suppressedClients.insert(client.processID) }
        }
        stopTask?.cancel(); stopTask = nil
        state = .stopping
        Task {
            do {
                let request = try await capture.stop()
                activeCaptureID = nil
                captureStatus = nil
                if let captureFailure { retryAfterFailure(captureFailure) }
                else { state = .idle }
                finalizeInBackground(request)
            }
            catch {
                activeCaptureID = nil
                captureStatus = nil
                recoverableFiles = await capture.recoverableFiles()
                retryAfterFailure(error.localizedDescription)
            }
        }
    }

    func dismissError() {
        guard case .error = state else { return }
        retryTask?.cancel(); retryTask = nil
        if startupReport == nil { checkPermissions() }
        else {
            state = .idle
            if recordingMethod == .manual { manualStart() }
        }
    }

    private func retryAfterFailure(_ message: String) {
        CaptureDiagnostics.error("capture.retry delaySeconds=10 error=\(message)")
        state = .error(message)
        retryTask?.cancel()
        retryTask = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(10)) }
            catch { return }
            self?.dismissError()
        }
    }

    func refreshPermissionStatus() async {
        guard !refreshingPermissions else { return }
        refreshingPermissions = true
        defer { refreshingPermissions = false }
        microphoneAccess = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        let appDocuments = await Task.detached {
            do { try MeetingStore().validateAccess(); return true }
            catch { return false }
        }.value
        do {
            let worker = try BackgroundWorkerClient()
            let report = try await worker.checkPermissions(root: MeetingStore().root)
            documentsAccess = appDocuments && report.documents
            accessibilityAccess = report.accessibility
        } catch {
            documentsAccess = appDocuments ? nil : false
            accessibilityAccess = nil
        }
    }

    func openPermissionSettings(_ pane: String) {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") else { return }
        NSWorkspace.shared.open(url)
    }

    func copyRecordingCheckError() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(permissionMessage, forType: .string)
    }

    func recoverInterruptedRecording(_ interruptedFile: URL) {
        guard Self.allowsMaintenance(in: state), !recoveryInProgress,
              recoverableFiles.contains(interruptedFile) else { return }
        recoveryInProgress = true
        recoveryError = nil
        let id = UUID()
        backgroundTasks[id] = Task {
            defer { backgroundTasks[id] = nil }
            do {
                lastManifest = try await capture.recoverInterruptedRecording(interruptedFile)
                recoverableFiles = await capture.recoverableFiles()
            } catch {
                recoverableFiles = await capture.recoverableFiles()
                recoveryError = error is CancellationError ? "Background processing cancelled. Saved audio remains available." : error.localizedDescription
            }
            recoveryInProgress = false
        }
    }

    var showsMaintenance: Bool { Self.allowsMaintenance(in: state) }

    var allowsTermination: Bool {
        switch state {
        case .idle, .permissionRequired, .checkingPermissions, .error: true
        case .recording, .starting, .stopping: false
        }
    }

    static func allowsMaintenance(in state: State) -> Bool {
        switch state {
        case .idle, .permissionRequired, .error: true
        default: false
        }
    }

    static func allowsPermissionCheck(in state: State) -> Bool {
        switch state {
        case .idle, .permissionRequired, .checkingPermissions, .error: true
        default: false
        }
    }

    private func finalizeInBackground(_ request: RecordingSession) {
        finalizationsInProgress += 1
        let id = UUID()
        backgroundTasks[id] = Task {
            defer { backgroundTasks[id] = nil }
            let session = request.paths.session.lastPathComponent
            CaptureDiagnostics.log("finalize.start session=\(session)")
            do {
                lastManifest = try await capture.finalize(request)
                CaptureDiagnostics.log("finalize.finished session=\(session)")
            } catch {
                CaptureDiagnostics.error("finalize.failed session=\(session) \(CaptureError.details(error))")
                recoveryError = error is CancellationError ? "Background processing cancelled. Saved audio remains available." : error.localizedDescription
            }
            finalizationsInProgress -= 1
            recoverableFiles = await capture.recoverableFiles()
        }
    }

    func cancelBackgroundProcessing() {
        for task in backgroundTasks.values { task.cancel() }
    }

    func revealLastRecording() {
        if let lastManifest { NSWorkspace.shared.activateFileViewerSelecting([lastManifest]) }
    }

    func revealInterruptedRecording(_ file: URL) {
        let paths = MeetingStore().paths(forInterruptedFile: file)
        let files = (try? FileManager.default.contentsOfDirectory(at: paths.directory, includingPropertiesForKeys: nil)) ?? []
        let related = files.filter {
            $0.lastPathComponent.hasPrefix(".\(paths.baseName)-") || $0 == paths.session
        }
        NSWorkspace.shared.activateFileViewerSelecting(related.isEmpty ? [file] : related)
    }

    func handle(_ clients: [AudioClient]) {
        activeMicrophoneClients = Set(clients.map(\.processID))
        suppressedClients.formIntersection(activeMicrophoneClients)
        switch state {
        case .idle:
            guard let candidate = clients.first(where: { !isIgnored($0) }) else { return }
            beginRecording(candidate)
        case let .recording(candidate):
            if let current = clients.first(where: { $0.processID == candidate.processID }) {
                stopTask?.cancel(); stopTask = nil
                capture.updateMicrophoneDevices(current.inputDevices)
                if current != candidate { state = .recording(current) }
            } else if Self.shouldAutoStop(method: recordingMethod), stopTask == nil {
                stopTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(15))
                    guard !Task.isCancelled else { return }
                    self?.stopRecording()
                    self?.stopTask = nil
                }
            }
        default: break
        }
    }

    private func beginRecording(_ client: AudioClient, method: TriggerMethod = .audioProcess) {
        guard startupReport != nil else { return }
        guard case .idle = state else { return }
        recordingMethod = method
        state = .starting
        captureFailure = nil
        let id = UUID()
        activeCaptureID = id
        captureStatus = nil
        let trigger = CaptureTrigger(method: method, processID: client.processID > 0 ? client.processID : nil, bundleID: client.bundleID, applicationName: client.applicationName)
        Task {
            do {
                try await capture.start(id: id, trigger: trigger, microphoneDevice: client.primaryInputDevice)
                state = .recording(client)
                if captureFailure != nil { stopRecording(suppressClients: false) }
            }
            catch {
                activeCaptureID = nil
                captureStatus = nil
                recoverableFiles = await capture.recoverableFiles()
                if let captureError = error as? CaptureError {
                    switch captureError {
                    case .microphonePermissionRequired:
                        startupReport = nil
                        monitor?.stop()
                        permissionMessage = error.localizedDescription
                        state = .permissionRequired
                        return
                    default: break
                    }
                }
                retryAfterFailure(error.localizedDescription)
            }
        }
    }

    static func shouldAutoStop(method: TriggerMethod) -> Bool { method != .manual }

    private func startMonitoring() {
        monitor?.onClientsChanged = { [weak self] clients in self?.handle(clients) }
        state = .idle
        monitor?.start()
    }

    private func key(_ client: AudioClient) -> String { client.bundleID ?? "name:\(client.applicationName)" }

    private func isIgnored(_ client: AudioClient) -> Bool {
        if suppressedClients.contains(client.processID) { return true }
        let value = key(client)
        if ignoredUntil[value, default: .distantPast] > Date() { return true }
        return Set(UserDefaults.standard.stringArray(forKey: "excludedBundleIDs") ?? []).contains(value)
    }
}
