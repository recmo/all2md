import Foundation

@MainActor
final class CaptureCoordinator: ObservableObject {
    @Published private(set) var microphoneLevel: Float = 0
    @Published private(set) var participantsLevel: Float = 0
    @Published private(set) var startedAt: Date?

    private let microphone = MicrophoneRecorder()
    private let participants = SystemAudioRecorder()
    private let store = MeetingStore()
    private var paths: RecordingPaths?
    private var trigger: CaptureTrigger?
    private var metadata: [MetadataEvent] = []
    private var warnings: [String] = []
    private var interruptions: [CaptureTimeRange] = []

    func start(trigger: CaptureTrigger) async throws {
        let start = Date()
        let title = trigger.processID.flatMap { AccessibilityMetadataProvider.windowTitle(processID: $0) } ?? trigger.applicationName
        let paths = try store.paths(startedAt: start, title: title)
        self.paths = paths
        self.trigger = trigger
        startedAt = start
        if let title { metadata.append(MetadataEvent(timestamp: start, kind: .windowTitle, value: title, confidence: 0.7)) }

        microphone.onLevel = { [weak self] level in Task { @MainActor in self?.microphoneLevel = level } }
        participants.onLevel = { [weak self] level in Task { @MainActor in self?.participantsLevel = level } }
        do {
            try microphone.start(to: paths.microphoneTemporary)
            if let pid = trigger.processID {
                try await participants.start(processID: pid, bundleID: trigger.bundleID, to: paths.participantsTemporary)
            } else {
                warnings.append("Participant audio unavailable for a manual recording without a selected process.")
            }
        } catch {
            microphone.stop()
            try? await participants.stop()
            try? FileManager.default.removeItem(at: paths.microphoneTemporary)
            try? FileManager.default.removeItem(at: paths.participantsTemporary)
            throw error
        }
    }

    func stop() async throws -> URL {
        guard let start = startedAt, let paths, let trigger else { throw CaptureError.writerFailure("no active recording") }
        microphone.stop()
        do { try await participants.stop() } catch { warnings.append(error.localizedDescription) }

        var tracks: [AudioTrack] = []
        if FileManager.default.fileExists(atPath: paths.microphoneTemporary.path) {
            tracks.append(try AudioFinalizer.convertToFLAC(source: paths.microphoneTemporary, destination: paths.microphoneFinal, role: .microphone))
        }
        if FileManager.default.fileExists(atPath: paths.participantsTemporary.path) {
            tracks.append(try AudioFinalizer.convertToFLAC(source: paths.participantsTemporary, destination: paths.participantsFinal, role: .participants))
        }
        let status: CaptureManifest.Status = tracks.contains(where: { $0.role == .microphone }) && tracks.contains(where: { $0.role == .participants }) ? .complete : .incomplete
        let manifest = CaptureManifest(
            schemaVersion: 1,
            meetingID: UUID(),
            slug: String(paths.baseName.dropFirst(min(11, paths.baseName.count))),
            title: metadata.first(where: { $0.kind == .windowTitle })?.value,
            platform: trigger.applicationName,
            calendarEventID: nil,
            startedAt: start,
            endedAt: Date(),
            timeZone: TimeZone.current.identifier,
            trigger: trigger,
            audio: tracks,
            interruptions: interruptions,
            metadataEvents: metadata,
            warnings: warnings,
            status: status
        )
        try store.write(manifest, to: paths.manifest)
        try? FileManager.default.removeItem(at: paths.microphoneTemporary)
        try? FileManager.default.removeItem(at: paths.participantsTemporary)
        reset()
        return paths.manifest
    }

    func recoverableFiles() -> [URL] { store.interruptedRecordings() }

    func recoverInterruptedRecordings() throws -> URL? {
        let files = store.interruptedRecordings()
        guard let first = files.first else { return nil }
        let paths = store.paths(forInterruptedFile: first)
        var tracks: [AudioTrack] = []
        if FileManager.default.fileExists(atPath: paths.microphoneTemporary.path) {
            tracks.append(try AudioFinalizer.convertToFLAC(source: paths.microphoneTemporary, destination: paths.microphoneFinal, role: .microphone))
        }
        if FileManager.default.fileExists(atPath: paths.participantsTemporary.path) {
            tracks.append(try AudioFinalizer.convertToFLAC(source: paths.participantsTemporary, destination: paths.participantsFinal, role: .participants))
        }
        let attributes = try? FileManager.default.attributesOfItem(atPath: first.path)
        let started = attributes?[.creationDate] as? Date ?? Date()
        let ended = attributes?[.modificationDate] as? Date ?? Date()
        let manifest = CaptureManifest(
            schemaVersion: 1,
            meetingID: UUID(),
            slug: String(paths.baseName.dropFirst(min(11, paths.baseName.count))),
            title: nil,
            platform: nil,
            calendarEventID: nil,
            startedAt: started,
            endedAt: max(started, ended),
            timeZone: TimeZone.current.identifier,
            trigger: CaptureTrigger(method: .deviceRunning, processID: nil, bundleID: nil, applicationName: nil),
            audio: tracks,
            interruptions: [CaptureTimeRange(startedAt: started, endedAt: ended, reason: "application interruption")],
            metadataEvents: [],
            warnings: ["Recovered from crash-safe temporary audio; capture metadata may be incomplete."],
            status: .incomplete
        )
        try store.write(manifest, to: paths.manifest)
        for file in files where file.deletingLastPathComponent() == paths.directory && file.lastPathComponent.hasPrefix(".\(paths.baseName)-") {
            try? FileManager.default.removeItem(at: file)
        }
        return paths.manifest
    }

    private func reset() {
        microphoneLevel = 0
        participantsLevel = 0
        startedAt = nil
        paths = nil
        trigger = nil
        metadata = []
        warnings = []
        interruptions = []
    }
}
