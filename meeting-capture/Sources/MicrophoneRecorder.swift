@preconcurrency import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var currentSegmentURL: URL?
    private var currentSegmentStartedAt: Date?
    private var completedSegments: [CapturedAudioSegment] = []
    private let writeLock = NSLock()
    private(set) var format: AVAudioFormat?
    private(set) var deviceID: AudioDeviceID?
    var onLevel: (@Sendable (Float) -> Void)?
    var onError: (@Sendable (Error) -> Void)?

    func start(to url: URL, deviceID: AudioDeviceID?) throws {
        completedSegments = []
        try startEngine(deviceID: deviceID, to: url)
    }

    func switchDevice(
        to newDeviceID: AudioDeviceID,
        segmentURL: URL,
        rollbackURL: URL
    ) throws {
        guard newDeviceID != deviceID else { return }
        let previousDeviceID = deviceID
        stopEngine()
        do {
            try startEngine(deviceID: newDeviceID, to: segmentURL)
        } catch {
            try? FileManager.default.removeItem(at: segmentURL)
            if let previousDeviceID {
                try? startEngine(deviceID: previousDeviceID, to: rollbackURL)
            }
            throw error
        }
    }

    func stop() -> [CapturedAudioSegment] {
        stopEngine()
        let result = completedSegments
        completedSegments = []
        return result
    }

    private func startEngine(deviceID: AudioDeviceID?, to url: URL) throws {
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
        let file = try AVAudioFile(
            forWriting: url,
            settings: inputFormat.settings,
            commonFormat: .pcmFormatFloat32,
            interleaved: false
        )
        format = inputFormat
        self.file = file
        self.deviceID = deviceID
        currentSegmentURL = url
        currentSegmentStartedAt = Date()
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            self.file = nil
            self.deviceID = nil
            currentSegmentURL = nil
            currentSegmentStartedAt = nil
            try? FileManager.default.removeItem(at: url)
            throw error
        }
    }

    private func stopEngine() {
        guard engine.isRunning || file != nil else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        let endedAt = Date()
        writeLock.lock()
        file = nil
        writeLock.unlock()
        if let url = currentSegmentURL, let startedAt = currentSegmentStartedAt {
            completedSegments.append(CapturedAudioSegment(url: url, startedAt: startedAt, endedAt: endedAt))
        }
        currentSegmentURL = nil
        currentSegmentStartedAt = nil
        deviceID = nil
    }

    private func consume(_ buffer: AVAudioPCMBuffer) {
        onLevel?(Self.level(buffer))
        writeLock.lock()
        defer { writeLock.unlock() }
        do { try file?.write(from: buffer) }
        catch { onError?(error) }
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

enum CaptureError: LocalizedError {
    case noMicrophone
    case noDisplay
    case triggeringApplicationUnavailable
    case deviceSelectionFailure(String)
    case writerFailure(String)

    var errorDescription: String? {
        switch self {
        case .noMicrophone: "No usable microphone is available."
        case .noDisplay: "No display is available for system-audio capture."
        case .triggeringApplicationUnavailable: "The triggering application's audio is unavailable."
        case let .deviceSelectionFailure(message): "Could not select microphone: \(message)"
        case let .writerFailure(message): "Audio writer failed: \(message)"
        }
    }
}
