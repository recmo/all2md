import AVFoundation
import CryptoKit
import Foundation

struct ArchiveResult: Sendable {
    let container: AudioContainer
    let tracks: [AudioTrack]
}

enum AudioFinalizer {
    private struct ProbedStream: Decodable {
        let index: Int
        let codec_name: String
        let sample_rate: String
        let channels: Int
        let duration: String?
        let tags: [String: String]?
    }

    private struct ProbedFormat: Decodable { let duration: String? }
    private struct ProbeResult: Decodable {
        let streams: [ProbedStream]
        let format: ProbedFormat
    }

    static func createArchive(
        microphoneSegments: [CapturedAudioSegment],
        participants: URL?,
        participantsStartedAt: Date?,
        captureStartedAt: Date,
        captureEndedAt: Date,
        temporaryDestination: URL,
        finalDestination: URL
    ) throws -> ArchiveResult {
        let microphoneSegments = microphoneSegments.filter { FileManager.default.fileExists(atPath: $0.url.path) }
        let participants = participants.flatMap { FileManager.default.fileExists(atPath: $0.path) ? $0 : nil }
        guard !microphoneSegments.isEmpty || participants != nil else {
            throw CaptureError.writerFailure("no captured audio is available for finalization")
        }

        try? FileManager.default.removeItem(at: temporaryDestination)
        var arguments = ["-nostdin", "-v", "error", "-y"]
        for segment in microphoneSegments { arguments += ["-i", segment.url.path] }
        if let participants { arguments += ["-i", participants.path] }

        let duration = max(0, captureEndedAt.timeIntervalSince(captureStartedAt))
        var filters: [String] = []
        var maps: [(label: String, role: AudioTrack.Role, bitrate: Int, channels: Int)] = []
        if !microphoneSegments.isEmpty {
            var labels: [String] = []
            for (index, segment) in microphoneSegments.enumerated() {
                let delay = max(0, Int(segment.startedAt.timeIntervalSince(captureStartedAt) * 1_000))
                let label = "microphone\(index)"
                filters.append("[\(index):a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=mono,adelay=\(delay):all=1[\(label)]")
                labels.append("[\(label)]")
            }
            filters.append("\(labels.joined())amix=inputs=\(labels.count):duration=longest:normalize=0,atrim=duration=\(duration),asetpts=PTS-STARTPTS[microphone]")
            maps.append(("microphone", .microphone, 96_000, 1))
        }
        if participants != nil {
            let inputIndex = microphoneSegments.count
            let delay = max(0, Int((participantsStartedAt ?? captureStartedAt).timeIntervalSince(captureStartedAt) * 1_000))
            filters.append("[\(inputIndex):a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=\(delay):all=1,atrim=duration=\(duration),asetpts=PTS-STARTPTS[participants]")
            maps.append(("participants", .participants, 128_000, 2))
        }

        arguments += ["-filter_complex", filters.joined(separator: ";")]
        for (index, map) in maps.enumerated() {
            arguments += [
                "-map", "[\(map.label)]",
                "-c:a:\(index)", "libopus",
                "-b:a:\(index)", "\(map.bitrate)",
                "-vbr:a:\(index)", "on",
                "-application:a:\(index)", "audio",
                "-frame_duration:a:\(index)", "20",
                "-compression_level:a:\(index)", "10",
                "-metadata:s:a:\(index)", "title=\(map.role == .microphone ? "Microphone" : "Participants")",
                "-disposition:a:\(index)", "0",
            ]
        }
        arguments += ["-map_metadata", "-1", "-f", "matroska", temporaryDestination.path]
        try runTool("ffmpeg", arguments: arguments)

        let probe = try probe(temporaryDestination)
        guard probe.streams.count == maps.count else {
            throw CaptureError.writerFailure("archive contains \(probe.streams.count) audio tracks; expected \(maps.count)")
        }
        for (index, expected) in maps.enumerated() {
            let stream = probe.streams[index]
            guard stream.index == index,
                  stream.codec_name == "opus",
                  Int(stream.sample_rate) == 48_000,
                  stream.channels == expected.channels else {
                throw CaptureError.writerFailure("archive track \(index) does not match the \(expected.role.rawValue) contract")
            }
            let expectedTitle = expected.role == .microphone ? "Microphone" : "Participants"
            guard stream.tags?["title"] == expectedTitle else {
                throw CaptureError.writerFailure("archive track \(index) is missing its \(expectedTitle) identity")
            }
            try runTool("ffmpeg", arguments: [
                "-nostdin", "-v", "error", "-i", temporaryDestination.path,
                "-map", "0:a:\(index)", "-f", "null", "-",
            ])
        }

        if FileManager.default.fileExists(atPath: finalDestination.path) {
            _ = try FileManager.default.replaceItemAt(finalDestination, withItemAt: temporaryDestination)
        } else {
            try FileManager.default.moveItem(at: temporaryDestination, to: finalDestination)
        }
        let archiveDuration = Double(probe.format.duration ?? "") ?? duration
        let tracks = maps.enumerated().map { index, map in
            let stream = probe.streams[index]
            return AudioTrack(
                role: map.role,
                streamIndex: index,
                codec: stream.codec_name,
                sampleRate: Double(stream.sample_rate) ?? 48_000,
                channels: stream.channels,
                durationSeconds: Double(stream.duration ?? "") ?? archiveDuration,
                bitrate: map.bitrate
            )
        }
        return ArchiveResult(
            container: AudioContainer(
                file: finalDestination.lastPathComponent,
                format: "matroska",
                sha256: try sha256(finalDestination)
            ),
            tracks: tracks
        )
    }

    static func recoveredSegments(from urls: [URL], startedAt: Date) throws -> [CapturedAudioSegment] {
        var cursor = startedAt
        return try urls.sorted { $0.lastPathComponent < $1.lastPathComponent }.map { url in
            let input = try AVAudioFile(forReading: url)
            let duration = input.processingFormat.sampleRate > 0
                ? Double(input.length) / input.processingFormat.sampleRate
                : 0
            let segment = CapturedAudioSegment(url: url, startedAt: cursor, endedAt: cursor.addingTimeInterval(duration))
            cursor = segment.endedAt
            return segment
        }
    }

    static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty { hasher.update(data: data) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func probe(_ url: URL) throws -> ProbeResult {
        let data = try runTool("ffprobe", arguments: [
            "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index,codec_name,sample_rate,channels,duration:stream_tags=title:format=duration",
            "-of", "json", url.path,
        ], captureStandardOutput: true)
        return try JSONDecoder().decode(ProbeResult.self, from: data)
    }

    @discardableResult
    private static func runTool(
        _ name: String,
        arguments: [String],
        captureStandardOutput: Bool = false
    ) throws -> Data {
        let process = Process()
        process.executableURL = try executable(named: name)
        process.arguments = arguments
        let standardOutput = Pipe()
        let standardError = Pipe()
        process.standardOutput = standardOutput
        process.standardError = standardError
        try process.run()
        let output = standardOutput.fileHandleForReading.readDataToEndOfFile()
        let error = standardError.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let message = String(data: error, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            throw CaptureError.writerFailure("\(name) failed: \(message?.isEmpty == false ? message! : "exit \(process.terminationStatus)")")
        }
        return captureStandardOutput ? output : Data()
    }

    private static func executable(named name: String) throws -> URL {
        if let bundled = Bundle.main.url(forResource: name, withExtension: nil, subdirectory: "bin"),
           FileManager.default.isExecutableFile(atPath: bundled.path) {
            return bundled
        }
        var paths = ProcessInfo.processInfo.environment["PATH"]?.split(separator: ":").map(String.init) ?? []
        paths += [
            "/etc/profiles/per-user/\(NSUserName())/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        for directory in paths {
            let candidate = URL(fileURLWithPath: directory, isDirectory: true).appending(path: name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        throw CaptureError.writerFailure("\(name) is unavailable")
    }
}
