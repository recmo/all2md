import AVFoundation
import Darwin
import Foundation

@main
enum MeetingCaptureWorker {
    static func main() {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments == ["keep-running"] { try keepRunning(); return }
            guard arguments.count == 3 else { throw CaptureError.writerFailure("Invalid worker arguments") }
            let data = try Data(contentsOf: URL(fileURLWithPath: arguments[1]))
            let response: WorkerResponse
            do {
                let result: URL
                switch arguments[0] {
                case "finalize": result = try finalize(JSONDecoder().decode(RecordingSession.self, from: data))
                case "recover": result = try recover(JSONDecoder().decode(RecoverCaptureRequest.self, from: data))
                default: throw CaptureError.writerFailure("Unknown worker operation")
                }
                response = .success(result)
            } catch { response = .failure(error) }
            try JSONEncoder().encode(response).write(to: URL(fileURLWithPath: arguments[2]), options: .atomic)
        } catch {
            FileHandle.standardError.write(Data("MeetingCapture: \(error.localizedDescription)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }

    private static func keepRunning() throws {
        // Swift cannot import libproc's composed PROC_PIDPATHINFO_MAXSIZE macro.
        let capacity = 4 * Int(MAXPATHLEN)
        var path = [CChar](repeating: 0, count: capacity)
        guard proc_pidpath(getpid(), &path, UInt32(capacity)) > 0 else {
            throw CaptureError.writerFailure("Could not locate application")
        }
        let app = URL(fileURLWithFileSystemRepresentation: path, isDirectory: false, relativeTo: nil)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        // LaunchServices reuses an open instance. Become the waiting launcher
        // so launchd receives its exit directly, without another process waiter.
        let command: [String] = ["/usr/bin/open", "-g", "-W", app.path]
        let arguments = command.map { strdup($0) }
        defer { arguments.forEach { free($0) } }
        var argv = arguments + [nil]
        execv("/usr/bin/open", &argv)
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }

    private static func finalize(_ request: RecordingSession) throws -> URL {
        let claim = try RecordingClaim(request.paths)
        defer { withExtendedLifetime(claim) {} }
        return try finish(request)
    }

    private static func finish(_ request: RecordingSession) throws -> URL {
        if let committed = try publishedRecording(request.paths) { return committed }
        guard let endedAt = request.captureEndedAt, endedAt >= request.captureStartedAt else {
            throw CaptureError.writerFailure("Recording has no valid stop time; original audio was kept")
        }
        let archive = try AudioFinalizer.createArchive(
            microphoneSegments: request.microphoneSegments,
            systemSegments: request.systemSegments,
            captureStartedAt: request.captureStartedAt,
            captureEndedAt: endedAt,
            temporaryDestination: request.paths.archiveTemporary,
            finalDestination: request.paths.archiveFinal
        )
        let status: CaptureManifest.Status = !request.captureFailed && archive.tracks.contains(where: { $0.role == .microphone })
            ? .complete : .incomplete
        let manifest = CaptureManifest(
            schemaVersion: 3,
            meetingID: UUID(),
            slug: String(request.paths.baseName.dropFirst(min(11, request.paths.baseName.count))),
            title: request.trigger.applicationName,
            platform: request.trigger.applicationName,
            startedAt: request.captureStartedAt,
            endedAt: endedAt,
            timeZone: TimeZone.current.identifier,
            trigger: request.trigger,
            container: archive.container,
            audio: archive.tracks,
            microphoneChanges: request.microphoneChanges,
            warnings: request.warnings,
            status: status
        )
        try MeetingStore().write(manifest, to: request.paths.manifest)
        try cleanup(request.paths)
        return request.paths.manifest
    }

    private static func recover(_ request: RecoverCaptureRequest) throws -> URL {
        let store = MeetingStore()
        let paths = store.paths(forInterruptedFile: request.interruptedFile)
        let claim = try RecordingClaim(paths)
        defer { withExtendedLifetime(claim) {} }
        if let committed = try publishedRecording(paths) { return committed }
        guard FileManager.default.fileExists(atPath: paths.session.path) else {
            throw CaptureError.writerFailure("No timing journal exists for \(paths.baseName). Original audio was kept; use Show files for manual salvage.")
        }
        var session = try RecordingSession.load(from: paths.session)
        // A journal may only refer to this recording's own files.
        let directory = paths.directory.resolvingSymlinksInPath()
        let prefix = ".\(paths.baseName)-"
        func owns(_ segment: CapturedAudioSegment, sources: [String]) -> Bool {
            let url = segment.url
            return url.resolvingSymlinksInPath().deletingLastPathComponent() == directory
                && store.paths(forInterruptedFile: url).baseName == paths.baseName
                && sources.contains { url.lastPathComponent.hasPrefix(prefix + $0 + "-") }
        }
        guard session.paths.directory.resolvingSymlinksInPath() == directory,
              session.paths.baseName == paths.baseName,
              session.microphoneSegments.allSatisfy({ owns($0, sources: ["microphone"]) }),
              session.systemSegments.allSatisfy({ owns($0, sources: ["system", "process"]) }) else {
            throw CaptureError.writerFailure("Invalid recording session paths; original files were kept")
        }
        let stoppedAt = session.captureEndedAt
        guard stoppedAt.map({ $0 >= session.captureStartedAt }) ?? true else {
            throw CaptureError.writerFailure("Invalid recording stop time; original audio was kept")
        }
        session.microphoneSegments = session.microphoneSegments.filter { FileManager.default.fileExists(atPath: $0.url.path) }
        session.systemSegments = session.systemSegments.filter { FileManager.default.fileExists(atPath: $0.url.path) }
        let inputs = session.microphoneSegments.map { ($0.url, $0.startedAt) }
            + session.systemSegments.map { ($0.url, $0.startedAt) }
        var recoveredEnd = session.captureStartedAt
        for (url, start) in inputs {
            let input = try AVAudioFile(forReading: url)
            if input.length > 0 {
                recoveredEnd = max(recoveredEnd, start.addingTimeInterval(Double(input.length) / input.processingFormat.sampleRate))
            }
        }
        guard recoveredEnd > session.captureStartedAt else {
            throw CaptureError.writerFailure("No audio frames were saved; use Show files to inspect the recording")
        }
        session.captureEndedAt = stoppedAt ?? recoveredEnd
        session.captureFailed = true
        session.warnings.append("Recovered after an interrupted recording; saved segment timing was preserved.")
        try session.save()
        return try finish(session)
    }

    // The manifest is the commit record. Never rebuild a committed archive from
    // raw files that may already have been partially removed by cleanup.
    private static func publishedRecording(_ paths: RecordingPaths) throws -> URL? {
        guard FileManager.default.fileExists(atPath: paths.manifest.path) else { return nil }
        struct Publication: Decodable { let container: AudioContainer }
        let manifest = try JSONDecoder().decode(Publication.self, from: Data(contentsOf: paths.manifest))
        guard manifest.container.file == paths.archiveFinal.lastPathComponent,
              try AudioFinalizer.sha256(paths.archiveFinal) == manifest.container.sha256 else {
            throw CaptureError.writerFailure("Published archive verification failed; all remaining files were kept")
        }
        try cleanup(paths)
        return paths.manifest
    }

    private static func cleanup(_ paths: RecordingPaths) throws {
        let store = MeetingStore()
        let files = try FileManager.default.contentsOfDirectory(at: paths.directory, includingPropertiesForKeys: nil)
        for file in files where file.path.hasSuffix(".part.caf") && store.paths(forInterruptedFile: file).baseName == paths.baseName {
            try FileManager.default.removeItem(at: file)
        }
        for file in [paths.archiveTemporary, paths.session] where FileManager.default.fileExists(atPath: file.path) {
            try FileManager.default.removeItem(at: file)
        }
    }
}
