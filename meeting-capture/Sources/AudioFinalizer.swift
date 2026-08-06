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
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 32_768) else {
            throw CaptureError.writerFailure("cannot allocate conversion buffer")
        }

        // AVAudioFile finishes the FLAC header and metadata when it is released.
        // Release it explicitly before hashing so the manifest describes the
        // bytes that remain on disk, rather than the still-open file.
        var output: AVAudioFile? = try AVAudioFile(forWriting: destination, settings: settings)
        while input.framePosition < input.length {
            buffer.frameLength = 0
            try input.read(into: buffer)
            if buffer.frameLength == 0 { break }
            try output?.write(from: buffer)
        }
        output = nil

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
