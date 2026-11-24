@preconcurrency import AVFoundation
import CoreMedia
import Foundation
import Synchronization

struct CaptureStatus: Sendable {
    let sessionID: UUID
    let startedAt: Date
    let microphoneName: String
    let microphoneLevel: Float
    let systemAudioLevel: Float
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
    private let systemAudio = SystemAudioRecorder()
    private let store = MeetingStore()
    private let backgroundWorker = BackgroundWorkerClient()
    private var session: RecordingSession?
    private var sessionID: UUID?
    private var claim: RecordingClaim?
    private var claimedRecordings = Set<URL>()
    private var hostStart: CMTime = .zero
    private var timer: DispatchSourceTimer?
    private var nextHealth: TimeInterval = 0
    private let stopRequested = Atomic<Bool>(false)
    private var fatalFailure = false
    private var microphoneGeneration = UUID()
    private var microphoneDevice: AudioInputDevice?
    private var microphoneStarted: TimeInterval = 0
    private var microphoneRetry: TimeInterval?
    private var systemGeneration = UUID()
    private var systemRetry: TimeInterval?

    func start(id: UUID, trigger: CaptureTrigger, microphoneDevice: AudioInputDevice?) async throws {
        try await perform { [self] in
            guard self.session == nil else { throw CaptureError.writerFailure("Recording is already active") }
            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                throw CaptureError.microphonePermissionRequired
            }
            self.stopRequested.store(false, ordering: .releasing)
            self.hostStart = CMClockGetTime(CMClockGetHostTimeClock())
            let start = Date()
            let paths = try self.store.paths(startedAt: start, title: trigger.applicationName)
            self.claim = try RecordingClaim(paths)
            self.claimedRecordings.insert(paths.manifest)
            self.sessionID = id
            self.session = RecordingSession(microphoneSegments: [], systemSegments: [],
                captureStartedAt: start, captureEndedAt: nil, paths: paths, trigger: trigger,
                microphoneChanges: [], warnings: [])
            self.microphoneDevice = microphoneDevice
            CaptureDiagnostics.log("capture.start session=\(paths.session.lastPathComponent) pid=\(trigger.processID ?? 0) \(CaptureDiagnostics.devices())")
            do {
                try self.session?.save()
                self.startMicrophone()
                self.startSystemAudio()
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

    func stop() async throws -> RecordingSession {
        let time = CMClockGetTime(CMClockGetHostTimeClock())
        stopRequested.store(true, ordering: .releasing)
        return try await perform { try self.stopCapture(at: time) }
    }

    func updateMicrophoneDevices(_ devices: [AudioInputDevice]) {
        queue.async {
            let next = devices.first
            guard self.session != nil, !self.stopRequested.load(ordering: .acquiring), next != self.microphoneDevice else { return }
            self.microphoneFailed(CaptureError.deviceSelectionFailure("Input changed to \(next?.name ?? "system default")"))
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
        guard session != nil, !fatalFailure, !stopRequested.load(ordering: .acquiring) else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let retry = microphoneRetry, now >= retry { startMicrophone() }
        if microphoneRetry == nil,
           !microphone.isRunning || (microphone.callbackAge ?? (now - microphoneStarted)) > 5 {
            microphoneFailed(CaptureError.deviceSelectionFailure("Microphone stopped delivering audio"))
        }
        if let retry = systemRetry, now >= retry { startSystemAudio() }
        if let warning = microphone.captureWarning { warn("Microphone: \(warning)", incomplete: true) }
        if let warning = systemAudio.captureWarning { warn("System audio: \(warning)", incomplete: true) }
        if now >= nextHealth {
            nextHealth = now + 5
            CaptureDiagnostics.log("capture.health \(CaptureDiagnostics.devices())")
            CaptureDiagnostics.log("microphone.health \(microphone.diagnosticSummary)")
            CaptureDiagnostics.log("system.health \(systemAudio.diagnosticSummary)")
        }
        publish()
    }

    private func startMicrophone() {
        guard let session, !fatalFailure, !stopRequested.load(ordering: .acquiring) else { return }
        let host = CMClockGetTime(CMClockGetHostTimeClock())
        let date = captureDate(at: host)
        let segment = CapturedAudioSegment(url: session.paths.microphoneTemporary(segment: session.microphoneSegments.count + 1),
                                           startedAt: date)
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
            try microphone.start(to: segment.url, deviceID: microphoneDevice?.id, startTime: host)
            microphoneStarted = ProcessInfo.processInfo.systemUptime
            microphoneRetry = nil
            if let device = microphoneDevice {
                self.session?.microphoneChanges.append(MicrophoneChange(timestamp: date, device: device.manifestValue))
            }
            save()
        } catch { microphoneFailed(error) }
    }

    private func microphoneFailed(_ error: Error) {
        guard session != nil, !fatalFailure, !stopRequested.load(ordering: .acquiring) else { return }
        microphoneGeneration = UUID() // Ignore errors already queued by the old segment.
        microphone.stop()
        if let last = session?.microphoneSegments.last, !FileManager.default.fileExists(atPath: last.url.path) {
            session?.microphoneSegments.removeLast()
        }
        // Draining can report an overflow just before a replacement clears it.
        if let warning = microphone.captureWarning { warn("Microphone: \(warning)", incomplete: true) }
        microphoneRetry = ProcessInfo.processInfo.systemUptime + 10
        warn("Microphone interrupted; retrying: \(microphone.failure?.localizedDescription ?? error.localizedDescription)", incomplete: true)
    }

    private func startSystemAudio() {
        guard let session, !fatalFailure, !stopRequested.load(ordering: .acquiring) else { return }
        let host = CMClockGetTime(CMClockGetHostTimeClock())
        let segment = CapturedAudioSegment(url: session.paths.systemTemporary(segment: session.systemSegments.count + 1),
                                           startedAt: captureDate(at: host))
        self.session?.systemSegments.append(segment)
        guard save() else { return }
        let generation = UUID()
        systemGeneration = generation
        systemAudio.onError = { [weak self] error in
            self?.queue.async { [weak self] in
                guard let self, self.systemGeneration == generation else { return }
                self.systemFailed(error)
            }
        }
        do {
            try systemAudio.start(to: segment.url, startTime: host)
            systemRetry = nil
        } catch { systemFailed(error) }
    }

    private func systemFailed(_ error: Error) {
        guard session != nil, !fatalFailure, !stopRequested.load(ordering: .acquiring) else { return }
        systemGeneration = UUID()
        var failure = error
        do { try systemAudio.stop() } catch { failure = error }
        if let last = session?.systemSegments.last, !FileManager.default.fileExists(atPath: last.url.path) {
            session?.systemSegments.removeLast()
        }
        if let warning = systemAudio.captureWarning { warn("System audio: \(warning)", incomplete: true) }
        systemRetry = ProcessInfo.processInfo.systemUptime + 10
        warn("System audio interrupted; retrying: \(failure.localizedDescription)", incomplete: true)
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
        let warning: String? = if microphoneRetry != nil {
            "Microphone unavailable; retrying."
        } else if systemRetry != nil {
            "System audio interrupted; retrying."
        } else if session.captureFailed {
            "Some audio was dropped or interrupted."
        } else { nil }
        onStatus?(CaptureStatus(sessionID: id, startedAt: session.captureStartedAt,
            microphoneName: microphoneDevice?.name ?? "System default microphone",
            microphoneLevel: microphone.level, systemAudioLevel: systemAudio.level, warning: warning))
    }

    private func stopCapture(at time: CMTime = CMClockGetTime(CMClockGetHostTimeClock())) throws -> RecordingSession {
        guard session != nil else { throw CaptureError.writerFailure("No active recording") }
        CaptureDiagnostics.log("capture.stop")
        let endedAt = captureDate(at: time)
        stopRequested.store(true, ordering: .releasing)
        timer?.cancel(); timer = nil
        // Invalidate queued errors without changing callbacks while they can still run.
        microphoneGeneration = UUID()
        systemGeneration = UUID()
        // Stop both native inputs before either writer can block on disk.
        microphone.stopInput()
        systemAudio.stopInput()
        session?.captureEndedAt = endedAt
        save() // Recovery must retain Stop even if a later drain is interrupted.
        microphone.stop()
        if let failure = microphone.failure { warn("Microphone: \(failure.localizedDescription)", incomplete: true) }
        if let warning = microphone.captureWarning { warn("Microphone: \(warning)", incomplete: true) }
        do { try systemAudio.stop() }
        catch { warn("System audio: \(error.localizedDescription)", incomplete: true) }
        if let warning = systemAudio.captureWarning { warn("System audio: \(warning)", incomplete: true) }
        session?.microphoneSegments.removeAll { !FileManager.default.fileExists(atPath: $0.url.path) }
        session?.systemSegments.removeAll { !FileManager.default.fileExists(atPath: $0.url.path) }
        guard let finished = session else { throw CaptureError.writerFailure("No active recording") }
        defer {
            session = nil; sessionID = nil; claim = nil; fatalFailure = false
            microphoneRetry = nil; microphoneDevice = nil
            systemRetry = nil; nextHealth = 0
        }
        do { try finished.save() }
        catch { claimedRecordings.remove(finished.paths.manifest); throw error }
        CaptureDiagnostics.log("capture.stopped failed=\(finished.captureFailed) warnings=\(finished.warnings.count)")
        return finished
    }

    func recoverableFiles() async -> [URL] {
        // This block cannot throw.
        (try? await perform { self.store.interruptedRecordings(excluding: self.claimedRecordings) }) ?? []
    }

    func finalize(_ request: RecordingSession) async throws -> URL {
        defer { queue.async { self.claimedRecordings.remove(request.paths.manifest) } }
        return try await backgroundWorker.finalize(request)
    }

    func recoverInterruptedRecording(_ file: URL) async throws -> URL {
        let identity = try await perform {
            let identity = RecordingPaths(interruptedFile: file).manifest
            guard self.claimedRecordings.insert(identity).inserted else {
                throw CaptureError.writerFailure("Recording is already being captured or processed")
            }
            return identity
        }
        defer { queue.async { self.claimedRecordings.remove(identity) } }
        return try await backgroundWorker.recover(RecoverCaptureRequest(interruptedFile: file))
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
