import AVFoundation
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private(set) var format: AVAudioFormat?
    var onLevel: (@Sendable (Float) -> Void)?

    func start(to url: URL) throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw CaptureError.noMicrophone
        }
        format = inputFormat
        file = try AVAudioFile(forWriting: url, settings: inputFormat.settings, commonFormat: .pcmFormatFloat32, interleaved: false)
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            do { try self.file?.write(from: buffer) } catch { return }
            self.onLevel?(Self.level(buffer))
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        file = nil
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
    case writerFailure(String)

    var errorDescription: String? {
        switch self {
        case .noMicrophone: "No usable microphone is available."
        case .noDisplay: "No display is available for system-audio capture."
        case .triggeringApplicationUnavailable: "The triggering application's audio is unavailable."
        case let .writerFailure(message): "Audio writer failed: \(message)"
        }
    }
}
