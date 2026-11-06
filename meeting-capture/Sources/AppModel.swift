import AppKit
@preconcurrency import AVFoundation
import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum State: Equatable {
        case idle
        case detecting(AudioClient, since: Date)
        case countdown(AudioClient, remaining: Int)
        case recording(AudioClient)
        case finalizing
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
    let capture = CaptureCoordinator()

    private let monitor: AudioActivityMonitor?
    private var countdownTask: Task<Void, Never>?
    private var stopTask: Task<Void, Never>?
    private var ignoredUntil: [String: Date] = [:]
    private var started = false
    private var recordingMethod: TriggerMethod = .audioProcess
    private var activeMicrophoneClients = Set<pid_t>()
    private var suppressedClients = Set<pid_t>()
    private var captureFailure: String?

    init(checkCapture: @escaping @MainActor () async throws -> StartupCaptureReport = StartupCaptureCheck.run,
         monitor: AudioActivityMonitor? = AudioActivityMonitor()) {
        self.checkCapture = checkCapture
        self.monitor = monitor
        capture.onFailure = { [weak self] message in
            guard let self else { return }
            self.captureFailure = message
            if case .recording = self.state { self.stopRecording() }
        }
    }

    var statusIcon: String {
        switch state {
        case .recording: "record.circle.fill"
        case .countdown, .detecting: "mic.badge.plus"
        case .permissionRequired, .error: "exclamationmark.triangle.fill"
        default: "waveform"
        }
    }

    func start() {
        guard !started else { return }
        started = true
        recoverableFiles = capture.recoverableFiles()
        checkPermissions()
    }

    func checkPermissions() {
        guard permissionCheckTask == nil else { return }
        guard Self.allowsPermissionCheck(in: state) else { return }
        monitor?.stop()
        startupReport = nil
        state = .checkingPermissions
        permissionMessage = "Checking microphone and system-audio recording…"
        permissionCheckTask = Task {
            do {
                startupReport = try await checkCapture()
                systemAudioAccess = true
                startMonitoring()
            } catch {
                if case CaptureError.systemAudioCaptureFailure = error { systemAudioAccess = false }
                permissionMessage = error is CaptureError ? error.localizedDescription : CaptureError.details(error)
                state = .permissionRequired
            }
            permissionCheckTask = nil
        }
    }

    func startNow() { if case let .countdown(client, _) = state { beginRecording(client) } }

    func manualStart() {
        let client = AudioClient(audioObjectID: 0, processID: 0, bundleID: nil, applicationName: "Manual recording", inputDevices: [])
        beginRecording(client, method: .manual)
    }

    func skip() {
        guard case let .countdown(client, _) = state else { return }
        suppressedClients.insert(client.processID)
        countdownTask?.cancel()
        countdownTask = nil
        state = .idle
    }

    func ignoreForHour() {
        guard case let .countdown(client, _) = state else { return }
        ignoredUntil[key(client)] = Date().addingTimeInterval(3600)
        skip()
    }

    func neverRecordApplication() {
        guard case let .countdown(client, _) = state else { return }
        var values = Set(UserDefaults.standard.stringArray(forKey: "excludedBundleIDs") ?? [])
        values.insert(key(client))
        UserDefaults.standard.set(Array(values).sorted(), forKey: "excludedBundleIDs")
        skip()
    }

    func stopRecording() {
        guard case let .recording(client) = state else { return }
        if client.processID == 0 { suppressedClients.formUnion(activeMicrophoneClients) }
        else { suppressedClients.insert(client.processID) }
        stopTask?.cancel(); stopTask = nil
        state = .finalizing
        Task {
            do {
                let request = try await capture.stop()
                state = captureFailure.map(State.error) ?? .idle
                finalizeInBackground(request)
            }
            catch {
                recoverableFiles = capture.recoverableFiles()
                state = .error(error.localizedDescription)
            }
        }
    }

    func dismissError() {
        if startupReport == nil { checkPermissions() } else { state = .idle }
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
        Task {
            do {
                lastManifest = try await capture.recoverInterruptedRecording(interruptedFile)
                recoverableFiles = capture.recoverableFiles()
            } catch {
                recoverableFiles = capture.recoverableFiles()
                recoveryError = error.localizedDescription
            }
            recoveryInProgress = false
        }
    }

    var showsMaintenance: Bool { Self.allowsMaintenance(in: state) }

    var allowsTermination: Bool {
        switch state {
        case .idle, .permissionRequired, .checkingPermissions, .error: true
        case .detecting, .countdown, .recording, .finalizing: false
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

    private func finalizeInBackground(_ request: FinalizeCaptureRequest) {
        finalizationsInProgress += 1
        Task {
            do {
                lastManifest = try await capture.finalize(request)
            } catch {
                recoveryError = error.localizedDescription
            }
            finalizationsInProgress -= 1
            recoverableFiles = capture.recoverableFiles()
        }
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

    func handle(_ clients: [AudioClient], now: Date = Date()) {
        activeMicrophoneClients = Set(clients.map(\.processID))
        suppressedClients.formIntersection(activeMicrophoneClients)
        switch state {
        case .idle:
            guard let candidate = clients.first(where: { !isIgnored($0) }) else { return }
            state = .detecting(candidate, since: now)
        case let .detecting(candidate, since):
            guard let current = clients.first(where: { $0.processID == candidate.processID }) else { state = .idle; return }
            if now.timeIntervalSince(since) >= 2 { beginCountdown(current) }
            else if current != candidate { state = .detecting(current, since: since) }
        case let .countdown(candidate, remaining):
            guard let current = clients.first(where: { $0.processID == candidate.processID }) else { state = .idle; return }
            if current != candidate { state = .countdown(current, remaining: remaining) }
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

    private func beginCountdown(_ client: AudioClient) {
        state = .countdown(client, remaining: 10)
        countdownTask?.cancel()
        countdownTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled, let self,
                      case let .countdown(client, remaining) = self.state else { return }
                if remaining <= 1 {
                    self.beginRecording(client)
                    return
                }
                self.state = .countdown(client, remaining: remaining - 1)
            }
        }
    }

    private func beginRecording(_ client: AudioClient, method: TriggerMethod = .audioProcess) {
        guard startupReport != nil else { return }
        switch state {
        case .idle, .detecting, .countdown: break
        default: return
        }
        countdownTask?.cancel(); countdownTask = nil
        stopTask?.cancel(); stopTask = nil
        recordingMethod = method
        state = .finalizing
        captureFailure = nil
        let trigger = CaptureTrigger(method: method, processID: client.processID > 0 ? client.processID : nil, bundleID: client.bundleID, applicationName: client.applicationName)
        Task {
            do {
                try await capture.start(trigger: trigger, microphoneDevice: client.primaryInputDevice)
                state = .recording(client)
                if captureFailure != nil { stopRecording() }
            }
            catch {
                recoverableFiles = capture.recoverableFiles()
                if let captureError = error as? CaptureError {
                    switch captureError {
                    case .systemAudioCaptureFailure, .microphonePermissionRequired:
                        if case .systemAudioCaptureFailure = captureError { systemAudioAccess = false }
                        startupReport = nil
                        monitor?.stop()
                        permissionMessage = error.localizedDescription
                        state = .permissionRequired
                        return
                    default: break
                    }
                }
                state = .error(error.localizedDescription)
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
