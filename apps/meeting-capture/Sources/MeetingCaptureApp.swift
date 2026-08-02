import SwiftUI

@main
struct MeetingCaptureApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            StatusMenuView(model: model)
                .task {
                    model.start()
                    HUDController.shared.attach(to: model)
                }
        } label: {
            Label("Meeting Capture", systemImage: model.statusIcon)
        }
        .menuBarExtraStyle(.window)
    }
}
