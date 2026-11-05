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
                if !model.activeOutputClients.isEmpty {
                    Text("Active audio applications").font(.caption).foregroundStyle(.secondary)
                    ForEach(model.activeOutputClients) { client in
                        Button("Record \(client.applicationName)") {
                            model.manualStart(client: client)
                        }
                    }
                }
            case let .detecting(client, _):
                Label("Checking \(client.applicationName)…", systemImage: "waveform")
                Text(client.inputDeviceSummary).font(.caption).foregroundStyle(.secondary)
            case let .countdown(client, remaining):
                Text("\(client.applicationName) is using the microphone")
                    .font(.headline)
                Text(client.inputDeviceSummary).font(.caption).foregroundStyle(.secondary)
                Text("Recording in \(remaining) seconds")
                HStack {
                    Button("Start now") { model.startNow() }.keyboardShortcut(.defaultAction)
                    Button("Skip") { model.skip() }.keyboardShortcut(.cancelAction)
                }
                Button("Ignore for one hour") { model.ignoreForHour() }
                Button("Never record this application") { model.neverRecordApplication() }
            case let .recording(client):
                Label("Recording \(client.applicationName)", systemImage: "record.circle.fill").foregroundStyle(.red)
                if let startedAt = capture.startedAt { TimelineView(.periodic(from: .now, by: 1)) { _ in Text(startedAt, style: .timer).monospacedDigit() } }
                LevelRow(name: capture.activeMicrophoneName ?? client.inputDeviceSummary, level: capture.microphoneLevel)
                LevelRow(name: "Participants", level: capture.participantsLevel)
                Button("Stop") { model.stopRecording() }.keyboardShortcut(.defaultAction)
            case .finalizing:
                ProgressView("Preparing recording…")
            case .checkingPermissions:
                ProgressView("Checking recording access…")
                Text("A brief test captures microphone and system audio, then deletes it. Respond to any macOS permission prompts.")
                    .font(.caption).foregroundStyle(.secondary)
            case .permissionRequired:
                Label("Recording check failed", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text(model.permissionMessage)
                Text("Enable access for this installed app, then retry. If macOS requests a restart, quit and reopen Meeting Capture.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Retry recording check") { model.checkPermissions() }
            case let .error(message):
                Label(message, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Button("Dismiss") { model.dismissError() }
            }
            if model.showsMaintenance {
                if model.lastManifest != nil { Button("Reveal last recording") { model.revealLastRecording() } }
                if model.finalizationsInProgress > 0 {
                    ProgressView("Finalizing \(model.finalizationsInProgress) recording(s) in isolated workers…")
                }
                if !model.recoverableFiles.isEmpty {
                    Text("\(model.recoverableFiles.count) interrupted recording file(s) need recovery").font(.caption).foregroundStyle(.orange)
                    if model.recoveryInProgress {
                        ProgressView("Recovering in an isolated worker…")
                    } else {
                        Button("Recover interrupted recording") { model.recoverInterruptedRecordings() }
                    }
                }
                if let recoveryError = model.recoveryError {
                    Text(recoveryError).font(.caption).foregroundStyle(.orange)
                }
            }
            Divider()
            Text("Permissions").font(.caption).foregroundStyle(.secondary)
            permissionRow("Microphone", granted: model.microphoneAccess, pane: "Privacy_Microphone")
            permissionRow("Screen & System Audio Recording", granted: model.screenRecordingAccess, pane: "Privacy_ScreenCapture")
            permissionRow("Documents / Files & Folders", granted: model.documentsAccess, pane: "Privacy_FilesAndFolders")
            permissionRow("Accessibility (optional)", granted: model.accessibilityAccess, pane: "Privacy_Accessibility")
            Text("✓ Available   ○ Unavailable   — Unknown\nClick a permission to open System Settings.")
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
        .task {
            while !Task.isCancelled {
                await model.refreshPermissionStatus()
                do { try await Task.sleep(for: .seconds(3)) }
                catch { return }
            }
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
