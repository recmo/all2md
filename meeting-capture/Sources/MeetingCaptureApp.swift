import SwiftUI
import ServiceManagement

@main
struct MeetingCaptureApp: App {
    @NSApplicationDelegateAdaptor(CaptureAppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            StatusMenuView(model: delegate.model)
        } label: {
            CaptureStatusLabel(model: delegate.model)
        }
        .menuBarExtraStyle(.window)
    }
}

private struct CaptureStatusLabel: View {
    @ObservedObject var model: AppModel

    var body: some View { Label("Meeting Capture", systemImage: model.statusIcon) }
}

@MainActor
final class CaptureAppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard NSClassFromString("XCTestCase") == nil else { return }
        CaptureDiagnostics.log("app.start pid=\(ProcessInfo.processInfo.processIdentifier) version=\(Bundle.main.infoDictionary?["CFBundleVersion"] ?? "unknown") os=\(ProcessInfo.processInfo.operatingSystemVersionString)")
        model.keepRunning.start()
        model.start()
        HUDController.shared.attach(to: model)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        model.allowsTermination ? .terminateNow : .terminateCancel
    }
}

@MainActor
final class KeepRunningService: ObservableObject {
    private let service = SMAppService.agent(plistName: "ventures.wicked.MeetingCapture.keep-running.plist")
    @Published private(set) var status: SMAppService.Status = .notRegistered
    @Published private(set) var errorMessage: String?

    var isRegistered: Bool { status == .enabled || status == .requiresApproval }

    func start() {
        // Remember successful setup so System Settings disable survives relaunch.
        if UserDefaults.standard.bool(forKey: "keepRunningConfigured") { refresh() }
        else { setEnabled(true) }
    }

    func refresh() { status = service.status }

    func setEnabled(_ enabled: Bool) {
        errorMessage = nil
        do {
            refresh()
            if enabled {
                if !isRegistered { try service.register() }
            } else if isRegistered {
                try service.unregister()
            }
            UserDefaults.standard.set(true, forKey: "keepRunningConfigured")
        } catch {
            errorMessage = error.localizedDescription
        }
        refresh()
    }

    func openSettings() { SMAppService.openSystemSettingsLoginItems() }

    func disableAndQuit() {
        setEnabled(false)
        guard errorMessage == nil, !isRegistered else { return }
        NSApplication.shared.terminate(nil)
    }
}

private struct StatusMenuView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var keepRunning: KeepRunningService

    init(model: AppModel) {
        self.model = model
        keepRunning = model.keepRunning
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch model.state {
            case .idle:
                Label("Watching microphone activity", systemImage: "mic")
                Button("Start recording manually") { model.manualStart() }
                Text("Records the microphone and all system audio as two separate tracks.")
                    .font(.caption).foregroundStyle(.secondary)
            case let .recording(client):
                Label("Recording \(client.applicationName)", systemImage: "record.circle.fill").foregroundStyle(.red)
                if let startedAt = model.captureStatus?.startedAt { TimelineView(.periodic(from: .now, by: 1)) { _ in Text(startedAt, style: .timer).monospacedDigit() } }
                LevelRow(name: model.captureStatus?.microphoneName ?? client.inputDeviceSummary, level: model.captureStatus?.microphoneLevel ?? 0)
                LevelRow(name: "System audio", level: model.captureStatus?.systemAudioLevel ?? 0)
                if let warning = model.captureStatus?.warning {
                    Text(warning).font(.caption).foregroundStyle(.orange)
                }
                Button("Stop") { model.stopRecording() }.keyboardShortcut(.defaultAction)
                if client.processID > 0 {
                    Button("Ignore for one hour") { model.ignoreForHour() }
                    Button("Never record this application") { model.neverRecordApplication() }
                }
            case .starting:
                ProgressView("Starting recording…")
            case .stopping:
                ProgressView("Saving recorded audio…")
            case .checkingPermissions:
                ProgressView("Checking recording access…")
            case .permissionRequired:
                Label {
                    Text("Recording access required")
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                Text(model.permissionMessage)
                    .textSelection(.enabled)
                Text("Retry after addressing the error. If you changed macOS permissions, quit and reopen Meeting Capture first.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Retry") { model.checkPermissions() }
                Button("Copy error details") { model.copyRecordingCheckError() }
            case let .error(message):
                Label {
                    Text(message).textSelection(.enabled)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                Button("Retry") { model.checkPermissions() }
            }
            if model.finalizationsInProgress > 0 || model.recoveryInProgress {
                Button("Cancel background processing") { model.cancelBackgroundProcessing() }
            }
            if model.allowsTermination {
                if model.lastManifest != nil { Button("Reveal last recording") { model.revealLastRecording() } }
                if model.finalizationsInProgress > 0 {
                    ProgressView("Saving \(model.finalizationsInProgress) recording(s)…")
                }
                if !model.recoverableFiles.isEmpty {
                    Label {
                        Text("\(model.recoverableFiles.count) interrupted recording(s)")
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                    Text("Choose a recording to recover or inspect. Originals are kept if recovery fails.")
                        .font(.caption).foregroundStyle(.secondary)
                    if model.recoveryInProgress {
                        ProgressView("Recovering in an isolated worker…")
                    }
                    Menu("Interrupted recordings") {
                        ForEach(model.recoverableFiles, id: \.self) { file in
                            Menu(MeetingStore().paths(forInterruptedFile: file).baseName) {
                                Button("Recover") { model.recoverInterruptedRecording(file) }
                                    .disabled(model.recoveryInProgress)
                                Button("Show files") { model.revealInterruptedRecording(file) }
                            }
                        }
                    }
                }
                if let recoveryError = model.recoveryError {
                    Text(recoveryError)
                        .font(.callout).foregroundStyle(.primary)
                        .textSelection(.enabled)
                }
            }
            Divider()
            Menu("Permissions") {
                Button("Microphone…") { model.openPermissionSettings("Privacy_Microphone") }
                Button("System Audio Recording…") { model.openPermissionSettings("Privacy_ScreenCapture") }
                Button("Files and Folders…") { model.openPermissionSettings("Privacy_FilesAndFolders") }
            }
            Toggle("Start at login and keep running", isOn: Binding(
                get: { keepRunning.isRegistered },
                set: { keepRunning.setEnabled($0) }
            ))
            if keepRunning.status == .requiresApproval {
                Button("Allow in Login Items…") { keepRunning.openSettings() }
                Text("Automatic startup and restart require approval in System Settings.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error = keepRunning.errorMessage {
                Text(error).textSelection(.enabled)
            }
            Button("Disable and Quit") { keepRunning.disableAndQuit() }
                .disabled(!model.allowsTermination)
        }
        .lineLimit(nil)
        .multilineTextAlignment(.leading)
        .fixedSize(horizontal: false, vertical: true)
        .padding(14)
        .frame(width: 320)
        .task { keepRunning.refresh() }
    }
}


private struct LevelRow: View {
    let name: String
    let level: Float
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(name).font(.caption).lineLimit(1)
            ProgressView(value: level).progressViewStyle(.linear)
        }
    }
}
