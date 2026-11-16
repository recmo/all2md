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
        refresh()
        // Register once, so disabling the agent in System Settings survives
        // subsequent manual launches and upgrades.
        guard !UserDefaults.standard.bool(forKey: "keepRunningConfigured") else { return }
        UserDefaults.standard.set(true, forKey: "keepRunningConfigured")
        if !isRegistered { setEnabled(true) }
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
