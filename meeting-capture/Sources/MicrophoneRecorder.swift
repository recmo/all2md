@preconcurrency import AVFoundation
import AudioToolbox
import CoreAudio
import CoreMedia
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private var file: PCMFile?
    private var currentSegmentURL: URL?
    private var currentSegmentStartedAt: Date?
    private var currentSegmentHostStart: CMTime?
    private var completedSegments: [CapturedAudioSegment] = []
    private let writeLock = NSLock()
    private var configurationObserver: NSObjectProtocol?
    private var writeFailure: Error?
    private(set) var format: AVAudioFormat?
    private(set) var deviceID: AudioDeviceID?
    var onLevel: (@Sendable (Float) -> Void)?
    var onError: (@Sendable (Error) -> Void)?
    var onConfigurationChange: (@Sendable () -> Void)?
    var isRunning: Bool { engine.isRunning }
    var failure: Error? {
        writeLock.lock()
        defer { writeLock.unlock() }
        return writeFailure
    }

    init() {
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in self?.onConfigurationChange?() }
    }

    deinit {
        if let configurationObserver { NotificationCenter.default.removeObserver(configurationObserver) }
    }

    func start(to url: URL, deviceID: AudioDeviceID?, startedAt: Date, startTime: CMTime) throws {
        completedSegments = []
        try startEngine(deviceID: deviceID, to: url, startedAt: startedAt, startTime: startTime)
    }

    func restart(to url: URL, deviceID: AudioDeviceID?, startedAt: Date, startTime: CMTime) throws {
        stopEngine()
        try startEngine(deviceID: deviceID, to: url, startedAt: startedAt, startTime: startTime)
    }

    func stop() -> [CapturedAudioSegment] {
        stopEngine()
        let result = completedSegments
        completedSegments = []
        return result
    }

    private func startEngine(deviceID: AudioDeviceID?, to url: URL, startedAt: Date, startTime: CMTime) throws {
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
        let file = PCMFile(url: url, startTime: startTime.seconds)
        format = inputFormat
        self.file = file
        writeFailure = nil
        self.deviceID = deviceID
        currentSegmentURL = url
        currentSegmentStartedAt = startedAt
        currentSegmentHostStart = startTime
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, time in
            self?.consume(buffer, time: time)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            stopEngine() // Preserve any frames delivered before startup failed.
            throw error
        }
    }

    private func stopEngine() {
        guard engine.isRunning || file != nil else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        let endedAt = CMClockGetTime(CMClockGetHostTimeClock())
        writeLock.lock()
        file?.close()
        file = nil
        writeLock.unlock()
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

    private func consume(_ buffer: AVAudioPCMBuffer, time: AVAudioTime) {
        onLevel?(Self.level(buffer))
        writeLock.lock()
        var failure: Error?
        if writeFailure == nil {
            do {
                guard time.isHostTimeValid else { throw CaptureError.writerFailure("Microphone buffer has no host timestamp") }
                try file?.write(buffer, at: AVAudioTime.seconds(forHostTime: time.hostTime))
            } catch { writeFailure = error; failure = error }
        }
        writeLock.unlock()
        if let failure { onError?(failure) }
    }

    private static func level(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData?[0] else { return 0 }
        let count = Int(buffer.frameLength)
        guard count > 0 else { return 0 }
        var sum: Float = 0
        for index in 0..<count { sum += data[index] * data[index] }
        return min(1, sqrt(sum / Float(count)) * 4)
    }
}
