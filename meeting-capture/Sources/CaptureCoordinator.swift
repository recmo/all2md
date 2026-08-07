import CoreAudio
import Foundation

@MainActor
final class CaptureCoordinator: ObservableObject {
    @Published private(set) var microphoneLevel: Float = 0
    @Published private(set) var participantsLevel: Float = 0
    @Published private(set) var activeMicrophoneName: String?
    @Published private(set) var startedAt: Date?

    private let microphone = MicrophoneRecorder()
    private let participants = SystemAudioRecorder()
    private let store = MeetingStore()
    private var paths: RecordingPaths?
    private var trigger: CaptureTrigger?
    private var metadata: [MetadataEvent] = []
    private var warnings: [String] = []
    private var interruptions: [CaptureTimeRange] = []
    private var currentMicrophoneDevice: AudioInputDevice?
    private var failedMicrophoneDeviceID: AudioDeviceID?
    private var microphoneSegmentIndex = 0

    func start(trigger: CaptureTrigger, microphoneDevice: AudioInputDevice?) async throws {
        let start = Date()
        let title = trigger.processID.flatMap { AccessibilityMetadataProvider.windowTitle(processID: $0) } ?? trigger.applicationName
        let paths = try store.paths(startedAt: start, title: title)
        self.paths = paths
        self.trigger = trigger
        startedAt = start
        if let title { metadata.append(MetadataEvent(timestamp: start, kind: .windowTitle, value: title, confidence: 0.7)) }
        currentMicrophoneDevice = microphoneDevice
        activeMicrophoneName = microphoneDevice?.name ?? "System default microphone"
        if let microphoneDevice {
            metadata.append(MetadataEvent(timestamp: start, kind: .microphoneDevice, value: microphoneDevice.manifestValue, confidence: 1))
        }

        microphone.onLevel = { [weak self] level in Task { @MainActor in self?.microphoneLevel = level } }
        microphone.onError = { [weak self] error in
            Task { @MainActor in self?.addWarning("Microphone recording error: \(error.localizedDescription)") }
        }
        participants.onLevel = { [weak self] level in Task { @MainActor in self?.participantsLevel = level } }
        do {
            try microphone.start(to: nextMicrophoneSegmentURL(), deviceID: microphoneDevice?.id)
            if let pid = trigger.processID {
                do { try await participants.start(processID: pid, to: paths.participantsTemporary) }
                catch { warnings.append("Participant audio unavailable: \(error.localizedDescription)") }
            } else {
                warnings.append("Participant audio unavailable for a manual recording without a selected process.")
            }
        } catch {
            for segment in microphone.stop() { try? FileManager.default.removeItem(at: segment.url) }
            reset()
            throw error
        }
    }

    func updateMicrophoneDevices(_ devices: [AudioInputDevice]) {
        guard startedAt != nil, !devices.isEmpty else { return }
        if let currentMicrophoneDevice, devices.contains(where: { $0.id == currentMicrophoneDevice.id }) {
            failedMicrophoneDeviceID = nil
            return
        }
        guard let nextDevice = devices.first else { return }
        guard nextDevice.id != failedMicrophoneDeviceID else { return }

        let previousName = activeMicrophoneName ?? "unknown microphone"
        let switchStarted = Date()
        do {
            try microphone.switchDevice(
                to: nextDevice.id,
                segmentURL: nextMicrophoneSegmentURL(),
                rollbackURL: nextMicrophoneSegmentURL()
            )
            let switchEnded = Date()
            currentMicrophoneDevice = nextDevice
            failedMicrophoneDeviceID = nil
            activeMicrophoneName = nextDevice.name
            metadata.append(MetadataEvent(timestamp: switchEnded, kind: .microphoneDevice, value: nextDevice.manifestValue, confidence: 1))
            interruptions.append(CaptureTimeRange(
                startedAt: switchStarted,
                endedAt: switchEnded,
                reason: "microphone device switch from \(previousName) to \(nextDevice.name)"
            ))
        } catch {
            let switchEnded = Date()
            failedMicrophoneDeviceID = nextDevice.id
            if microphone.deviceID == nil { activeMicrophoneName = "Microphone unavailable" }
            interruptions.append(CaptureTimeRange(
                startedAt: switchStarted,
                endedAt: switchEnded,
                reason: "failed microphone device switch from \(previousName) to \(nextDevice.name)"
            ))
            addWarning("Could not follow microphone switch to \(nextDevice.name): \(error.localizedDescription)")
        }
    }

    func stop() async throws -> URL {
        guard let start = startedAt, let paths, let trigger else { throw CaptureError.writerFailure("no active recording") }
        let microphoneSegments = microphone.stop()
        do { try await participants.stop() } catch { warnings.append(error.localizedDescription) }
        let end = Date()
        defer { reset() }

        let participantsURL = FileManager.default.fileExists(atPath: paths.participantsTemporary.path)
            ? paths.participantsTemporary
            : nil
        let participantsStartedAt = participants.firstSampleAt
        let archive = try await Task.detached {
            try AudioFinalizer.createArchive(
                microphoneSegments: microphoneSegments,
                participants: participantsURL,
                participantsStartedAt: participantsStartedAt,
                captureStartedAt: start,
                captureEndedAt: end,
                temporaryDestination: paths.archiveTemporary,
                finalDestination: paths.archiveFinal
            )
        }.value
        let status: CaptureManifest.Status = archive.tracks.contains(where: { $0.role == .microphone }) && archive.tracks.contains(where: { $0.role == .participants }) ? .complete : .incomplete
        let manifest = CaptureManifest(
            schemaVersion: 2,
            meetingID: UUID(),
            slug: String(paths.baseName.dropFirst(min(11, paths.baseName.count))),
            title: metadata.first(where: { $0.kind == .windowTitle })?.value,
            platform: trigger.applicationName,
            calendarEventID: nil,
            startedAt: start,
            endedAt: end,
            timeZone: TimeZone.current.identifier,
            trigger: trigger,
            container: archive.container,
            audio: archive.tracks,
            interruptions: interruptions,
            metadataEvents: metadata,
            warnings: warnings,
            status: status
        )
        try store.write(manifest, to: paths.manifest)
        for segment in microphoneSegments { try? FileManager.default.removeItem(at: segment.url) }
        try? FileManager.default.removeItem(at: paths.participantsTemporary)
        return paths.manifest
    }

    func recoverableFiles() -> [URL] { store.interruptedRecordings() }

    func recoverInterruptedRecordings() async throws -> URL? {
        let files = store.interruptedRecordings()
        guard let first = files.first else { return nil }
        let paths = store.paths(forInterruptedFile: first)
        let relatedFiles = files.filter { store.paths(forInterruptedFile: $0).baseName == paths.baseName }
        let datedFiles = relatedFiles.compactMap { url -> (URL, Date, Date)? in
            guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) else { return nil }
            return (url, attributes[.creationDate] as? Date ?? Date(), attributes[.modificationDate] as? Date ?? Date())
        }
        let started = datedFiles.map { $0.1 }.min() ?? Date()
        let ended = max(started, datedFiles.map { $0.2 }.max() ?? Date())
        let microphoneURLs = relatedFiles.filter { $0.lastPathComponent.contains("-microphone") }
        let microphoneSegments = try AudioFinalizer.recoveredSegments(from: microphoneURLs, startedAt: started)
        let participantsURL = relatedFiles.first { $0.lastPathComponent.hasSuffix("-participants.part.caf") }
        let archive = try await Task.detached {
            try AudioFinalizer.createArchive(
                microphoneSegments: microphoneSegments,
                participants: participantsURL,
                participantsStartedAt: participantsURL == nil ? nil : started,
                captureStartedAt: started,
                captureEndedAt: ended,
                temporaryDestination: paths.archiveTemporary,
                finalDestination: paths.archiveFinal
            )
        }.value
        let manifest = CaptureManifest(
            schemaVersion: 2,
            meetingID: UUID(),
            slug: String(paths.baseName.dropFirst(min(11, paths.baseName.count))),
            title: nil,
            platform: nil,
            calendarEventID: nil,
            startedAt: started,
            endedAt: max(started, ended),
            timeZone: TimeZone.current.identifier,
            trigger: CaptureTrigger(method: .deviceRunning, processID: nil, bundleID: nil, applicationName: nil),
            container: archive.container,
            audio: archive.tracks,
            interruptions: [CaptureTimeRange(startedAt: started, endedAt: ended, reason: "application interruption")],
            metadataEvents: [],
            warnings: ["Recovered from crash-safe temporary audio; capture metadata may be incomplete."],
            status: .incomplete
        )
        try store.write(manifest, to: paths.manifest)
        for file in relatedFiles {
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
        currentMicrophoneDevice = nil
        failedMicrophoneDeviceID = nil
        microphoneSegmentIndex = 0
        activeMicrophoneName = nil
        microphone.onError = nil
    }

    private func addWarning(_ warning: String) {
        guard !warnings.contains(warning) else { return }
        warnings.append(warning)
    }

    private func nextMicrophoneSegmentURL() -> URL {
        microphoneSegmentIndex += 1
        return paths!.microphoneTemporary(segment: microphoneSegmentIndex)
    }
}
