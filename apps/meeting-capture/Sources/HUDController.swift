import AppKit
import SwiftUI

@MainActor
final class HUDController {
    static let shared = HUDController()

    private var panel: NSPanel?

    func attach(to model: AppModel) {
        guard panel == nil else { return }

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 86),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.contentView = NSHostingView(rootView: RecordingHUD(model: model))
        position(panel)
        panel.orderFrontRegardless()
        self.panel = panel

        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { if let panel = self?.panel { self?.position(panel) } }
        }
    }

    private func position(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let frame = screen.frame
        panel.setFrameOrigin(NSPoint(x: frame.midX - panel.frame.width / 2, y: frame.maxY - panel.frame.height - 4))
    }
}

private struct RecordingHUD: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var capture: CaptureCoordinator

    init(model: AppModel) {
        self.model = model
        capture = model.capture
    }

    var body: some View {
        Group {
            switch model.state {
            case let .countdown(client, remaining):
                HStack(spacing: 12) {
                    Image(systemName: "mic.fill").foregroundStyle(.orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(client.applicationName).font(.headline).lineLimit(1)
                        Text("Recording in \(remaining)s").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Skip") { model.skip() }
                    Button("Start") { model.startNow() }.buttonStyle(.borderedProminent)
                }
            case let .recording(client):
                HStack(spacing: 12) {
                    Circle().fill(.red).frame(width: 10, height: 10)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(client.applicationName).font(.headline).lineLimit(1)
                        if let startedAt = capture.startedAt {
                            TimelineView(.periodic(from: .now, by: 1)) { _ in Text(startedAt, style: .timer).font(.caption).monospacedDigit() }
                        }
                    }
                    Spacer()
                    Button("Stop") { model.stopRecording() }.buttonStyle(.borderedProminent).tint(.red)
                }
            case .finalizing:
                HStack { ProgressView(); Text("Preparing recording…"); Spacer() }
            default:
                EmptyView()
            }
        }
        .padding(.horizontal, 16)
        .frame(width: 360, height: visible ? 72 : 0)
        .background(.regularMaterial, in: Capsule())
        .opacity(visible ? 1 : 0)
        .animation(.snappy, value: visible)
    }

    private var visible: Bool {
        switch model.state {
        case .countdown, .recording, .finalizing: true
        default: false
        }
    }
}
