import AppKit
import Combine
import SwiftUI

@MainActor
final class HUDController {
    static let shared = HUDController()

    private weak var model: AppModel?
    private var panel: NSPanel?
    private var stateObserver: AnyCancellable?

    func attach(to model: AppModel) {
        guard panel == nil else { return }
        self.model = model

        let panel = NotchPanel(
            contentRect: .zero,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        self.panel = panel
        updateLayout()
        stateObserver = model.$state.sink { [weak self] state in
            MainActor.assumeIsolated { self?.setVisible(state.showsHUD) }
        }

        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.updateLayout() }
        }
    }

    private func updateLayout() {
        guard let panel, let model, let screen = NSScreen.main else { return }
        let layout = HUDLayout(screen: screen)
        panel.setContentSize(layout.size)
        panel.contentView = NSHostingView(rootView: RecordingHUD(model: model, layout: layout))

        let y = layout.hasNotch
            ? screen.visibleFrame.maxY
            : screen.visibleFrame.maxY - layout.size.height - 6
        panel.setFrameOrigin(NSPoint(x: screen.frame.midX - layout.size.width / 2, y: y))
    }

    private func setVisible(_ visible: Bool) {
        guard let panel else { return }
        panel.ignoresMouseEvents = !visible
        if visible {
            panel.orderFrontRegardless()
            // Ordering a non-activating panel lets AppKit move it below the
            // menu bar. Restore the notch position after it is onscreen.
            updateLayout()
        } else {
            panel.orderOut(nil)
        }
    }
}

/// AppKit normally moves utility windows below the menu bar. The recording HUD
/// deliberately joins the hardware notch, so it must be allowed into that band.
private final class NotchPanel: NSPanel {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        frameRect
    }
}

private extension AppModel.State {
    var showsHUD: Bool {
        switch self {
        case .countdown, .recording, .finalizing: true
        default: false
        }
    }
}

private struct HUDLayout {
    let notchWidth: CGFloat
    let hasNotch: Bool
    let size: NSSize

    init(screen: NSScreen) {
        if let left = screen.auxiliaryTopLeftArea,
           let right = screen.auxiliaryTopRightArea,
           right.minX > left.maxX {
            let gap = right.minX - left.maxX
            // The safe-area inset describes the auxiliary menu-bar regions,
            // which can be one point shorter than the actual content boundary.
            // visibleFrame is the authoritative edge the HUD must meet.
            let notchHeight = max(screen.frame.maxY - screen.visibleFrame.maxY, 42)
            notchWidth = gap
            hasNotch = true
            size = NSSize(width: min(gap + 320, screen.frame.width - 24), height: notchHeight)
        } else {
            notchWidth = 0
            hasNotch = false
            size = NSSize(width: 360, height: 44)
        }
    }
}

private struct RecordingHUD: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var capture: CaptureCoordinator
    let layout: HUDLayout

    init(model: AppModel, layout: HUDLayout) {
        self.model = model
        capture = model.capture
        self.layout = layout
    }

    var body: some View {
        Group {
            if visible {
                if layout.hasNotch {
                    notchContent
                } else {
                    compactContent
                }
            }
        }
        .frame(width: layout.size.width, height: layout.size.height, alignment: .top)
        .background {
            if visible {
                if layout.hasNotch {
                    UnevenRoundedRectangle(
                        topLeadingRadius: 0,
                        bottomLeadingRadius: 15,
                        bottomTrailingRadius: 15,
                        topTrailingRadius: 0
                    )
                    .fill(.black)
                } else {
                    Capsule().fill(.black)
                }
            }
        }
        .foregroundStyle(.white)
        .opacity(visible ? 1 : 0)
        .animation(.snappy, value: visible)
    }

    private var notchContent: some View {
        HStack(spacing: 0) {
            HStack(spacing: 7) {
                statusMark
                Text(applicationName)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Color.clear.frame(width: layout.notchWidth + 12)

            HStack(spacing: 8) {
                statusDetail
                actionButtons
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 13)
        .frame(height: layout.size.height)
    }

    private var compactContent: some View {
        HStack(spacing: 10) {
            statusMark
            Text(applicationName)
                .font(.system(size: 12, weight: .semibold))
                .lineLimit(1)
            Spacer(minLength: 8)
            statusDetail
            actionButtons
        }
        .padding(.horizontal, 14)
        .frame(height: layout.size.height)
    }

    @ViewBuilder private var statusMark: some View {
        switch model.state {
        case .countdown:
            Image(systemName: "mic.fill").foregroundStyle(.orange)
        case .recording:
            Circle().fill(.red).frame(width: 8, height: 8)
        case .finalizing:
            ProgressView().controlSize(.small).tint(.white)
        default:
            EmptyView()
        }
    }

    @ViewBuilder private var statusDetail: some View {
        switch model.state {
        case let .countdown(_, remaining):
            Text("in \(remaining)s")
                .font(.system(size: 11, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(.secondary)
        case .recording:
            if let startedAt = capture.startedAt {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    Text(startedAt, style: .timer)
                        .font(.system(size: 11, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
        default:
            EmptyView()
        }
    }

    @ViewBuilder private var actionButtons: some View {
        switch model.state {
        case .countdown:
            Button(action: model.skip) {
                Image(systemName: "xmark").frame(width: 18, height: 18)
            }
            .buttonStyle(.plain)
            .help("Skip")
            Button(action: model.startNow) {
                Image(systemName: "record.circle.fill")
                    .foregroundStyle(.red)
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.plain)
            .help("Start recording")
        case .recording:
            Button(action: model.stopRecording) {
                Image(systemName: "stop.fill")
                    .font(.system(size: 9, weight: .bold))
                    .frame(width: 20, height: 20)
                    .background(.red, in: Circle())
            }
            .buttonStyle(.plain)
            .help("Stop recording")
        default:
            EmptyView()
        }
    }

    private var applicationName: String {
        switch model.state {
        case let .countdown(client, _), let .recording(client):
            client.applicationName
        case .finalizing:
            "Preparing recording…"
        default:
            ""
        }
    }

    private var visible: Bool {
        switch model.state {
        case .countdown, .recording, .finalizing: true
        default: false
        }
    }
}
