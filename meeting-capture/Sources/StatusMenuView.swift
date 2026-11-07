import SwiftUI

struct StatusMenuView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var capture: CaptureCoordinator

    init(model: AppModel) {
        self.model = model
        capture = model.capture
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch model.state {
            case .idle:
                Label("Watching microphone activity", systemImage: "mic")
                if let report = model.startupReport {
                    Text(report.systemAudioObserved
                         ? "Startup recording check passed"
                         : "Microphone verified; system capture authorized. No system audio was observed during the check.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("App and worker storage / archive checks passed.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(report.accessibility).font(.caption).foregroundStyle(.secondary)
                }
                Button("Check recording access again") { model.checkPermissions() }
                Button("Start recording manually") { model.manualStart() }
                Text("Records the microphone and every app producing audio as separate tracks.")
                    .font(.caption).foregroundStyle(.secondary)
            case let .recording(client):
                Label("Recording \(client.applicationName)", systemImage: "record.circle.fill").foregroundStyle(.red)
                if let startedAt = capture.startedAt { TimelineView(.periodic(from: .now, by: 1)) { _ in Text(startedAt, style: .timer).monospacedDigit() } }
                LevelRow(name: capture.activeMicrophoneName ?? client.inputDeviceSummary, level: capture.microphoneLevel)
                LevelRow(name: "Application audio", level: capture.processAudioLevel)
                Text("\(capture.processTrackCount) process track(s)").font(.caption).foregroundStyle(.secondary)
                Button("Stop") { model.stopRecording() }.keyboardShortcut(.defaultAction)
                if client.processID > 0 {
                    Button("Ignore for one hour") { model.ignoreForHour() }
                    Button("Never record this application") { model.neverRecordApplication() }
                }
            case .finalizing:
                ProgressView("Preparing recording…")
            case .checkingPermissions:
                ProgressView("Checking recording access…")
                Text("A brief test captures microphone and system audio, then deletes it. Respond to any macOS permission prompts.")
                    .font(.caption).foregroundStyle(.secondary)
            case .permissionRequired:
                Label {
                    Text("Recording check failed")
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                Text(model.permissionMessage)
                    .textSelection(.enabled)
                Text("Retry after addressing the error. If you changed macOS permissions, quit and reopen Meeting Capture first.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Retry recording check") { model.checkPermissions() }
                Button("Copy error details") { model.copyRecordingCheckError() }
            case let .error(message):
                Label {
                    Text(message).textSelection(.enabled)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                Button("Dismiss") { model.dismissError() }
            }
            if model.showsMaintenance {
                if model.lastManifest != nil { Button("Reveal last recording") { model.revealLastRecording() } }
                if model.finalizationsInProgress > 0 {
                    ProgressView("Finalizing \(model.finalizationsInProgress) recording(s) in isolated workers…")
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
            Text("Permissions").font(.caption).foregroundStyle(.secondary)
            permissionRow("Microphone", granted: model.microphoneAccess, pane: "Privacy_Microphone")
            permissionRow("System Audio Recording", granted: model.systemAudioAccess, pane: "Privacy_ScreenCapture")
            permissionRow("Documents / Files & Folders", granted: model.documentsAccess, pane: "Privacy_FilesAndFolders")
            permissionRow("Accessibility (optional)", granted: model.accessibilityAccess, pane: "Privacy_Accessibility")
            Text("✓ Available   ○ Unavailable   — Unknown\nAudio access reflects the last recording check.\nClick a permission to open System Settings.")
                .font(.caption).foregroundStyle(.secondary)
            Divider()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .disabled(!model.allowsTermination)
        }
        .lineLimit(nil)
        .multilineTextAlignment(.leading)
        .fixedSize(horizontal: false, vertical: true)
        .padding(14)
        .frame(width: 320)
        .task { await model.refreshPermissionStatus() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await model.refreshPermissionStatus() }
        }
    }

    private func permissionRow(_ title: String, granted: Bool?, pane: String) -> some View {
        Button { model.openPermissionSettings(pane) } label: {
            HStack {
                Image(systemName: granted == true ? "checkmark" : (granted == false ? "circle" : "minus"))
                    .frame(width: 16)
                Text(title)
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title): \(granted == true ? "available" : (granted == false ? "unavailable" : "unknown")). Open System Settings")
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
