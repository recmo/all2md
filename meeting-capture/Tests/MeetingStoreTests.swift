import Foundation
import AVFoundation
import Darwin
import XCTest
@testable import MeetingCapture

final class MeetingStoreTests: XCTestCase {
    func testWorkerAccessCheckProcessesAppAudioAndCleansArtifacts() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let microphone = root.appending(path: "test.caf")
        try writeTone(to: microphone, sampleRate: 48_000, channels: 1, duration: 0.1)
        let original = try Data(contentsOf: microphone)
        let worker = try BackgroundWorkerClient()
        let report = try await worker.checkAccess(WorkerAccessRequest(
            root: root, microphone: microphone,
            applicationProcessID: ProcessInfo.processInfo.processIdentifier
        ))
        XCTAssertTrue(report.accessibility.hasPrefix("Accessibility:"))
        let metadata = try await worker.checkMetadataAccess(WorkerAccessRequest(
            root: root, microphone: microphone, applicationProcessID: ProcessInfo.processInfo.processIdentifier
        ))
        XCTAssertTrue(metadata.accessibility.hasPrefix("Accessibility:"))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["test.caf"])
        XCTAssertEqual(try Data(contentsOf: microphone), original)
        do {
            _ = try await worker.checkAccess(WorkerAccessRequest(root: microphone, microphone: microphone,
                                                               applicationProcessID: ProcessInfo.processInfo.processIdentifier))
            XCTFail("Worker must reject an unusable meeting directory")
        } catch {}
        XCTAssertEqual(try Data(contentsOf: microphone), original)
    }

    @MainActor
    func testStartupCheckGatesRecordingAndCanRetry() async throws {
        var attempts = 0
        let model = AppModel(checkCapture: {
            attempts += 1
            throw CaptureError.microphonePermissionRequired
        })
        XCTAssertEqual(model.state, .checkingPermissions)
        model.manualStart()
        XCTAssertEqual(model.state, .checkingPermissions)
        model.start()
        for _ in 0..<100 where model.state == .checkingPermissions { await Task.yield() }
        XCTAssertEqual(model.state, .permissionRequired)
        XCTAssertNil(model.startupReport)
        XCTAssertTrue(model.permissionMessage.contains("Microphone"))
        model.manualStart()
        XCTAssertEqual(model.state, .permissionRequired)
        model.checkPermissions()
        for _ in 0..<100 where model.state == .checkingPermissions { await Task.yield() }
        XCTAssertEqual(attempts, 2)
        XCTAssertEqual(model.state, .permissionRequired)
    }

    @MainActor
    func testStartupSuccessRequiresCompletedCheck() async throws {
        var finish: CheckedContinuation<StartupCaptureReport, Error>?
        let model = AppModel(checkCapture: {
            try await withCheckedThrowingContinuation { finish = $0 }
        })
        model.start()
        for _ in 0..<100 where finish == nil { await Task.yield() }
        XCTAssertEqual(model.state, .checkingPermissions)
        XCTAssertNil(model.startupReport)
        model.checkPermissions() // Must not start a duplicate probe.
        finish?.resume(returning: StartupCaptureReport(systemAudioObserved: false))
        for _ in 0..<100 where model.state == .checkingPermissions { await Task.yield() }
        XCTAssertEqual(model.state, .idle)
        XCTAssertEqual(model.startupReport, StartupCaptureReport(systemAudioObserved: false))
    }

    @MainActor
    func testPermissionChecksCannotInterruptCapture() {
        let client = AudioClient(audioObjectID: 0, processID: 1, bundleID: nil, applicationName: "Fixture", inputDevices: [])
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .recording(client)))
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .detecting(client, since: Date())))
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .countdown(client, remaining: 2)))
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .finalizing))
        XCTAssertTrue(AppModel.allowsPermissionCheck(in: .permissionRequired))
        XCTAssertTrue(AppModel.allowsPermissionCheck(in: .idle))
    }

    @MainActor
    func testStartupRejectsEmptyMicrophoneRecording() {
        XCTAssertThrowsError(try StartupCaptureCheck.validateMicrophoneFrames(0))
        XCTAssertNoThrow(try StartupCaptureCheck.validateMicrophoneFrames(4096))
    }

    @MainActor
    func testStartupStorageCheckCleansUpAndReportsUnwritableDestination() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try StartupCaptureCheck.validateStorage(root)
        XCTAssertTrue(try FileManager.default.contentsOfDirectory(atPath: root.path).isEmpty)
        let file = root.appending(path: "not-a-directory")
        try Data("keep".utf8).write(to: file)
        XCTAssertThrowsError(try StartupCaptureCheck.validateStorage(file))
        XCTAssertEqual(try Data(contentsOf: file), Data("keep".utf8))
    }

    @MainActor
    func testManualRecordingsDoNotAutoStopWithoutMicrophoneActivity() {
        XCTAssertFalse(AppModel.shouldAutoStop(method: .manual))
        XCTAssertTrue(AppModel.shouldAutoStop(method: .audioProcess))
        XCTAssertTrue(AppModel.shouldAutoStop(method: .deviceRunning))
    }

    func testClaimExcludesDiscoveryAndRejectsConcurrentWorker() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = MeetingStore(root: root)
        let paths = try store.paths(startedAt: Date(), title: "claim")
        let input = paths.microphoneTemporary(segment: 1)
        try writeTone(to: input, sampleRate: 48_000, channels: 1, duration: 0.1)
        let original = try Data(contentsOf: input)
        var claim: RecordingClaim? = try RecordingClaim(paths)
        XCTAssertTrue(store.interruptedRecordings().isEmpty)
        let worker = try BackgroundWorkerClient()
        do {
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: input))
            XCTFail("Recovery must reject a recording owned by capture or finalization")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("already being captured or processed"))
        }
        withExtendedLifetime(claim) {}
        claim = nil
        XCTAssertEqual(store.interruptedRecordings().map { $0.resolvingSymlinksInPath() }, [input.resolvingSymlinksInPath()])
        XCTAssertTrue(store.interruptedRecordings(excluding: [paths.manifest]).isEmpty)
        XCTAssertEqual(try Data(contentsOf: input), original)
        XCTAssertFalse(FileManager.default.fileExists(atPath: paths.archiveFinal.path))
        // Releasing the claim makes the same input recoverable by a real worker.
        let manifest = try await worker.recover(RecoverCaptureRequest(interruptedFile: input))
        XCTAssertEqual(manifest, paths.manifest)
    }

    func testWorkersPreserveUnreadableTracks() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = MeetingStore(root: root)
        let worker = try BackgroundWorkerClient()
        for corruptMicrophone in [false, true] {
            let start = Date()
            let paths = try store.paths(startedAt: start, title: "corrupt-\(corruptMicrophone)")
            let microphone = paths.microphoneTemporary(segment: 1)
            let participants = paths.participantsTemporary
            try writeTone(to: microphone, sampleRate: 48_000, channels: 1, duration: 0.1)
            try writeTone(to: participants, sampleRate: 48_000, channels: 2, duration: 0.1)
            try Data("damaged CAF with irreplaceable payload".utf8)
                .write(to: corruptMicrophone ? microphone : participants)
            let microphoneData = try Data(contentsOf: microphone)
            let participantData = try Data(contentsOf: participants)
            let request = FinalizeCaptureRequest(
                microphoneSegments: [CapturedAudioSegment(url: microphone, startedAt: start, endedAt: start.addingTimeInterval(0.1))],
                participants: participants, participantsStartedAt: start,
                captureStartedAt: start, captureEndedAt: start.addingTimeInterval(0.1),
                paths: paths, accessibilityWorkerProcessID: nil,
                trigger: CaptureTrigger(method: .manual, processID: nil, bundleID: nil, applicationName: nil),
                metadata: [], warnings: [], interruptions: []
            )
            do {
                _ = try await worker.finalize(request)
                XCTFail("Unreadable inputs must fail finalization")
            } catch {}
            do {
                _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: microphone))
                XCTFail("Unreadable inputs must fail recovery")
            } catch {}
            XCTAssertEqual(try Data(contentsOf: microphone), microphoneData)
            XCTAssertEqual(try Data(contentsOf: participants), participantData)
            XCTAssertFalse(FileManager.default.fileExists(atPath: paths.manifest.path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: paths.archiveFinal.path))
        }
    }

    func testSystemAudioWriterInputUsesSafePassthroughConstruction() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let writer = try AVAssetWriter(outputURL: root, fileType: .caf)
        let input = try SystemAudioRecorder.makeWriterInput()
        XCTAssertEqual(input.mediaType, .audio)
        XCTAssertTrue(writer.canAdd(input))
    }

    func testAccessibilityTreeDiffRecordsAddedRemovedAndChangedAttributes() {
        let before = [
            AccessibilitySnapshotNode(path: "0", attributes: ["AXRole": "AXApplication"]),
            AccessibilitySnapshotNode(path: "0/0", attributes: ["AXTitle": "Alice", "AXValue": "idle"]),
            AccessibilitySnapshotNode(path: "0/1", attributes: ["AXTitle": "Bob"]),
        ]
        let after = [
            AccessibilitySnapshotNode(path: "0", attributes: ["AXRole": "AXApplication"]),
            AccessibilitySnapshotNode(path: "0/0", attributes: ["AXTitle": "Alice", "AXValue": "speaking"]),
            AccessibilitySnapshotNode(path: "0/2", attributes: ["AXTitle": "Carol"]),
        ]

        let diff = AccessibilityProbe.difference(from: before, to: after)

        XCTAssertEqual(diff.added.map(\.path), ["0/2"])
        XCTAssertEqual(diff.removed, ["0/1"])
        XCTAssertEqual(diff.changed, [
            AccessibilityAttributeChange(path: "0/0", attribute: "AXValue", before: "idle", after: "speaking"),
        ])
    }

    func testSlugNormalizesTitle() {
        XCTAssertEqual(MeetingStore.slug("  Leadership Wéékly / Europe  "), "leadership-weekly-europe")
        XCTAssertEqual(MeetingStore.slug("🎙️"), "meeting")
    }

    func testPathsUseYearMonthAndAvoidCollisions() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let date = calendar.date(from: DateComponents(year: 2026, month: 8, day: 2))!
        let store = MeetingStore(root: root)

        let first = try store.paths(startedAt: date, title: "Leadership Weekly")
        XCTAssertEqual(first.directory.path, root.appending(path: "2026/08").path)
        XCTAssertEqual(first.baseName, "2026-08-02-leadership-weekly")
        try Data("{}".utf8).write(to: first.manifest)

        let second = try store.paths(startedAt: date, title: "Leadership Weekly")
        XCTAssertEqual(second.baseName, "2026-08-02-leadership-weekly-2")

        let interrupted = first.directory.appending(path: ".2026-08-02-leadership-weekly-microphone-0002.part.caf")
        XCTAssertEqual(store.paths(forInterruptedFile: interrupted).baseName, first.baseName)
    }

    func testManifestEncodingMatchesVersionTwoContract() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appending(path: "fixture.json")
        let now = Date(timeIntervalSince1970: 1_775_260_800)
        let manifest = CaptureManifest(
            schemaVersion: 2,
            meetingID: UUID(uuidString: "E83C18C0-CF42-4AC1-B493-00F3C144FB1E")!,
            slug: "leadership-weekly",
            title: "Leadership Weekly",
            platform: "Zoom",
            calendarEventID: nil,
            startedAt: now,
            endedAt: now.addingTimeInterval(60),
            timeZone: "Europe/Warsaw",
            trigger: CaptureTrigger(method: .audioProcess, processID: 42, bundleID: "us.zoom.xos", applicationName: "zoom.us"),
            container: AudioContainer(file: "leadership-weekly.mka", format: "matroska", sha256: String(repeating: "a", count: 64)),
            accessibility: AccessibilityArtifact(file: "leadership-weekly-accessibility.jsonl", format: "accessibility-jsonl-v1", sha256: String(repeating: "b", count: 64)),
            audio: [AudioTrack(role: .microphone, streamIndex: 0, codec: "opus", sampleRate: 48_000, channels: 1, durationSeconds: 60, bitrate: 96_000)],
            interruptions: [],
            metadataEvents: [
                MetadataEvent(
                    timestamp: now,
                    kind: .microphoneDevice,
                    value: "Studio Display Microphone [AppleUSBAudioEngine:fixture]",
                    confidence: 1
                ),
            ],
            warnings: [],
            status: .incomplete
        )

        try MeetingStore(root: root).write(manifest, to: url)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        XCTAssertEqual(object["schemaVersion"] as? Int, 2)
        XCTAssertEqual(object["slug"] as? String, "leadership-weekly")
        XCTAssertNotNil(object["trigger"])
        XCTAssertEqual((object["container"] as? [String: Any])?["file"] as? String, "leadership-weekly.mka")
        XCTAssertEqual((object["accessibility"] as? [String: Any])?["file"] as? String, "leadership-weekly-accessibility.jsonl")
        XCTAssertNotNil(object["audio"])
        let metadataEvents = try XCTUnwrap(object["metadataEvents"] as? [[String: Any]])
        XCTAssertEqual(metadataEvents.first?["kind"] as? String, "microphoneDevice")
        XCTAssertEqual(metadataEvents.first?["value"] as? String, "Studio Display Microphone [AppleUSBAudioEngine:fixture]")
    }

    func testAudioClientSummarizesInputDevicesAndStableManifestIdentity() {
        let device = AudioInputDevice(id: 17, uid: "fixture-device", name: "External Microphone")
        let client = AudioClient(
            audioObjectID: 4,
            processID: 42,
            bundleID: "us.zoom.xos",
            applicationName: "zoom.us",
            inputDevices: [device]
        )

        XCTAssertEqual(client.primaryInputDevice, device)
        XCTAssertEqual(client.inputDeviceSummary, "External Microphone")
        XCTAssertEqual(device.manifestValue, "External Microphone [fixture-device]")
    }

    func testFinalizationCreatesVerifiedTwoStreamOpusArchive() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let microphone1 = root.appending(path: ".fixture-microphone-0001.part.caf")
        let microphone2 = root.appending(path: ".fixture-microphone-0002.part.caf")
        let participants = root.appending(path: ".fixture-participants.part.caf")
        try writeTone(to: microphone1, sampleRate: 44_100, channels: 1, duration: 0.20)
        try writeTone(to: microphone2, sampleRate: 48_000, channels: 2, duration: 0.20)
        try writeTone(to: participants, sampleRate: 48_000, channels: 2, duration: 0.50)
        let startedAt = Date(timeIntervalSince1970: 1_775_260_800)
        let temporary = root.appending(path: ".fixture.part.mka")
        let final = root.appending(path: "fixture.mka")

        let result = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt, endedAt: startedAt.addingTimeInterval(0.20)),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30), endedAt: startedAt.addingTimeInterval(0.50)),
            ],
            participants: participants,
            participantsStartedAt: startedAt.addingTimeInterval(0.05),
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )

        XCTAssertEqual(result.container.file, "fixture.mka")
        XCTAssertEqual(result.container.format, "matroska")
        XCTAssertEqual(result.container.sha256.count, 64)
        XCTAssertEqual(result.tracks.map(\.role), [.microphone, .participants])
        XCTAssertEqual(result.tracks.map(\.streamIndex), [0, 1])
        XCTAssertEqual(result.tracks.map(\.codec), ["opus", "opus"])
        XCTAssertEqual(result.tracks.map(\.channels), [1, 2])
        XCTAssertEqual(result.tracks.map(\.bitrate), [96_000, 128_000])
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: microphone1.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: participants.path))

        let retried = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt, endedAt: startedAt.addingTimeInterval(0.20)),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30), endedAt: startedAt.addingTimeInterval(0.50)),
            ],
            participants: participants,
            participantsStartedAt: startedAt.addingTimeInterval(0.05),
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )
        XCTAssertEqual(retried.tracks.count, 2)
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
    }

    func testRecoveryRejectsHeaderOnlyAudio() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let empty = root.appending(path: ".fixture-microphone.part.caf")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        _ = try AVAudioFile(forWriting: empty, settings: format.settings)

        let segments = try AudioFinalizer.recoveredSegments(from: [empty], startedAt: Date())

        XCTAssertTrue(segments.isEmpty)
    }

    @MainActor
    func testMaintenanceCannotStartDuringCaptureCriticalStates() {
        let client = AudioClient(
            audioObjectID: 1,
            processID: 42,
            bundleID: "fixture.meeting",
            applicationName: "Fixture",
            inputDevices: []
        )
        XCTAssertTrue(AppModel.allowsMaintenance(in: .idle))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .detecting(client, since: Date())))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .countdown(client, remaining: 5)))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .recording(client)))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .finalizing))
    }

    func testWorkerSegfaultDoesNotCrashHost() async throws {
        let worker = try BackgroundWorkerClient()
        do {
            try await worker.verifyCrashIsolation()
            XCTFail("The crash-test worker unexpectedly succeeded")
        } catch let error as BackgroundWorkerError {
            guard case let .crashed(command, reason, status) = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
            XCTAssertEqual(command, "crash-test")
            XCTAssertEqual(reason, .uncaughtSignal)
            XCTAssertEqual(status, SIGSEGV)
        }
    }

    func testProcessResolverWalksFromBrowserHelperToOwningApplication() throws {
        let browser = CaptureApplicationIdentity(processID: 100, bundleID: "com.brave.Browser", applicationName: "Brave Browser", isUserApplication: true)
        let helper = CaptureApplicationIdentity(processID: 200, bundleID: "com.brave.Browser.helper", applicationName: "Brave Browser Helper", isUserApplication: false)
        let resolved = ProcessApplicationResolver.resolve(
            processID: helper.processID,
            bundleID: helper.bundleID,
            applications: [browser, helper],
            parentPID: { [200: 100][$0] }
        )
        XCTAssertEqual(resolved, browser)
    }

    func testSystemAudioFallsBackToResolvedBundleID() {
        let candidates = [
            CaptureApplicationIdentity(processID: 100, bundleID: "com.brave.Browser", applicationName: "Brave Browser", isUserApplication: true),
            CaptureApplicationIdentity(processID: 300, bundleID: "us.zoom.xos", applicationName: "zoom.us", isUserApplication: true),
        ]
        XCTAssertEqual(
            SystemAudioRecorder.matchingApplicationIndex(processID: 999, bundleID: "com.brave.Browser", candidates: candidates),
            0
        )
    }

    private func writeTone(to url: URL, sampleRate: Double, channels: AVAudioChannelCount, duration: Double) throws {
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: channels))
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        let frameCount = AVAudioFrameCount(sampleRate * duration)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount))
        buffer.frameLength = frameCount
        for channel in 0..<Int(channels) {
            for frame in 0..<Int(frameCount) {
                buffer.floatChannelData?[channel][frame] = sin(Float(frame) / 20) * 0.1
            }
        }
        try file.write(from: buffer)
    }
}
