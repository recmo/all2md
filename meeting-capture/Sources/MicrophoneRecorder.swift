@preconcurrency import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private final class ConverterInput: @unchecked Sendable {
        private let buffer: AVAudioPCMBuffer
        private var supplied = false

        init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }

        func next(status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
            guard !supplied else {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
    }

    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private let writeLock = NSLock()
    private let recordingFormat = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
    private(set) var format: AVAudioFormat?
    private(set) var deviceID: AudioDeviceID?
    var onLevel: (@Sendable (Float) -> Void)?
    var onError: (@Sendable (Error) -> Void)?

    func start(to url: URL, deviceID: AudioDeviceID?) throws {
        file = try AVAudioFile(
            forWriting: url,
            settings: recordingFormat.settings,
            commonFormat: .pcmFormatFloat32,
            interleaved: false
        )
        do {
            try startEngine(deviceID: deviceID)
        } catch {
            file = nil
            throw error
        }
    }

    func switchDevice(to newDeviceID: AudioDeviceID) throws {
        guard newDeviceID != deviceID else { return }
        let previousDeviceID = deviceID
        stopEngine()
        do {
            try startEngine(deviceID: newDeviceID)
        } catch {
            if let previousDeviceID {
                try? startEngine(deviceID: previousDeviceID)
            }
            throw error
        }
    }

    func stop() {
        stopEngine()
        writeLock.lock()
        file = nil
        writeLock.unlock()
    }

    private func startEngine(deviceID: AudioDeviceID?) throws {
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
        guard let converter = AVAudioConverter(from: inputFormat, to: recordingFormat) else {
            throw CaptureError.deviceSelectionFailure("cannot convert \(inputFormat) to the recording format")
        }
        format = inputFormat
        self.converter = converter
        self.deviceID = deviceID
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            self.converter = nil
            self.deviceID = nil
            throw error
        }
    }

    private func stopEngine() {
        guard engine.isRunning || converter != nil else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        writeLock.lock()
        flushConverter()
        converter = nil
        writeLock.unlock()
        deviceID = nil
    }

    private func flushConverter() {
        guard let converter, let file,
              let outputBuffer = AVAudioPCMBuffer(pcmFormat: recordingFormat, frameCapacity: 4096) else { return }
        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError) { _, inputStatus in
            inputStatus.pointee = .endOfStream
            return nil
        }
        if status == .error {
            onError?(conversionError ?? CaptureError.writerFailure("could not flush microphone converter"))
        } else if outputBuffer.frameLength > 0 {
            do { try file.write(from: outputBuffer) }
            catch { onError?(error) }
        }
    }

    private func consume(_ inputBuffer: AVAudioPCMBuffer) {
        onLevel?(Self.level(inputBuffer))
        writeLock.lock()
        defer { writeLock.unlock() }
        guard let converter, let file else { return }

        let scale = recordingFormat.sampleRate / inputBuffer.format.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(inputBuffer.frameLength) * scale)) + 64
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: recordingFormat, frameCapacity: capacity) else { return }
        let converterInput = ConverterInput(inputBuffer)
        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError) { _, inputStatus in
            converterInput.next(status: inputStatus)
        }
        if status == .error {
            onError?(conversionError ?? CaptureError.writerFailure("microphone conversion failed"))
            return
        }
        guard outputBuffer.frameLength > 0 else { return }
        do {
            try file.write(from: outputBuffer)
        } catch {
            onError?(error)
        }
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
