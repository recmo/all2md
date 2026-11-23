import Darwin
import Foundation
import AVFoundation
import ApplicationServices

enum WorkerOperations {
    static func checkPermissions(_ root: URL) -> WorkerPermissionReport {
        let documents: Bool
        do {
            try MeetingStore(root: root).validateAccess()
            documents = true
        } catch { documents = false }
        return WorkerPermissionReport(documents: documents, accessibility: AXIsProcessTrusted())
    }

    static func checkAccess(_ request: WorkerAccessRequest) throws -> WorkerAccessReport {
        // Check the worker's access to app-created audio and the meeting folder.
        // A successful parent-process write does not verify this execution path.
        _ = try FileManager.default.contentsOfDirectory(at: request.root, includingPropertiesForKeys: nil)
        let directory = request.root.appending(path: ".worker-check-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let input = try AVAudioFile(forReading: request.microphone)
        guard input.length > 0, input.processingFormat.sampleRate > 0 else {
            throw CaptureError.writerFailure("Worker could not read the startup recording")
        }
        let start = Date()
        let end = start.addingTimeInterval(Double(input.length) / input.processingFormat.sampleRate)
        let archive = try AudioFinalizer.createArchive(
            microphoneSegments: [CapturedAudioSegment(url: request.microphone, startedAt: start, endedAt: end)],
            processTracks: [],
            captureStartedAt: start, captureEndedAt: end,
            temporaryDestination: directory.appending(path: "test.part.mka"),
            finalDestination: directory.appending(path: "test.mka")
        )
        guard archive.tracks.count == 1 else { throw CaptureError.writerFailure("Worker archive check failed") }
        try FileManager.default.removeItem(at: directory)
        return WorkerAccessReport(accessibility: "Accessibility: not checked.")
    }

    static func checkMetadataAccess(_ request: WorkerAccessRequest) -> WorkerAccessReport {
        WorkerAccessReport(accessibility: checkAccessibility(processID: request.applicationProcessID))
    }

    private static func checkAccessibility(processID: Int32) -> String {
        guard AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": false] as CFDictionary) else {
            return "Accessibility: not granted to the metadata worker (optional)."
        }
        let application = AXUIElementCreateApplication(processID)
        AXUIElementSetMessagingTimeout(application, 0.2)
        var role: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(application, kAXRoleAttribute as CFString, &role)
        guard result == .success, role != nil else {
            return "Accessibility: authorized, but reading the app could not be verified (\(result.rawValue))."
        }
        return "Accessibility: metadata worker read check passed."
    }

    static func finalize(_ request: RecordingSession) throws -> URL {
        let claim = try RecordingClaim(request.paths)
        defer { withExtendedLifetime(claim) {} }
        return try finish(request)
    }

    private static func finish(_ request: RecordingSession) throws -> URL {
        if let committed = try publishedRecording(request.paths) { return committed }
        WorkerProgress.touch()
        let accessibility = finalizeAccessibilityArtifact(paths: request.paths)
        var metadata = request.metadata
        if let title = accessibilityWindowTitle(request.paths.accessibilityFinal) {
            metadata.removeAll { $0.kind == .windowTitle }
            metadata.append(MetadataEvent(
                timestamp: request.captureStartedAt,
                kind: .windowTitle,
                value: title,
                confidence: 0.7
            ))
        }
        let archive = try AudioFinalizer.createArchive(
            microphoneSegments: request.microphoneSegments,
            processTracks: request.processTracks,
            captureStartedAt: request.captureStartedAt,
            captureEndedAt: request.captureEndedAt,
            temporaryDestination: request.paths.archiveTemporary,
            finalDestination: request.paths.archiveFinal
        )
        let status: CaptureManifest.Status = !request.captureFailed && archive.tracks.contains(where: { $0.role == .microphone })
            ? .complete : .incomplete
        let manifest = CaptureManifest(
            schemaVersion: 3,
            meetingID: UUID(),
            slug: String(request.paths.baseName.dropFirst(min(11, request.paths.baseName.count))),
            title: metadata.first(where: { $0.kind == .windowTitle })?.value,
            platform: request.trigger.applicationName,
            startedAt: request.captureStartedAt,
            endedAt: request.captureEndedAt,
            timeZone: TimeZone.current.identifier,
            trigger: request.trigger,
            container: archive.container,
            accessibility: accessibility,
            audio: archive.tracks,
            interruptions: request.interruptions,
            metadataEvents: metadata,
            warnings: request.warnings,
            status: status
        )
        try WorkerTools.checkCancellation()
        try MeetingStore().write(manifest, to: request.paths.manifest)
        try cleanup(request.paths)
        return request.paths.manifest
    }

    static func recover(_ request: RecoverCaptureRequest) throws -> URL {
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
        guard session.paths.directory.resolvingSymlinksInPath() == paths.directory.resolvingSymlinksInPath(),
              session.paths.baseName == paths.baseName,
              session.microphoneSegments.allSatisfy({
                  $0.url.deletingLastPathComponent().resolvingSymlinksInPath() == paths.directory.resolvingSymlinksInPath()
                      && $0.url.lastPathComponent.hasPrefix(".\(paths.baseName)-microphone-")
                      && $0.url.lastPathComponent.hasSuffix(".part.caf")
              }), session.processTracks.allSatisfy({
                  $0.url.resolvingSymlinksInPath() == paths.processTemporary($0.id).resolvingSymlinksInPath()
              }),
              Set(session.processTracks.map(\.id)).count == session.processTracks.count else {
            throw CaptureError.writerFailure("Invalid recording session paths; original files were kept")
        }
        var segments: [CapturedAudioSegment] = []
        for segment in session.microphoneSegments {
            guard FileManager.default.fileExists(atPath: segment.url.path) else {
                session.warnings.append("Missing microphone segment: \(segment.url.lastPathComponent)")
                continue
            }
            let input = try AVAudioFile(forReading: segment.url)
            let end = segment.startedAt.addingTimeInterval(Double(max(0, input.length)) / input.processingFormat.sampleRate)
            segments.append(CapturedAudioSegment(url: segment.url, startedAt: segment.startedAt, endedAt: end))
            session.captureEndedAt = max(session.captureEndedAt, end)
        }
        session.microphoneSegments = segments
        var tracks: [CapturedProcessTrack] = []
        var hasProcessAudio = false
        for track in session.processTracks {
            guard FileManager.default.fileExists(atPath: track.url.path) else {
                session.warnings.append("Missing process track: \(track.url.lastPathComponent)")
                continue
            }
            let input = try AVAudioFile(forReading: track.url)
            if input.length > 0 {
                hasProcessAudio = true
                let end = track.startedAt.addingTimeInterval(Double(input.length) / input.processingFormat.sampleRate)
                session.captureEndedAt = max(session.captureEndedAt, end)
            }
            tracks.append(track)
        }
        session.processTracks = tracks
        guard segments.contains(where: { $0.endedAt > $0.startedAt }) || hasProcessAudio else {
            let files = try FileManager.default.contentsOfDirectory(at: paths.directory, includingPropertiesForKeys: nil)
                .filter { store.paths(forInterruptedFile: $0).baseName == paths.baseName && $0.path.hasSuffix(".part.caf") }
            quarantineEmptyRecoveryFiles(files, archiveTemporary: paths.archiveTemporary)
            quarantine(paths.session, replacing: ".session.json", with: ".unrecoverable.json")
            throw CaptureError.writerFailure("No audio frames were saved; empty files were marked unrecoverable")
        }
        session.captureFailed = true
        session.warnings.append("Recovered after an interrupted recording; saved segment timing was preserved.")
        try session.save()
        return try finish(session)
    }

    // The manifest is the commit record. Never rebuild a committed archive from
    // raw files that may already have been partially removed by cleanup.
    private static func publishedRecording(_ paths: RecordingPaths) throws -> URL? {
        guard FileManager.default.fileExists(atPath: paths.manifest.path) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let manifest = try decoder.decode(CaptureManifest.self, from: Data(contentsOf: paths.manifest))
        guard manifest.container.file == paths.archiveFinal.lastPathComponent,
              try AudioFinalizer.sha256(paths.archiveFinal) == manifest.container.sha256 else {
            throw CaptureError.writerFailure("Published archive verification failed; all remaining files were kept")
        }
        if let metadata = manifest.accessibility {
            guard metadata.file == paths.accessibilityFinal.lastPathComponent,
                  try AudioFinalizer.sha256(paths.accessibilityFinal) == metadata.sha256 else {
                throw CaptureError.writerFailure("Published metadata verification failed; all remaining files were kept")
            }
        }
        try cleanup(paths)
        return paths.manifest
    }

    private static func cleanup(_ paths: RecordingPaths) throws {
        let store = MeetingStore()
        let files = try FileManager.default.contentsOfDirectory(at: paths.directory, includingPropertiesForKeys: nil)
        for file in files where file.path.hasSuffix(".part.caf") && store.paths(forInterruptedFile: file).baseName == paths.baseName {
            try WorkerTools.checkCancellation()
            try FileManager.default.removeItem(at: file)
        }
        for file in [paths.archiveTemporary, paths.session] where FileManager.default.fileExists(atPath: file.path) {
            try WorkerTools.checkCancellation()
            try FileManager.default.removeItem(at: file)
        }
    }

    private static func finalizeAccessibilityArtifact(paths: RecordingPaths) -> AccessibilityArtifact? {
        do {
            // A crashed recorder releases its recording claim before its probe
            // observes EOF. Wait for that writer to exit before publishing or
            // hashing the log. The probe's lifetime watcher runs off its AX queue.
            let lockURL = paths.accessibilityTemporary.appendingPathExtension("lock")
            let lock = open(lockURL.path, O_CREAT | O_RDWR | O_CLOEXEC, mode_t(0o600))
            guard lock >= 0 else { return nil }
            defer { close(lock) }
            let deadline = ProcessInfo.processInfo.systemUptime + 3
            while flock(lock, LOCK_EX | LOCK_NB) != 0 {
                guard errno == EWOULDBLOCK || errno == EINTR,
                      ProcessInfo.processInfo.systemUptime < deadline else { return nil }
                Thread.sleep(forTimeInterval: 0.05)
            }
            if FileManager.default.fileExists(atPath: paths.accessibilityTemporary.path) {
                if FileManager.default.fileExists(atPath: paths.accessibilityFinal.path) {
                    _ = try FileManager.default.replaceItemAt(paths.accessibilityFinal, withItemAt: paths.accessibilityTemporary)
                } else {
                    try FileManager.default.moveItem(at: paths.accessibilityTemporary, to: paths.accessibilityFinal)
                }
            }
            guard FileManager.default.fileExists(atPath: paths.accessibilityFinal.path) else { return nil }
            return AccessibilityArtifact(
                file: paths.accessibilityFinal.lastPathComponent,
                format: "accessibility-jsonl-v1",
                sha256: try AudioFinalizer.sha256(paths.accessibilityFinal)
            )
        } catch {
            return nil
        }
    }

    private static func accessibilityWindowTitle(_ url: URL) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let data = try? handle.read(upToCount: 1_048_576),
              let text = String(data: data, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n") {
            guard let data = line.data(using: .utf8),
                  let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  ["probeStarted", "windowTitle"].contains(record["type"] as? String ?? ""),
                  let title = record["windowTitle"] as? String, !title.isEmpty else { continue }
            return title
        }
        return nil
    }

    private static func quarantineEmptyRecoveryFiles(_ files: [URL], archiveTemporary: URL) {
        for file in files { quarantine(file, replacing: ".part.caf", with: ".unrecoverable.caf") }
        quarantine(archiveTemporary, replacing: ".part.mka", with: ".unrecoverable.mka")
    }

    private static func quarantine(_ file: URL, replacing suffix: String, with replacement: String) {
        guard FileManager.default.fileExists(atPath: file.path), file.path.hasSuffix(suffix) else { return }
        let base = String(file.path.dropLast(suffix.count))
        var destination = URL(fileURLWithPath: base + replacement)
        if FileManager.default.fileExists(atPath: destination.path) {
            destination = URL(fileURLWithPath: base + "-\(UUID().uuidString)" + replacement)
        }
        try? FileManager.default.moveItem(at: file, to: destination)
    }
}
