import AVFoundation
import CryptoKit
import Darwin
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
        systemSegments: [CapturedAudioSegment],
        captureStartedAt: Date,
        captureEndedAt: Date,
        temporaryDestination: URL,
        finalDestination: URL
    ) throws -> ArchiveResult {
        let sources: [(role: AudioTrack.Role, segments: [CapturedAudioSegment], channels: Int, bitrate: Int, title: String)] = try [
            (.microphone, microphoneSegments.filter { try containsAudio($0.url) }, 1, 96_000, "Microphone"),
            (.system, systemSegments.filter { try containsAudio($0.url) }, 2, 128_000, "System audio"),
        ].filter { !$0.segments.isEmpty }
        guard !sources.isEmpty else {
            throw CaptureError.writerFailure("no captured audio is available for finalization")
        }

        try? FileManager.default.removeItem(at: temporaryDestination)
        var arguments = ["-nostdin", "-v", "error", "-xerror", "-y"]
        for source in sources {
            for segment in source.segments { arguments += ["-i", segment.url.path] }
        }

        let duration = max(0, captureEndedAt.timeIntervalSince(captureStartedAt))
        // FFmpeg's duration options do not accept scientific notation.
        let durationArgument = String(format: "%.9f", locale: Locale(identifier: "en_US_POSIX"), duration)
        var filters: [String] = []
        var inputIndex = 0
        for source in sources {
            var labels: [String] = []
            for segment in source.segments {
                let delay = max(0, Int(segment.startedAt.timeIntervalSince(captureStartedAt) * 1_000))
                let label = "segment\(inputIndex)"
                let layout = source.channels == 1 ? "mono" : "stereo"
                filters.append("[\(inputIndex):a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=\(layout),adelay=\(delay):all=1[\(label)]")
                labels.append("[\(label)]")
                inputIndex += 1
            }
            filters.append("\(labels.joined())amix=inputs=\(labels.count):duration=longest:normalize=0,apad=whole_dur=\(durationArgument),atrim=duration=\(durationArgument),asetpts=PTS-STARTPTS[\(source.role.rawValue)]")
        }

        arguments += ["-filter_complex", filters.joined(separator: ";")]
        for (index, source) in sources.enumerated() {
            arguments += [
                "-map", "[\(source.role.rawValue)]",
                "-c:a:\(index)", "libopus",
                "-b:a:\(index)", "\(source.bitrate)",
                "-vbr:a:\(index)", "on",
                "-application:a:\(index)", "audio",
                "-frame_duration:a:\(index)", "20",
                "-compression_level:a:\(index)", "10",
                "-metadata:s:a:\(index)", "title=\(source.title)",
                "-disposition:a:\(index)", "0",
            ]
        }
        arguments += ["-map_metadata", "-1", "-f", "matroska", temporaryDestination.path]
        try runTool("ffmpeg", arguments: arguments)

        let probe = try probe(temporaryDestination)
        guard probe.streams.count == sources.count else {
            throw CaptureError.writerFailure("archive contains \(probe.streams.count) audio tracks; expected \(sources.count)")
        }
        for (index, expected) in sources.enumerated() {
            let stream = probe.streams[index]
            guard stream.index == index,
                  stream.codec_name == "opus",
                  Int(stream.sample_rate) == 48_000,
                  stream.channels == expected.channels else {
                throw CaptureError.writerFailure("archive track \(index) does not match the \(expected.role.rawValue) contract")
            }
            let expectedTitle = expected.title
            guard stream.tags?["title"] == expectedTitle else {
                throw CaptureError.writerFailure("archive track \(index) is missing its \(expectedTitle) identity")
            }
        }

        try runTool("ffmpeg", arguments: [
            "-nostdin", "-v", "error", "-xerror", "-i", temporaryDestination.path,
            "-map", "0:a", "-f", "null", "-",
        ])
        if FileManager.default.fileExists(atPath: finalDestination.path) {
            _ = try FileManager.default.replaceItemAt(finalDestination, withItemAt: temporaryDestination)
        } else {
            try FileManager.default.moveItem(at: temporaryDestination, to: finalDestination)
        }
        let published = try FileHandle(forWritingTo: finalDestination)
        defer { try? published.close() }
        try published.synchronize()
        let archiveDuration = Double(probe.format.duration ?? "") ?? duration
        let tracks = sources.enumerated().map { index, source in
            let stream = probe.streams[index]
            return AudioTrack(
                role: source.role,
                streamIndex: index,
                codec: stream.codec_name,
                sampleRate: Double(stream.sample_rate) ?? 48_000,
                channels: stream.channels,
                durationSeconds: Double(stream.duration ?? "") ?? archiveDuration,
                bitrate: source.bitrate
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

    static func containsAudio(_ url: URL) throws -> Bool {
        do {
            return try AVAudioFile(forReading: url).length > 0
        } catch {
            throw CaptureError.writerFailure("Cannot read \(url.lastPathComponent); original audio preserved: \(error.localizedDescription)")
        }
    }

    static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty {
            hasher.update(data: data)
        }
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
        let executable = try executable(named: name)
        let standardOutput = Pipe()
        guard let errorStream = tmpfile() else {
            throw CaptureError.writerFailure("Could not capture \(name) errors: \(String(cString: strerror(errno)))")
        }
        defer { fclose(errorStream) }
        let errorFile = FileHandle(fileDescriptor: fileno(errorStream), closeOnDealloc: false)
        func check(_ status: Int32) throws {
            guard status == 0 else {
                throw CaptureError.writerFailure("Could not launch \(name): \(String(cString: strerror(status)))")
            }
        }
        var actions: posix_spawn_file_actions_t? = nil
        try check(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }
        try check(posix_spawn_file_actions_adddup2(&actions, standardOutput.fileHandleForWriting.fileDescriptor, STDOUT_FILENO))
        try check(posix_spawn_file_actions_adddup2(&actions, errorFile.fileDescriptor, STDERR_FILENO))
        try check(posix_spawn_file_actions_addclose(&actions, standardOutput.fileHandleForReading.fileDescriptor))
        try check(posix_spawn_file_actions_addclose(&actions, standardOutput.fileHandleForWriting.fileDescriptor))
        try check(posix_spawn_file_actions_addclose(&actions, errorFile.fileDescriptor))
        let strings = ([executable.path] + arguments).map { strdup($0) }
        defer { strings.forEach { free($0) } }
        var argv = strings + [nil]
        let environment = ProcessInfo.processInfo.environment.map { strdup("\($0.key)=\($0.value)") }
        defer { environment.forEach { free($0) } }
        var envp = environment + [nil]
        var processID: pid_t = 0
        try check(posix_spawn(&processID, executable.path, &actions, nil, &argv, &envp))
        try standardOutput.fileHandleForWriting.close()
        let output = standardOutput.fileHandleForReading.readDataToEndOfFile()
        var status: Int32 = 0
        while waitpid(processID, &status, 0) < 0 {
            if errno != EINTR { throw CaptureError.writerFailure("Could not wait for \(name)") }
        }
        try errorFile.seek(toOffset: 0)
        let error = errorFile.readDataToEndOfFile()
        guard status == 0 else {
            let message = String(data: error, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            throw CaptureError.writerFailure("\(name) failed: \(message?.isEmpty == false ? message! : "status \(status)")")
        }
        return captureStandardOutput ? output : Data()
    }

    private static func executable(named name: String) throws -> URL {
        if let bundled = Bundle.main.url(forResource: name, withExtension: nil, subdirectory: "bin"),
           FileManager.default.isExecutableFile(atPath: bundled.path) {
            return bundled
        }
        if let executable = Bundle.main.executableURL {
            let appBundled = executable.deletingLastPathComponent().deletingLastPathComponent()
                .appending(path: "Resources/bin/\(name)")
            if FileManager.default.isExecutableFile(atPath: appBundled.path) { return appBundled }
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
