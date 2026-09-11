import AppKit
import CoreGraphics
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
    private let checkCapture: @MainActor () async throws -> StartupCaptureReport
    private var permissionCheckTask: Task<Void, Never>?
    @Published var lastManifest: URL?
    @Published var recoverableFiles: [URL] = []
    @Published private(set) var recoveryInProgress = false
    @Published private(set) var recoveryError: String?
    @Published private(set) var finalizationsInProgress = 0
    @Published private(set) var activeOutputClients: [AudioClient] = []
    let capture = CaptureCoordinator()

    private let monitor = AudioActivityMonitor()
    private var countdownTask: Task<Void, Never>?
    private var stopTask: Task<Void, Never>?
    private var ignoredUntil: [String: Date] = [:]
    private var started = false
    private var recordingMethod: TriggerMethod = .audioProcess

    init(checkCapture: @escaping @MainActor () async throws -> StartupCaptureReport = StartupCaptureCheck.run) {
        self.checkCapture = checkCapture
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
        monitor.stop()
        startupReport = nil
        state = .checkingPermissions
        permissionMessage = "Checking microphone and system-audio recording…"
        permissionCheckTask = Task {
            do {
                startupReport = try await checkCapture()
                startMonitoring()
            } catch {
                permissionMessage = error.localizedDescription
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

    func manualStart(client: AudioClient) {
        beginRecording(client, method: .manual)
    }

    func skip() {
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
        guard case .recording = state else { return }
        stopTask?.cancel(); stopTask = nil
        state = .finalizing
        Task {
            do {
                let request = try await capture.stop()
                state = .idle
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

    func openMicrophoneSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") else { return }
        NSWorkspace.shared.open(url)
    }

    func openFilesSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders") else { return }
        NSWorkspace.shared.open(url)
    }

    func openAccessibilitySettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") else { return }
        NSWorkspace.shared.open(url)
    }

    func openScreenRecordingSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") else { return }
        NSWorkspace.shared.open(url)
    }

    func recoverInterruptedRecordings() {
        guard Self.allowsMaintenance(in: state), !recoveryInProgress,
              let interruptedFile = recoverableFiles.first else { return }
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
        if case .idle = state { return true }
        return false
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
                if let manifest = try await capture.finalize(request) { lastManifest = manifest }
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

    private func handle(_ clients: [AudioClient]) {
        switch state {
        case .idle:
            guard let candidate = clients.first(where: { !isIgnored($0) }) else { return }
            state = .detecting(candidate, since: Date())
        case let .detecting(candidate, since):
            guard let current = clients.first(where: { $0.processID == candidate.processID }) else { state = .idle; return }
            if Date().timeIntervalSince(since) >= 2 { beginCountdown(current) }
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
        let resolvedMethod: TriggerMethod = method == .manual ? .manual : (client.processID > 0 ? .audioProcess : .deviceRunning)
        let trigger = CaptureTrigger(method: resolvedMethod, processID: client.processID > 0 ? client.processID : nil, bundleID: client.bundleID, applicationName: client.applicationName)
        Task {
            do { try await capture.start(trigger: trigger, microphoneDevice: client.primaryInputDevice); state = .recording(client) }
            catch {
                if let captureError = error as? CaptureError {
                    switch captureError {
                    case .screenRecordingPermissionRequired, .microphonePermissionRequired:
                        startupReport = nil
                        monitor.stop()
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
        monitor.onClientsChanged = { [weak self] clients in self?.handle(clients) }
        monitor.onOutputClientsChanged = { [weak self] clients in
            self?.activeOutputClients = clients
        }
        state = .idle
        monitor.start()
    }

    private func key(_ client: AudioClient) -> String { client.bundleID ?? "name:\(client.applicationName)" }

    private func isIgnored(_ client: AudioClient) -> Bool {
        let value = key(client)
        if ignoredUntil[value, default: .distantPast] > Date() { return true }
        return Set(UserDefaults.standard.stringArray(forKey: "excludedBundleIDs") ?? []).contains(value)
    }
}
