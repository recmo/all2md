@preconcurrency import AVFoundation
import CoreMedia
import Foundation

struct CaptureStatus: Sendable {
    let sessionID: UUID
    let startedAt: Date
    let microphoneName: String
    let microphoneLevel: Float
    let processAudioLevel: Float
    let processTrackCount: Int
    let warning: String?
}

// All mutable capture state, native start/stop calls and journal I/O belong to
// this serial queue. Audio callbacks only feed buffered writers; UI receives
// immutable snapshots and never waits for a device or a disk flush.
final class CaptureCoordinator: @unchecked Sendable {
    var onStatus: (@Sendable (CaptureStatus) -> Void)?
    var onFailure: (@Sendable (UUID, String) -> Void)?
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.capture", qos: .userInitiated)
    private let microphone = MicrophoneRecorder()
    private let catalog = AudioProcessCatalog()
    private let store = MeetingStore()
    private let backgroundWorker = try? BackgroundWorkerClient()
    private var session: RecordingSession?
    private var sessionID: UUID?
    private var claim: RecordingClaim?
    private var claimedRecordings = Set<URL>()
    private var hostStart: CMTime = .zero
    private var timer: DispatchSourceTimer?
    private var nextPoll: TimeInterval = 0
    private var nextHealth: TimeInterval = 0
    private var fatalFailure = false
    private var microphoneGeneration = UUID()
    private var microphoneDevice: AudioInputDevice?
    private var microphoneStarted: TimeInterval = 0
    private var microphoneRetry: TimeInterval?
    private var microphoneInterruptedAt: Date?
    private struct ProcessTrack {
        let process: AudioProcess
        let id: UUID
        let recorder: ProcessAudioRecorder
    }
    private struct Retry {
        let process: AudioProcess
        let startedAt: Date
        let after: TimeInterval
    }
    private var processes: [UInt32: ProcessTrack] = [:]
    private var retries: [UInt32: Retry] = [:]
    private var accessibilityWorker: BackgroundWorkerProcess?

    func start(id: UUID, trigger: CaptureTrigger, microphoneDevice: AudioInputDevice?) async throws {
        try await perform { [self] in
            guard self.session == nil else { throw CaptureError.writerFailure("Recording is already active") }
            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                throw CaptureError.microphonePermissionRequired
            }
            self.hostStart = CMClockGetTime(CMClockGetHostTimeClock())
            let start = Date()
            let paths = try self.store.paths(startedAt: start, title: trigger.applicationName)
            self.claim = try RecordingClaim(paths)
            self.claimedRecordings.insert(paths.manifest)
            self.sessionID = id
            self.session = RecordingSession(microphoneSegments: [], processTracks: [],
                captureStartedAt: start, captureEndedAt: start, paths: paths, trigger: trigger,
                metadata: [], warnings: [], interruptions: [])
            self.microphoneDevice = microphoneDevice
            CaptureDiagnostics.log("capture.start session=\(paths.session.lastPathComponent) pid=\(trigger.processID ?? 0) \(CaptureDiagnostics.devices())")
            self.microphone.onConfigurationChange = { [weak self] in
                self?.queue.async { [weak self] in
                    guard let self, self.sessionID == id else { return }
                    CaptureDiagnostics.log("microphone.configurationChanged \(self.microphone.diagnosticSummary) \(CaptureDiagnostics.devices())")
                    if !self.microphone.isRunning, self.microphoneRetry == nil { self.microphoneFailed(CaptureError.noMicrophone) }
                }
            }
            do {
                try self.session?.save()
                self.startMicrophone()
                self.pollProcesses()
                if let processID = trigger.processID, let name = trigger.applicationName {
                    self.startMetadata(processID: processID, name: name, bundleID: trigger.bundleID, paths: paths, id: id)
                }
                self.startTimer()
                self.publish()
                if self.fatalFailure { throw CaptureError.writerFailure(self.session?.warnings.last ?? "Could not persist the recording session") }
            } catch {
                _ = try? self.stopCapture()
                self.claimedRecordings.remove(paths.manifest)
                throw error
            }
        }
    }

    func stop() async throws -> RecordingSession { try await perform { try self.stopCapture() } }

    func updateMicrophoneDevices(_ devices: [AudioInputDevice]) {
        queue.async {
            guard self.session != nil, let next = devices.first, next != self.microphoneDevice else { return }
            self.microphoneFailed(CaptureError.deviceSelectionFailure("Input changed to \(next.name)"))
            self.microphoneDevice = next
            self.startMicrophone()
        }
    }

    private func startTimer() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(100))
        timer.setEventHandler { [weak self] in self?.tick() }
        self.timer = timer
        timer.resume()
    }

    private func tick() {
        guard session != nil, !fatalFailure else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let retry = microphoneRetry, now >= retry { startMicrophone() }
        if microphoneRetry == nil, microphoneInterruptedAt == nil,
           !microphone.isRunning || (microphone.callbackAge ?? (now - microphoneStarted)) > 5 {
            microphoneFailed(CaptureError.deviceSelectionFailure("Microphone stopped delivering audio"))
        }
        if now >= nextPoll { nextPoll = now + 0.5; pollProcesses() }
        if let warning = microphone.captureWarning { warn("Microphone: \(warning)", incomplete: true) }
        for track in processes.values { recordWarning(track) }
        if now >= nextHealth {
            nextHealth = now + 5
            CaptureDiagnostics.log("capture.health processes=\(processes.count) \(CaptureDiagnostics.devices())")
            CaptureDiagnostics.log("microphone.health \(microphone.diagnosticSummary)")
            for track in processes.values {
                CaptureDiagnostics.log("process.health pid=\(track.process.processID) track=\(track.id) \(track.recorder.diagnosticSummary)")
            }
        }
        publish()
    }

    private func startMicrophone() {
        guard let session, !fatalFailure else { return }
        let host = CMClockGetTime(CMClockGetHostTimeClock())
        let date = captureDate(at: host)
        let segment = CapturedAudioSegment(url: session.paths.microphoneTemporary(segment: session.microphoneSegments.count + 1),
                                           startedAt: date, endedAt: date)
        self.session?.microphoneSegments.append(segment)
        guard save() else { return }
        let generation = UUID()
        microphoneGeneration = generation
        microphone.onError = { [weak self] error in
            self?.queue.async { [weak self] in
                guard let self, self.microphoneGeneration == generation else { return }
                self.microphoneFailed(error)
            }
        }
        do {
            try microphone.restart(to: segment.url, deviceID: microphoneDevice?.id, startedAt: date, startTime: host)
            if let interrupted = microphoneInterruptedAt {
                recordInterruption(since: interrupted, reason: "Microphone interrupted")
            }
            microphoneStarted = ProcessInfo.processInfo.systemUptime
            microphoneInterruptedAt = nil
            microphoneRetry = nil
            if let device = microphoneDevice {
                self.session?.metadata.append(MetadataEvent(timestamp: date, kind: .microphoneDevice, value: device.manifestValue, confidence: 1))
            }
            save()
        } catch { microphoneFailed(error) }
    }

    private func microphoneFailed(_ error: Error) {
        guard session != nil, !fatalFailure else { return }
        if case CaptureError.storageFailure = error { fail(error.localizedDescription); return }
        microphoneGeneration = UUID() // Ignore errors already queued by the old segment.
        microphone.pause()
        if let failure = microphone.failure, case CaptureError.storageFailure = failure {
            fail(failure.localizedDescription); return
        }
        microphoneInterruptedAt = microphoneInterruptedAt ?? captureDate()
        microphoneRetry = ProcessInfo.processInfo.systemUptime + 10
        warn("Microphone interrupted; retrying: \(error.localizedDescription)", incomplete: true)
    }

    private func pollProcesses() {
        do {
            let snapshot = try catalog.outputProcesses()
            for object in Array(processes.keys) where !snapshot.all.contains(object) { stopProcess(object, exited: true) }
            for (object, retry) in retries where !snapshot.all.contains(object) {
                endInterruption(retry, reason: "Process exited after capture interruption")
                retries[object] = nil
            }
            for process in snapshot.active {
                if processes[process.objectID]?.process.processID == process.processID { continue }
                if processes[process.objectID] != nil { stopProcess(process.objectID, exited: true) }
                if let retry = retries[process.objectID], retry.process.processID == process.processID,
                   ProcessInfo.processInfo.systemUptime < retry.after { continue }
                startProcess(process)
            }
        } catch { warn("Process discovery unavailable; retrying: \(error.localizedDescription)") }
    }

    private func startProcess(_ process: AudioProcess) {
        guard let session, let identity = sessionID, !fatalFailure else { return }
        let id = UUID()
        let host = CMClockGetTime(CMClockGetHostTimeClock())
        let track = CapturedProcessTrack(id: id, process: process, url: session.paths.processTemporary(id), startedAt: captureDate(at: host))
        self.session?.processTracks.append(track)
        guard save() else { return }
        let recorder = ProcessAudioRecorder()
        processes[process.objectID] = ProcessTrack(process: process, id: id, recorder: recorder)
        recorder.onError = { [weak self] error in
            self?.queue.async { [weak self] in
                guard let self, self.sessionID == identity, self.processes[process.objectID]?.id == id else { return }
                self.processFailed(process, error: error)
            }
        }
        do {
            try recorder.start(processObject: process.objectID, to: track.url, startTime: host)
            if let retry = retries.removeValue(forKey: process.objectID) { endInterruption(retry, reason: "Process audio interrupted") }
        } catch { processFailed(process, error: error) }
    }

    private func processFailed(_ process: AudioProcess, error: Error) {
        guard session != nil, !fatalFailure else { return }
        if case CaptureError.storageFailure = error { fail(error.localizedDescription); return }
        if CaptureError.isProcessTapFailure(error),
           let live = try? catalog.processObjectIDs(), !live.contains(process.objectID) {
            stopProcess(process.objectID, exited: true)
            return
        }
        stopProcess(process.objectID, exited: false)
        let began = retries[process.objectID]?.startedAt ?? captureDate()
        retries[process.objectID] = Retry(process: process, startedAt: began, after: ProcessInfo.processInfo.systemUptime + 10)
        warn("\(process.title) interrupted; retrying: \(error.localizedDescription)", incomplete: true)
    }

    private func stopProcess(_ object: UInt32, exited: Bool) {
        guard let track = processes.removeValue(forKey: object) else { return }
        do { try track.recorder.stop(processExited: exited) }
        catch {
            if case CaptureError.storageFailure = error { fail(error.localizedDescription) }
            else if !exited { warn("\(track.process.title): \(error.localizedDescription)", incomplete: true) }
        }
        recordWarning(track)
    }

    private func recordWarning(_ track: ProcessTrack) {
        if let warning = track.recorder.captureWarning { warn("\(track.process.title): \(warning)", incomplete: true) }
    }

    private func endInterruption(_ retry: Retry, reason: String) {
        recordInterruption(since: retry.startedAt, reason: "\(retry.process.title): \(reason)")
        save()
    }

    private func recordInterruption(since start: Date, reason: String) {
        let range = CaptureTimeRange(startedAt: start, endedAt: captureDate(), reason: reason)
        session?.interruptions.append(range)
    }

    private func warn(_ message: String, incomplete: Bool = false) {
        guard session?.warnings.contains(message) == false else { return }
        CaptureDiagnostics.error("capture.warning \(message)")
        session?.warnings.append(message)
        if incomplete { session?.captureFailed = true }
        save()
    }

    @discardableResult private func save() -> Bool {
        do { try session?.save(); return true }
        catch { fail("Could not save session: \(error.localizedDescription)"); return false }
    }

    private func fail(_ message: String) {
        guard let id = sessionID, !fatalFailure else { return }
        fatalFailure = true
        session?.captureFailed = true
        session?.warnings.append(message)
        CaptureDiagnostics.error("capture.failed \(message)")
        try? session?.save()
        onFailure?(id, message)
    }

    private func captureDate(at time: CMTime = CMClockGetTime(CMClockGetHostTimeClock())) -> Date {
        (session?.captureStartedAt ?? Date()).addingTimeInterval((time - hostStart).seconds)
    }

    private func publish() {
        guard let session, let id = sessionID else { return }
        onStatus?(CaptureStatus(sessionID: id, startedAt: session.captureStartedAt,
            microphoneName: microphoneDevice?.name ?? "System default microphone",
            microphoneLevel: microphone.level, processAudioLevel: processes.values.map { $0.recorder.level }.max() ?? 0,
            processTrackCount: session.processTracks.count, warning: session.warnings.last))
    }

    private func stopCapture() throws -> RecordingSession {
        guard session != nil else { throw CaptureError.writerFailure("No active recording") }
        CaptureDiagnostics.log("capture.stop")
        timer?.cancel(); timer = nil
        microphoneGeneration = UUID()
        microphone.onError = nil
        microphone.onConfigurationChange = nil
        session?.microphoneSegments = microphone.stop()
        if let failure = microphone.failure { warn("Microphone: \(failure.localizedDescription)", incomplete: true) }
        if let warning = microphone.captureWarning { warn("Microphone: \(warning)", incomplete: true) }
        let live = try? catalog.processObjectIDs()
        for object in Array(processes.keys) { stopProcess(object, exited: live?.contains(object) == false) }
        if let interrupted = microphoneInterruptedAt {
            recordInterruption(since: interrupted, reason: "Microphone interrupted")
        }
        for retry in retries.values { endInterruption(retry, reason: "Process audio interrupted") }
        let endedAt = captureDate()
        session?.captureEndedAt = endedAt
        session?.microphoneSegments.removeAll { !FileManager.default.fileExists(atPath: $0.url.path) }
        session?.processTracks.removeAll { !FileManager.default.fileExists(atPath: $0.url.path) }
        accessibilityWorker?.terminate(); accessibilityWorker = nil
        guard let finished = session else { throw CaptureError.writerFailure("No active recording") }
        defer {
            session = nil; sessionID = nil; claim = nil; fatalFailure = false
            microphoneRetry = nil; microphoneInterruptedAt = nil; microphoneDevice = nil
            retries.removeAll(); nextPoll = 0; nextHealth = 0
        }
        do { try finished.save() }
        catch { claimedRecordings.remove(finished.paths.manifest); throw error }
        CaptureDiagnostics.log("capture.stopped failed=\(finished.captureFailed) warnings=\(finished.warnings.count)")
        return finished
    }

    private func startMetadata(processID: Int32, name: String, bundleID: String?, paths: RecordingPaths, id: UUID) {
        do {
            guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
            accessibilityWorker = try backgroundWorker.startAccessibilityProbe(
                AccessibilityProbeRequest(processID: processID, bundleID: bundleID, applicationName: name, outputURL: paths.accessibilityTemporary)
            ) { [weak self] error in
                self?.queue.async { [weak self] in
                    guard let self, self.sessionID == id else { return }
                    self.warn("Optional metadata stopped: \(error.localizedDescription)")
                    self.accessibilityWorker = nil
                }
            }
        } catch { warn("Optional metadata unavailable: \(error.localizedDescription)") }
    }

    func recoverableFiles() async -> [URL] {
        // This block cannot throw.
        (try? await perform { self.store.interruptedRecordings(excluding: self.claimedRecordings) }) ?? []
    }

    func finalize(_ request: RecordingSession) async throws -> URL {
        do {
            guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
            let result = try await backgroundWorker.finalize(request)
            await release(request.paths.manifest)
            return result
        } catch { await release(request.paths.manifest); throw error }
    }

    func recoverInterruptedRecording(_ file: URL) async throws -> URL {
        let identity = try await perform {
            let identity = self.store.paths(forInterruptedFile: file).manifest
            guard self.claimedRecordings.insert(identity).inserted else {
                throw CaptureError.writerFailure("Recording is already being captured or processed")
            }
            return identity
        }
        do {
            guard let backgroundWorker else { throw BackgroundWorkerError.unavailable }
            let result = try await backgroundWorker.recover(RecoverCaptureRequest(interruptedFile: file))
            await release(identity)
            return result
        } catch { await release(identity); throw error }
    }

    private func release(_ identity: URL) async {
        _ = try? await perform { self.claimedRecordings.remove(identity) }
    }

    private func perform<T: Sendable>(_ operation: @escaping @Sendable () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do { continuation.resume(returning: try operation()) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }
}
