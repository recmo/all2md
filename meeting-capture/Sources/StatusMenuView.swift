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
                Button("Start recording manually") { model.manualStart() }
            case let .detecting(client, _):
                Label("Checking \(client.applicationName)…", systemImage: "waveform")
            case let .countdown(client, remaining):
                Text("\(client.applicationName) is using the microphone")
                    .font(.headline)
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
                LevelRow(name: "Microphone", level: capture.microphoneLevel)
                LevelRow(name: "Participants", level: capture.participantsLevel)
                Button("Stop") { model.stopRecording() }.keyboardShortcut(.defaultAction)
            case .finalizing:
                ProgressView("Preparing recording…")
            case .permissionRequired:
                Label("Screen & System Audio Recording permission is required", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("Meeting Capture is paused until access is granted. If macOS requests it, quit and reopen the app after enabling access.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Open Screen & System Audio Settings") { model.openScreenRecordingSettings() }
            case let .error(message):
                Label(message, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Button("Dismiss") { model.dismissError() }
            }
            if model.lastManifest != nil { Button("Reveal last recording") { model.revealLastRecording() } }
            if !model.recoverableFiles.isEmpty {
                Text("\(model.recoverableFiles.count) interrupted recording file(s) need recovery").font(.caption).foregroundStyle(.orange)
                Button("Recover interrupted recording") { model.recoverInterruptedRecordings() }
            }
            Divider()
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .padding(14)
        .frame(width: 320)
    }
}

private struct LevelRow: View {
    let name: String
    let level: Float
    var body: some View { HStack { Text(name).frame(width: 88, alignment: .leading); ProgressView(value: level).progressViewStyle(.linear) } }
}
