import SwiftUI

@main
struct MeetingCaptureApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            StatusMenuView(model: model)
        } label: {
            Label("Meeting Capture", systemImage: model.statusIcon)
                .task {
                    model.start()
                    HUDController.shared.attach(to: model)
                }
        }
        .menuBarExtraStyle(.window)
    }
}
