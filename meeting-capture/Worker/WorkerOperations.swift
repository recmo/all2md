import Darwin
import Foundation

enum WorkerOperations {
    static func finalize(_ request: FinalizeCaptureRequest) throws -> WorkerManifestResult {
        let claim = try RecordingClaim(request.paths)
        defer { withExtendedLifetime(claim) {} }
        waitForAccessibilityWorker(request.accessibilityWorkerProcessID)
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
            participants: request.participants,
            participantsStartedAt: request.participantsStartedAt,
            captureStartedAt: request.captureStartedAt,
            captureEndedAt: request.captureEndedAt,
            temporaryDestination: request.paths.archiveTemporary,
            finalDestination: request.paths.archiveFinal
        )
        let status: CaptureManifest.Status = archive.tracks.contains(where: { $0.role == .microphone })
            && archive.tracks.contains(where: { $0.role == .participants }) ? .complete : .incomplete
        let manifest = CaptureManifest(
            schemaVersion: 2,
            meetingID: UUID(),
            slug: String(request.paths.baseName.dropFirst(min(11, request.paths.baseName.count))),
            title: metadata.first(where: { $0.kind == .windowTitle })?.value,
            platform: request.trigger.applicationName,
            calendarEventID: nil,
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
        try MeetingStore().write(manifest, to: request.paths.manifest)
        for segment in request.microphoneSegments { try? FileManager.default.removeItem(at: segment.url) }
        if let participants = request.participants { try? FileManager.default.removeItem(at: participants) }
        return WorkerManifestResult(manifest: request.paths.manifest)
    }

    static func recover(_ request: RecoverCaptureRequest) throws -> WorkerManifestResult {
        let store = MeetingStore()
        let paths = store.paths(forInterruptedFile: request.interruptedFile)
        let claim = try RecordingClaim(paths)
        defer { withExtendedLifetime(claim) {} }
        // This worker owns the claim, so enumerate its inputs directly.
        let files = try FileManager.default.contentsOfDirectory(at: paths.directory, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasSuffix(".part.caf") }
        guard files.contains(where: { $0.resolvingSymlinksInPath() == request.interruptedFile.resolvingSymlinksInPath() }) else {
            throw CaptureError.writerFailure("the selected interrupted recording is no longer available")
        }
        let relatedFiles = files.filter { store.paths(forInterruptedFile: $0).baseName == paths.baseName }
        let datedFiles = relatedFiles.compactMap { url -> (URL, Date, Date)? in
            guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) else { return nil }
            return (url, attributes[.creationDate] as? Date ?? Date(), attributes[.modificationDate] as? Date ?? Date())
        }
        let started = datedFiles.map { $0.1 }.min() ?? Date()
        let ended = max(started, datedFiles.map { $0.2 }.max() ?? Date())
        let microphoneURLs = relatedFiles.filter { $0.lastPathComponent.contains("-microphone") }
        let microphoneSegments = try AudioFinalizer.recoveredSegments(from: microphoneURLs, startedAt: started)
        let participantsURL = try relatedFiles.first { $0.lastPathComponent.hasSuffix("-participants.part.caf") }
            .flatMap { try AudioFinalizer.containsAudio($0) ? $0 : nil }
        guard !microphoneSegments.isEmpty || participantsURL != nil else {
            quarantineEmptyRecoveryFiles(relatedFiles, archiveTemporary: paths.archiveTemporary)
            throw CaptureError.writerFailure("the interrupted recording contains no audio frames; its empty files were quarantined")
        }
        let archive = try AudioFinalizer.createArchive(
            microphoneSegments: microphoneSegments,
            participants: participantsURL,
            participantsStartedAt: participantsURL == nil ? nil : started,
            captureStartedAt: started,
            captureEndedAt: ended,
            temporaryDestination: paths.archiveTemporary,
            finalDestination: paths.archiveFinal
        )
        let manifest = CaptureManifest(
            schemaVersion: 2,
            meetingID: UUID(),
            slug: String(paths.baseName.dropFirst(min(11, paths.baseName.count))),
            title: nil,
            platform: nil,
            calendarEventID: nil,
            startedAt: started,
            endedAt: ended,
            timeZone: TimeZone.current.identifier,
            trigger: CaptureTrigger(method: .deviceRunning, processID: nil, bundleID: nil, applicationName: nil),
            container: archive.container,
            accessibility: finalizeAccessibilityArtifact(paths: paths),
            audio: archive.tracks,
            interruptions: [CaptureTimeRange(startedAt: started, endedAt: ended, reason: "application interruption")],
            metadataEvents: [],
            warnings: ["Recovered from crash-safe temporary audio; capture metadata may be incomplete."],
            status: .incomplete
        )
        try store.write(manifest, to: paths.manifest)
        for file in relatedFiles { try? FileManager.default.removeItem(at: file) }
        return WorkerManifestResult(manifest: paths.manifest)
    }

    private static func finalizeAccessibilityArtifact(paths: RecordingPaths) -> AccessibilityArtifact? {
        do {
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
                  record["type"] as? String == "probeStarted" else { continue }
            return record["windowTitle"] as? String
        }
        return nil
    }

    private static func waitForAccessibilityWorker(_ processID: Int32?) {
        guard let processID, processID > 0 else { return }
        let deadline = Date().addingTimeInterval(2)
        while kill(processID, 0) == 0, Date() < deadline { usleep(20_000) }
        guard kill(processID, 0) == 0 else { return }
        _ = kill(processID, SIGKILL)
        let killDeadline = Date().addingTimeInterval(1)
        while kill(processID, 0) == 0, Date() < killDeadline { usleep(20_000) }
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
