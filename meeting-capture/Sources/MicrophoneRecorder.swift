@preconcurrency import AVFoundation
import AudioToolbox
import CoreAudio
import CoreMedia
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private var engine: AVAudioEngine?
    var diagnosticSummary: String {
        "running=\(isRunning) format=\(format?.description ?? "none") \(writer?.diagnosticSummary ?? "writer=none")"
    }
    private var writer: BufferedAudioWriter?
    private var warning: String?
    var captureWarning: String? { writeLock.withLock { warning } }
    var callbackAge: TimeInterval? { writer?.callbackAge }
    var level: Float { writer?.level ?? 0 }
    private var currentSegmentURL: URL?
    private var currentSegmentStartedAt: Date?
    private var currentSegmentHostStart: CMTime?
    private var completedSegments: [CapturedAudioSegment] = []
    private let writeLock = NSLock()
    private var configurationObserver: NSObjectProtocol?
    private var writeFailure: Error?
    private(set) var format: AVAudioFormat?
    private(set) var deviceID: AudioDeviceID?
    private var errorCallback: (@Sendable (Error) -> Void)?
    private var configurationCallback: (@Sendable () -> Void)?
    var onError: (@Sendable (Error) -> Void)? {
        get { writeLock.withLock { errorCallback } }
        set { writeLock.withLock { errorCallback = newValue } }
    }
    var onConfigurationChange: (@Sendable () -> Void)? {
        get { writeLock.withLock { configurationCallback } }
        set { writeLock.withLock { configurationCallback = newValue } }
    }
    var isRunning: Bool { engine?.isRunning == true }
    var failure: Error? {
        writeLock.lock()
        defer { writeLock.unlock() }
        return writeFailure
    }

    deinit {
        if let configurationObserver { NotificationCenter.default.removeObserver(configurationObserver) }
    }

    func start(to url: URL, deviceID: AudioDeviceID?, startedAt: Date, startTime: CMTime) throws {
        stopEngine()
        completedSegments = []
        try startEngine(deviceID: deviceID, to: url, startedAt: startedAt, startTime: startTime)
    }

    func restart(to url: URL, deviceID: AudioDeviceID?, startedAt: Date, startTime: CMTime) throws {
        stopEngine()
        try startEngine(deviceID: deviceID, to: url, startedAt: startedAt, startTime: startTime)
    }

    func pause() { stopEngine() }

    func stop() -> [CapturedAudioSegment] {
        stopEngine()
        let result = completedSegments
        completedSegments = []
        return result
    }

    private func startEngine(deviceID: AudioDeviceID?, to url: URL, startedAt: Date, startTime: CMTime) throws {
        CaptureDiagnostics.log("microphone.start segment=\(url.lastPathComponent) selected={\(CaptureDiagnostics.device(deviceID ?? AudioDeviceID(kAudioObjectUnknown)))} \(CaptureDiagnostics.devices())")
        // A disconnected device can leave its engine/input unit unusable. Each
        // segment resolves its device and format through a fresh engine.
        let engine = AVAudioEngine()
        self.engine = engine
        let input = engine.inputNode
        if let deviceID {
            guard let audioUnit = input.audioUnit else {
                throw CaptureError.deviceSelectionFailure("audio input unit is unavailable")
            }
            var selectedDeviceID = deviceID
            let status = AudioUnitSetProperty(
                audioUnit,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                0,
                &selectedDeviceID,
                UInt32(MemoryLayout<AudioDeviceID>.size)
            )
            guard status == noErr else {
                throw CaptureError.deviceSelectionFailure("Core Audio error \(status) selecting device \(deviceID)")
            }
        }

        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw CaptureError.noMicrophone
        }
        CaptureDiagnostics.log("microphone.format device=\(deviceID ?? 0) format=\(inputFormat)")
        let writer = try BufferedAudioWriter(format: inputFormat, url: url, startTime: startTime.seconds)
        writer.onError = { [weak self] error in
            guard let self else { return }
            self.writeLock.withLock { self.writeFailure = error }
            self.onError?(error)
        }
        writer.onWarning = { [weak self] message in
            guard let self else { return }
            self.writeLock.withLock { self.warning = message }
        }
        format = inputFormat
        self.writer = writer
        writeLock.withLock { writeFailure = nil; warning = nil }
        self.deviceID = deviceID
        currentSegmentURL = url
        currentSegmentStartedAt = startedAt
        currentSegmentHostStart = startTime
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in self?.onConfigurationChange?() }
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, time in
            var timestamp = AudioTimeStamp()
            timestamp.mHostTime = time.hostTime
            if time.isHostTimeValid { timestamp.mFlags = .hostTimeValid }
            writer.enqueue(buffer.audioBufferList, timestamp: timestamp)
        }
        engine.prepare()
        do {
            try engine.start()
            CaptureDiagnostics.log("microphone.started device=\(deviceID ?? 0) running=\(engine.isRunning)")
        } catch {
            CaptureDiagnostics.error("microphone.start.failed \(CaptureError.details(error))")
            stopEngine() // Preserve any frames delivered before startup failed.
            throw error
        }
    }

    private func stopEngine() {
        guard let engine else { return }
        defer { self.engine = nil }
        if let configurationObserver { NotificationCenter.default.removeObserver(configurationObserver) }
        configurationObserver = nil
        CaptureDiagnostics.log("microphone.stop device=\(deviceID ?? 0) \(diagnosticSummary)")
        engine.stop()
        if writer != nil { engine.inputNode.removeTap(onBus: 0) }
        let endedAt = CMClockGetTime(CMClockGetHostTimeClock())
        writer?.finish()
        writer = nil
        if let url = currentSegmentURL, let startedAt = currentSegmentStartedAt,
           let hostStart = currentSegmentHostStart {
            completedSegments.append(CapturedAudioSegment(url: url, startedAt: startedAt,
                endedAt: startedAt.addingTimeInterval((endedAt - hostStart).seconds)))
        }
        currentSegmentURL = nil
        currentSegmentStartedAt = nil
        currentSegmentHostStart = nil
        deviceID = nil
    }
}
