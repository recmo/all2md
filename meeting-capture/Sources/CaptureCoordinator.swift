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
    private var accessibilityWorker: BackgroundWorkerProcess?
    private var accessibilityWorkerID: UUID?
    private let backgroundWorker = try? BackgroundWorkerClient()
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
        let title = trigger.applicationName
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
                try await participants.start(processID: pid, bundleID: trigger.bundleID, to: paths.participantsTemporary)
            } else {
                warnings.append("Participant audio unavailable for a manual recording without a selected process.")
            }
            if let processID = trigger.processID, let applicationName = trigger.applicationName {
                let client = AudioClient(
                    audioObjectID: 0,
                    processID: processID,
                    bundleID: trigger.bundleID,
                    applicationName: applicationName,
                    inputDevices: []
                )
                startAccessibilityWorker(client: client, outputURL: paths.accessibilityTemporary)
            }
        } catch {
            for segment in microphone.stop() { try? FileManager.default.removeItem(at: segment.url) }
            try? await participants.stop()
            accessibilityWorker?.terminate()
            accessibilityWorker = nil
            accessibilityWorkerID = nil
            try? FileManager.default.removeItem(at: paths.accessibilityTemporary)
            try? FileManager.default.removeItem(at: paths.participantsTemporary)
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

    func stop() async throws -> FinalizeCaptureRequest {
        guard let start = startedAt, let paths, let trigger else { throw CaptureError.writerFailure("no active recording") }
        let microphoneSegments = microphone.stop()
        do { try await participants.stop() } catch { warnings.append(error.localizedDescription) }
        let end = Date()
        let participantsURL = FileManager.default.fileExists(atPath: paths.participantsTemporary.path)
            ? paths.participantsTemporary
            : nil
        let participantsStartedAt = participants.firstSampleAt
        let accessibilityWorkerProcessID = accessibilityWorker?.terminate()
        accessibilityWorker = nil
        accessibilityWorkerID = nil
        let request = FinalizeCaptureRequest(
            microphoneSegments: microphoneSegments,
            participants: participantsURL,
            participantsStartedAt: participantsStartedAt,
            captureStartedAt: start,
            captureEndedAt: end,
            paths: paths,
            accessibilityWorkerProcessID: accessibilityWorkerProcessID,
            trigger: trigger,
            metadata: metadata,
            warnings: warnings,
            interruptions: interruptions
        )
        reset()
        return request
    }

    func finalize(_ request: FinalizeCaptureRequest) async throws -> URL? {
        guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
        return try await backgroundWorker.finalize(request)
    }

    func recoverableFiles() -> [URL] { store.interruptedRecordings() }

    func recoverInterruptedRecording(_ file: URL) async throws -> URL? {
        guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
        return try await backgroundWorker.recover(RecoverCaptureRequest(interruptedFile: file))
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
        accessibilityWorker?.terminate()
        accessibilityWorker = nil
        accessibilityWorkerID = nil
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

    private func startAccessibilityWorker(client: AudioClient, outputURL: URL) {
        do {
            guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
            let workerID = UUID()
            accessibilityWorkerID = workerID
            accessibilityWorker = try backgroundWorker.startAccessibilityProbe(
                AccessibilityProbeRequest(
                    processID: client.processID,
                    bundleID: client.bundleID,
                    applicationName: client.applicationName,
                    outputURL: outputURL
                )
            ) { [weak self] error in
                guard self?.accessibilityWorkerID == workerID else { return }
                self?.addWarning("Accessibility metadata worker failed: \(error.localizedDescription)")
                self?.accessibilityWorker = nil
                self?.accessibilityWorkerID = nil
            }
        } catch {
            accessibilityWorkerID = nil
            warnings.append(
                "Accessibility metadata unavailable: \(error.localizedDescription)"
            )
        }
    }
}
