import CoreAudio
import AVFoundation
import CoreMedia
import Foundation

@MainActor
final class CaptureCoordinator: ObservableObject {
    @Published private(set) var microphoneLevel: Float = 0
    @Published private(set) var processAudioLevel: Float = 0
    @Published private(set) var activeMicrophoneName: String?
    @Published private(set) var startedAt: Date?
    var onFailure: ((String) -> Void)?

    private let microphone = MicrophoneRecorder()
    private let processMonitor = AudioActivityMonitor()
    private var processRecorders: [UInt32: (processID: Int32, trackID: UUID, recorder: SystemAudioRecorder)] = [:]
    private var processCaptureTask: Task<Void, Never>?
    @Published private(set) var processTrackCount = 0
    private var accessibilityWorker: BackgroundWorkerProcess?
    private var accessibilityWorkerID: UUID?
    private let backgroundWorker = try? BackgroundWorkerClient()
    private let store = MeetingStore()
    private var recordingClaim: RecordingClaim?
    private var claimedRecordings = Set<URL>()
    private var session: FinalizeCaptureRequest?
    private var hostStart: CMTime = .zero
    private var currentMicrophoneDevice: AudioInputDevice?
    private var stopping = false

    func start(trigger: CaptureTrigger, microphoneDevice: AudioInputDevice?) async throws {
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw CaptureError.microphonePermissionRequired
        }
        hostStart = CMClockGetTime(CMClockGetHostTimeClock())
        let start = Date()
        let paths = try store.paths(startedAt: start, title: trigger.applicationName)
        recordingClaim = try RecordingClaim(paths)
        claimedRecordings.insert(paths.manifest)
        session = FinalizeCaptureRequest(
            microphoneSegments: [], processTracks: [],
            captureStartedAt: start, captureEndedAt: start, paths: paths, trigger: trigger,
            metadata: [], warnings: [], interruptions: []
        )
        startedAt = start
        currentMicrophoneDevice = microphoneDevice
        activeMicrophoneName = microphoneDevice?.name ?? "System default microphone"
        if let title = trigger.applicationName {
            session?.metadata.append(MetadataEvent(timestamp: start, kind: .windowTitle, value: title, confidence: 0.7))
        }
        if let microphoneDevice { addMicrophoneMetadata(microphoneDevice) }
        let identity = paths.manifest
        microphone.onLevel = { [weak self] level in
            Task { @MainActor in
                guard self?.session?.paths.manifest == identity else { return }
                self?.microphoneLevel = level
            }
        }
        let failed: @Sendable (Error) -> Void = { [weak self] error in
            let message = error.localizedDescription
            Task { @MainActor in
                guard self?.session?.paths.manifest == identity else { return }
                self?.recordFailure(message)
            }
        }
        microphone.onError = failed
        microphone.onConfigurationChange = { [weak self] in
            Task { @MainActor in
                guard let self, self.session?.paths.manifest == identity,
                      !self.stopping, !self.microphone.isRunning else { return }
                self.restartMicrophone(device: self.currentMicrophoneDevice, reason: "Microphone audio configuration changed")
            }
        }
        do {
            let startTime = CMClockGetTime(CMClockGetHostTimeClock())
            let segment = try prepareMicrophoneSegment(at: startTime)
            try microphone.start(to: segment.url, deviceID: microphoneDevice?.id,
                                 startedAt: segment.startedAt, startTime: startTime)
            try await captureOutputProcesses()
            processCaptureTask = Task { [weak self] in
                do {
                    while !Task.isCancelled {
                        try await Task.sleep(for: .milliseconds(250))
                        guard let self, !self.stopping else { return }
                        try await self.captureOutputProcesses()
                    }
                } catch is CancellationError {
                    // Stopping waits for any in-flight tap startup below.
                } catch {
                    self?.recordFailure("Process audio capture: \(error.localizedDescription)")
                }
            }
            if let processID = trigger.processID, let applicationName = trigger.applicationName {
                startAccessibilityWorker(client: AudioClient(audioObjectID: 0, processID: processID,
                    bundleID: trigger.bundleID, applicationName: applicationName, inputDevices: []),
                    outputURL: paths.accessibilityTemporary)
            }
            try session?.save()
        } catch {
            recordFailure(error.localizedDescription)
            _ = try? await stop()
            claimedRecordings.remove(identity)
            throw error
        }
    }

    private func captureOutputProcesses() async throws {
        guard let active = session, !stopping else { return }
        let snapshot = try processMonitor.outputProcesses()
        // Quiet processes keep their tap so resumed audio stays on the same
        // track. Exited processes release HAL resources but retain their files.
        for objectID in processRecorders.keys.filter({ !snapshot.all.contains($0) }) {
            if let previous = processRecorders.removeValue(forKey: objectID) {
                try await previous.recorder.stop(processExited: true)
            }
        }
        for process in snapshot.active {
            guard !stopping, !Task.isCancelled else { return }
            if processRecorders[process.objectID]?.processID == process.processID { continue }
            if let previous = processRecorders.removeValue(forKey: process.objectID) {
                try await previous.recorder.stop(processExited: true)
            }
            let id = UUID()
            let startTime = CMClockGetTime(CMClockGetHostTimeClock())
            let track = CapturedProcessTrack(id: id, process: process,
                url: active.paths.processTemporary(id), startedAt: captureDate(at: startTime))
            session?.processTracks.append(track)
            try session?.save() // Journal identity and timing before opening a file.
            let recorder = SystemAudioRecorder()
            processRecorders[process.objectID] = (process.processID, id, recorder)
            let identity = active.paths.manifest
            recorder.onLevel = { [weak self] level in
                Task { @MainActor in
                    guard self?.session?.paths.manifest == identity else { return }
                    self?.processAudioLevel = level
                }
            }
            recorder.onError = { [weak self] error in
                let message = "\(process.title): \(error.localizedDescription)"
                Task { @MainActor in
                    guard let self, self.session?.paths.manifest == identity,
                          self.processRecorders[process.objectID]?.trackID == id else { return }
                    // An ordinary process exit is not a failed meeting capture.
                    if CaptureError.isProcessTapFailure(error),
                       let live = try? self.processMonitor.processObjectIDs(), !live.contains(process.objectID) { return }
                    self.recordFailure(message)
                }
            }
            do {
                try await recorder.start(processObject: process.objectID, to: track.url, startTime: startTime)
            } catch {
                processRecorders.removeValue(forKey: process.objectID)
                try? await recorder.stop()
                if CaptureError.isProcessTapFailure(error),
                   let live = try? processMonitor.processObjectIDs(), !live.contains(process.objectID) {
                    session?.warnings.append("\(process.title) exited before audio capture started.")
                    try session?.save()
                    continue
                }
                throw CaptureError.writerFailure("\(process.title): \(error.localizedDescription)")
            }
            processTrackCount = session?.processTracks.count ?? 0
        }
    }

    func updateMicrophoneDevices(_ devices: [AudioInputDevice]) {
        guard session != nil, !stopping, !devices.isEmpty else { return }
        if let currentMicrophoneDevice, devices.contains(where: { $0.id == currentMicrophoneDevice.id }) { return }
        guard let next = devices.first else { return }
        restartMicrophone(device: next, reason: "Microphone changed to \(next.name)")
    }

    private func restartMicrophone(device: AudioInputDevice?, reason: String) {
        guard session?.captureFailed == false else { return }
        let start = captureDate()
        do {
            let startTime = CMClockGetTime(CMClockGetHostTimeClock())
            let segment = try prepareMicrophoneSegment(at: startTime)
            try microphone.restart(to: segment.url, deviceID: device?.id,
                                   startedAt: segment.startedAt, startTime: startTime)
            currentMicrophoneDevice = device
            activeMicrophoneName = device?.name ?? "System default microphone"
            if let device { addMicrophoneMetadata(device) }
            session?.interruptions.append(CaptureTimeRange(startedAt: start, endedAt: captureDate(), reason: reason))
            try session?.save()
        } catch { recordFailure("Could not restart microphone: \(error.localizedDescription)") }
    }

    private func prepareMicrophoneSegment(at hostTime: CMTime) throws -> CapturedAudioSegment {
        guard let session else { throw CaptureError.writerFailure("No active recording") }
        let date = captureDate(at: hostTime)
        let segment = CapturedAudioSegment(url: session.paths.microphoneTemporary(segment: session.microphoneSegments.count + 1),
                                           startedAt: date, endedAt: date)
        self.session?.microphoneSegments.append(segment)
        try self.session?.save() // Journal the segment before opening its audio file.
        return segment
    }

    private func captureDate(at hostTime: CMTime = CMClockGetTime(CMClockGetHostTimeClock())) -> Date {
        guard let session else { return Date() }
        return session.captureStartedAt.addingTimeInterval((hostTime - hostStart).seconds)
    }

    private func recordFailure(_ message: String) {
        guard session != nil, session?.captureFailed == false else { return }
        session?.captureFailed = true
        session?.warnings.append(message)
        let date = captureDate()
        session?.captureEndedAt = date
        session?.interruptions.append(CaptureTimeRange(startedAt: date, endedAt: date, reason: "Capture failed: \(message)"))
        try? session?.save()
        onFailure?(message)
    }

    func stop() async throws -> FinalizeCaptureRequest {
        guard session != nil else { throw CaptureError.writerFailure("No active recording") }
        stopping = true
        defer { reset() }
        session?.microphoneSegments = microphone.stop()
        if let failure = microphone.failure { recordFailure(failure.localizedDescription) }
        processCaptureTask?.cancel()
        await processCaptureTask?.value
        processCaptureTask = nil
        let liveProcesses = try? processMonitor.processObjectIDs()
        for (objectID, entry) in processRecorders {
            do { try await entry.recorder.stop(processExited: liveProcesses?.contains(objectID) == false) }
            catch { recordFailure(error.localizedDescription) }
        }
        processRecorders.removeAll()
        let endedAt = captureDate()
        session?.captureEndedAt = endedAt
        session?.processTracks.removeAll { !FileManager.default.fileExists(atPath: $0.url.path) }
        accessibilityWorker?.terminate()
        guard let finished = session else { throw CaptureError.writerFailure("No active recording") }
        do { try finished.save() }
        catch { claimedRecordings.remove(finished.paths.manifest); throw error }
        if finished.microphoneSegments.isEmpty, finished.processTracks.isEmpty {
            try? FileManager.default.removeItem(at: finished.paths.session)
        }
        return finished
    }

    func finalize(_ request: FinalizeCaptureRequest) async throws -> URL {
        defer { claimedRecordings.remove(request.paths.manifest) }
        guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
        return try await backgroundWorker.finalize(request)
    }

    func recoverableFiles() -> [URL] { store.interruptedRecordings(excluding: claimedRecordings) }

    func recoverInterruptedRecording(_ file: URL) async throws -> URL {
        let identity = store.paths(forInterruptedFile: file).manifest
        guard !claimedRecordings.contains(identity) else {
            throw CaptureError.writerFailure("Recording is already being captured or processed")
        }
        claimedRecordings.insert(identity)
        defer { claimedRecordings.remove(identity) }
        guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
        return try await backgroundWorker.recover(RecoverCaptureRequest(interruptedFile: file))
    }

    private func reset() {
        recordingClaim = nil
        session = nil
        stopping = false
        microphoneLevel = 0
        processAudioLevel = 0
        startedAt = nil
        currentMicrophoneDevice = nil
        activeMicrophoneName = nil
        accessibilityWorker?.terminate()
        accessibilityWorker = nil
        accessibilityWorkerID = nil
        microphone.onError = nil
        microphone.onConfigurationChange = nil
        processTrackCount = 0
    }

    private func addMicrophoneMetadata(_ device: AudioInputDevice) {
        let timestamp = captureDate()
        session?.metadata.append(MetadataEvent(timestamp: timestamp, kind: .microphoneDevice,
                                              value: device.manifestValue, confidence: 1))
    }

    private func startAccessibilityWorker(client: AudioClient, outputURL: URL) {
        do {
            guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
            let workerID = UUID()
            accessibilityWorkerID = workerID
            accessibilityWorker = try backgroundWorker.startAccessibilityProbe(
                AccessibilityProbeRequest(processID: client.processID, bundleID: client.bundleID,
                                          applicationName: client.applicationName, outputURL: outputURL)
            ) { [weak self] error in
                guard self?.accessibilityWorkerID == workerID else { return }
                self?.session?.warnings.append("Accessibility metadata worker failed: \(error.localizedDescription)")
                do { try self?.session?.save() }
                catch { self?.recordFailure("Could not save session: \(error.localizedDescription)") }
                self?.accessibilityWorker = nil
                self?.accessibilityWorkerID = nil
            }
        } catch {
            accessibilityWorkerID = nil
            session?.warnings.append("Accessibility metadata unavailable: \(error.localizedDescription)")
        }
    }
}
