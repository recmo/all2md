import SwiftUI

@main
struct MeetingCaptureApp: App {
    @StateObject private var model = AppModel()
    @NSApplicationDelegateAdaptor(CaptureAppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            StatusMenuView(model: model)
        } label: {
            Label("Meeting Capture", systemImage: model.statusIcon)
                .task {
                    delegate.model = model
                    guard NSClassFromString("XCTestCase") == nil else { return }
                    model.start()
                    HUDController.shared.attach(to: model)
                }
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
final class CaptureAppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        model?.allowsTermination == false ? .terminateCancel : .terminateNow
    }
}
