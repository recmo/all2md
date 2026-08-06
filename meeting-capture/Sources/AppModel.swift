import AppKit
import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum State: Equatable {
        case idle
        case detecting(AudioClient, since: Date)
        case countdown(AudioClient, remaining: Int)
        case recording(AudioClient)
        case finalizing
        case error(String)
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var screenRecordingPermissionNeeded = false
    @Published var lastManifest: URL?
    @Published var recoverableFiles: [URL] = []
    let capture = CaptureCoordinator()

    private let monitor = AudioActivityMonitor()
    private var countdownTask: Task<Void, Never>?
    private var stopTask: Task<Void, Never>?
    private var ignoredUntil: [String: Date] = [:]
    private var started = false

    var statusIcon: String {
        switch state {
        case .recording: "record.circle.fill"
        case .countdown, .detecting: "mic.badge.plus"
        case .error: "exclamationmark.triangle.fill"
        default: "waveform"
        }
    }

    func start() {
        guard !started else { return }
        started = true
        recoverableFiles = capture.recoverableFiles()
        monitor.onClientsChanged = { [weak self] clients in self?.handle(clients) }
        monitor.start()
    }

    func startNow() { if case let .countdown(client, _) = state { beginRecording(client) } }

    func manualStart() {
        let client = AudioClient(audioObjectID: 0, processID: 0, bundleID: nil, applicationName: "Manual recording")
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
        state = .finalizing
        Task {
            do { lastManifest = try await capture.stop(); recoverableFiles = capture.recoverableFiles(); state = .idle }
            catch { state = .error(error.localizedDescription) }
        }
    }

    func dismissError() { state = .idle }

    func openScreenRecordingSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") else { return }
        NSWorkspace.shared.open(url)
    }

    func recoverInterruptedRecordings() {
        state = .finalizing
        Task {
            do {
                lastManifest = try capture.recoverInterruptedRecordings()
                recoverableFiles = capture.recoverableFiles()
                state = .idle
            } catch {
                state = .error(error.localizedDescription)
            }
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
            guard clients.contains(where: { $0.processID == candidate.processID }) else { state = .idle; return }
            if Date().timeIntervalSince(since) >= 2 { beginCountdown(candidate) }
        case let .recording(candidate):
            if clients.contains(where: { $0.processID == candidate.processID }) {
                stopTask?.cancel(); stopTask = nil
            } else if stopTask == nil {
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
        countdownTask?.cancel(); countdownTask = nil
        state = .finalizing
        let resolvedMethod: TriggerMethod = method == .manual ? .manual : (client.processID > 0 ? .audioProcess : .deviceRunning)
        let trigger = CaptureTrigger(method: resolvedMethod, processID: client.processID > 0 ? client.processID : nil, bundleID: client.bundleID, applicationName: client.applicationName)
        screenRecordingPermissionNeeded = false
        Task {
            do { try await capture.start(trigger: trigger); state = .recording(client) }
            catch {
                if let captureError = error as? CaptureError,
                   case .screenRecordingPermissionRequired = captureError {
                    screenRecordingPermissionNeeded = true
                }
                state = .error(error.localizedDescription)
            }
        }
    }

    private func key(_ client: AudioClient) -> String { client.bundleID ?? "name:\(client.applicationName)" }

    private func isIgnored(_ client: AudioClient) -> Bool {
        let value = key(client)
        if ignoredUntil[value, default: .distantPast] > Date() { return true }
        return Set(UserDefaults.standard.stringArray(forKey: "excludedBundleIDs") ?? []).contains(value)
    }
}
