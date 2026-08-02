import AVFoundation
import CryptoKit
import Foundation

enum AudioFinalizer {
    static func convertToFLAC(source: URL, destination: URL, role: AudioTrack.Role) throws -> AudioTrack {
        let input = try AVAudioFile(forReading: source)
        let format = input.processingFormat
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatFLAC,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: format.channelCount,
        ]
        let output = try AVAudioFile(forWriting: destination, settings: settings)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 32_768) else {
            throw CaptureError.writerFailure("cannot allocate conversion buffer")
        }
        while input.framePosition < input.length {
            buffer.frameLength = 0
            try input.read(into: buffer)
            if buffer.frameLength == 0 { break }
            try output.write(from: buffer)
        }
        let duration = format.sampleRate > 0 ? Double(input.length) / format.sampleRate : 0
        return AudioTrack(
            role: role,
            file: destination.lastPathComponent,
            format: "flac",
            sampleRate: format.sampleRate,
            channels: Int(format.channelCount),
            durationSeconds: duration,
            sha256: try sha256(destination)
        )
    }

    static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty { hasher.update(data: data) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
