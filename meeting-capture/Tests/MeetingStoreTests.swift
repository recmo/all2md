import Foundation
@preconcurrency import AVFoundation
import CoreMedia
import CoreAudio
import Darwin
import XCTest
@testable import MeetingCapture

final class MeetingStoreTests: XCTestCase {
    // Run explicitly on a Mac with audio permission. This exercises HAL delivery,
    // PCM timing, and repeated teardown without creating an application window.
    @MainActor
    func testWindowlessProcessTapRecordsTone() async throws {
        guard ProcessInfo.processInfo.environment["MEETING_CAPTURE_TEST_PROCESS_TAP"] == "1" else {
            throw XCTSkip("Set MEETING_CAPTURE_TEST_PROCESS_TAP=1 on macOS to exercise real audio capture.")
        }
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let tone = root.appending(path: "tone.caf")
        try writeTone(to: tone, sampleRate: 48_000, channels: 2, duration: 1)
        let player = try AVAudioPlayer(contentsOf: tone)
        player.numberOfLoops = -1
        XCTAssertTrue(player.play())
        defer { player.stop() }
        try await Task.sleep(for: .milliseconds(100))
        var pid = ProcessInfo.processInfo.processIdentifier
        var process = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                                                mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        XCTAssertEqual(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                                 UInt32(MemoryLayout<pid_t>.size), &pid, &size, &process), noErr)
        XCTAssertNotEqual(process, kAudioObjectUnknown)
        let recorder = ProcessAudioRecorder()
        for cycle in 0..<2 {
            let recorded = root.appending(path: "participants-\(cycle).caf")
            do {
                try recorder.start(processObject: process, to: recorded,
                                   startTime: CMClockGetTime(CMClockGetHostTimeClock()))
                try await Task.sleep(for: .milliseconds(500))
                try recorder.stop()
            } catch {
                try? recorder.stop()
                throw error
            }
            XCTAssertNotNil(recorder.firstSampleAt)
            let file = try AVAudioFile(forReading: recorded)
            XCTAssertGreaterThan(file.length, 0)
            XCTAssertLessThan(Double(file.length) / file.fileFormat.sampleRate, 2)
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                                       frameCapacity: AVAudioFrameCount(file.length)))
            try file.read(into: buffer)
            let samples = try XCTUnwrap(buffer.floatChannelData)[0]
            XCTAssertTrue((0..<Int(buffer.frameLength)).contains { abs(samples[$0]) > 0.001 },
                          "Process tap must deliver the known tone, not just silent buffers")
            try recorder.stop() // Teardown is idempotent.
        }
    }

    func testWorkerAccessCheckProcessesAppAudioAndCleansArtifacts() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let microphone = root.appending(path: "test.caf")
        try writeTone(to: microphone, sampleRate: 48_000, channels: 1, duration: 0.1)
        let original = try Data(contentsOf: microphone)
        let worker = try workerClient()
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
        }, monitor: nil)
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
        }, monitor: nil)
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
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .starting))
        XCTAssertFalse(AppModel.allowsPermissionCheck(in: .stopping))
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
        try RecordingSession(
            microphoneSegments: [CapturedAudioSegment(url: input, startedAt: Date(), endedAt: Date())],
            processTracks: [], captureStartedAt: Date(), captureEndedAt: Date(),
            paths: paths, trigger: CaptureTrigger(method: .manual, processID: nil, bundleID: nil, applicationName: nil),
            metadata: [], warnings: [], interruptions: []
        ).save()
        var claim: RecordingClaim? = try RecordingClaim(paths)
        XCTAssertTrue(store.interruptedRecordings().isEmpty)
        let worker = try workerClient()
        do {
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: input))
            XCTFail("Recovery must reject a recording owned by capture or finalization")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("already being captured or processed"))
        }
        withExtendedLifetime(claim) {}
        claim = nil
        XCTAssertEqual(store.interruptedRecordings().map { $0.resolvingSymlinksInPath() }, [paths.session.resolvingSymlinksInPath()])
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
        let worker = try workerClient()
        for corruptMicrophone in [false, true] {
            let start = Date()
            let paths = try store.paths(startedAt: start, title: "corrupt-\(corruptMicrophone)")
            let microphone = paths.microphoneTemporary(segment: 1)
            let process = processTrack(paths, startedAt: start)
            let participants = process.url
            try writeTone(to: microphone, sampleRate: 48_000, channels: 1, duration: 0.1)
            try writeTone(to: participants, sampleRate: 48_000, channels: 2, duration: 0.1)
            try Data("damaged CAF with irreplaceable payload".utf8)
                .write(to: corruptMicrophone ? microphone : participants)
            let microphoneData = try Data(contentsOf: microphone)
            let participantData = try Data(contentsOf: participants)
            let request = RecordingSession(
                microphoneSegments: [CapturedAudioSegment(url: microphone, startedAt: start, endedAt: start.addingTimeInterval(0.1))],
                processTracks: [process],
                captureStartedAt: start, captureEndedAt: start.addingTimeInterval(0.1),
                paths: paths,
                trigger: CaptureTrigger(method: .manual, processID: nil, bundleID: nil, applicationName: nil),
                metadata: [], warnings: [], interruptions: []
            )
            try request.save()
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

    func testSystemAudioPCMRoundTripsThroughCAF() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        for common in [AVAudioCommonFormat.pcmFormatFloat32, .pcmFormatFloat64, .pcmFormatInt16, .pcmFormatInt32] {
            for interleaved in [false, true] {
                for sampleRate in [44_100.0, 48_000.0] {
                    let format = try XCTUnwrap(AVAudioFormat(commonFormat: common,
                                                           sampleRate: sampleRate, channels: 2,
                                                           interleaved: interleaved))
                    let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 64))
                    pcm.frameLength = 64
                    let buffers = UnsafeMutableAudioBufferListPointer(pcm.mutableAudioBufferList)
                    for frame in 0..<64 {
                        for channel in 0..<2 {
                            let data = try XCTUnwrap(buffers[interleaved ? 0 : channel].mData)
                            let index = interleaved ? frame * 2 + channel : frame
                            let value = channel == 0 ? 0.25 : -0.5
                            switch common {
                            case .pcmFormatFloat32: data.assumingMemoryBound(to: Float.self)[index] = Float(value)
                            case .pcmFormatFloat64: data.assumingMemoryBound(to: Double.self)[index] = value
                            case .pcmFormatInt16: data.assumingMemoryBound(to: Int16.self)[index] = Int16(value * 32768)
                            case .pcmFormatInt32: data.assumingMemoryBound(to: Int32.self)[index] = Int32(value * 2147483648)
                            default: XCTFail("Unsupported fixture format")
                            }
                        }
                    }
                    // Disabled HAL streams advertise bytes with nil data. Entirely
                    // disabled callbacks are ignored; disabled planes are silence.
                    let disabledURL = root.appending(path: "disabled-\(common.rawValue)-\(interleaved)-\(sampleRate).caf")
                    let disabled = try BufferedAudioWriter(format: format, url: disabledURL, startTime: 0)
                    disabled.onError = { XCTFail("Disabled stream reported a source failure: \($0)") }
                    let disabledBuffers = AudioBufferList.allocate(maximumBuffers: buffers.count)
                    defer { free(disabledBuffers.unsafeMutablePointer) }
                    for index in buffers.indices {
                        disabledBuffers[index] = buffers[index]
                        disabledBuffers[index].mData = nil
                    }
                    disabled.enqueue(disabledBuffers.unsafePointer, timestamp: AudioTimeStamp())
                    for index in buffers.indices { disabledBuffers[index] = buffers[index] }
                    var enabledTime = AudioTimeStamp()
                    enabledTime.mFlags = .hostTimeValid
                    if !interleaved {
                        disabledBuffers[0].mData = nil
                        disabled.enqueue(disabledBuffers.unsafePointer, timestamp: enabledTime)
                        disabledBuffers[0] = buffers[0]
                        enabledTime.mHostTime = AVAudioTime.hostTime(forSeconds: 64 / sampleRate)
                    }
                    disabled.enqueue(pcm.audioBufferList, timestamp: enabledTime)
                    disabled.finish()
                    let enabledFile = try AVAudioFile(forReading: disabledURL)
                    XCTAssertEqual(enabledFile.length, interleaved ? 64 : 128)
                    let enabled = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: enabledFile.processingFormat, frameCapacity: 128))
                    try enabledFile.read(into: enabled)
                    XCTAssertEqual(enabled.floatChannelData?[0][0], interleaved ? 0.25 : 0)
                    XCTAssertEqual(enabled.floatChannelData?[1][0], -0.5)

                    let url = root.appending(path: "\(common.rawValue)-\(interleaved)-\(sampleRate).caf")
                    let writer = PCMFile(url: url, startTime: 0)
                    let first = 64 / sampleRate
                    try writer.write(pcm, at: first) // Leading silence, then 64 frames of audio.
                    try writer.write(pcm, at: first) // A duplicated callback must not add time.
                    try writer.write(pcm, at: 192 / sampleRate)
                    try writer.write(pcm, at: 224 / sampleRate)
                    writer.close() // The overlapping final buffer adds only 32 frames.

                    // The tap queue owns a copy, and finish must drain it before close.
                    let queuedURL = root.appending(path: "queued-\(common.rawValue)-\(interleaved)-\(sampleRate).caf")
                    let timestamp = AudioGetCurrentHostTime()
                    let queued = try BufferedAudioWriter(format: format, url: queuedURL,
                        startTime: AVAudioTime.seconds(forHostTime: timestamp))
                    var time = AudioTimeStamp()
                    time.mFlags = .hostTimeValid
                    time.mHostTime = timestamp
                    queued.onError = { XCTFail("Unexpected audio writer error: \($0)") }
                    queued.onWarning = { XCTFail("Unexpected audio writer warning: \($0)") }
                    // Many tiny callbacks cross block boundaries and leave a partial
                    // final block. Every frame must survive copying and finish.
                    let callbackCount = 1025
                    for index in 0..<callbackCount {
                        time.mHostTime = timestamp + AVAudioTime.hostTime(forSeconds: Double(index * 64) / sampleRate)
                        queued.enqueue(pcm.audioBufferList, timestamp: time)
                    }
                    for buffer in buffers { memset(buffer.mData, 0, Int(buffer.mDataByteSize)) }
                    queued.finish()
                    let copied = try AVAudioFile(forReading: queuedURL)
                    XCTAssertEqual(copied.length, Int64(callbackCount * 64))
                    let copy = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: copied.processingFormat,
                                                            frameCapacity: AVAudioFrameCount(callbackCount * 64)))
                    try copied.read(into: copy)
                    for frame in 0..<(callbackCount * 64) {
                        XCTAssertEqual(copy.floatChannelData?[0][frame] ?? 0, 0.25, accuracy: 0.00001)
                        XCTAssertEqual(copy.floatChannelData?[1][frame] ?? 0, -0.5, accuracy: 0.00001)
                    }

                    // A malformed callback ends the source, but valid audio
                    // already copied into the final partial block must survive.
                    let tailURL = root.appending(path: "tail-\(common.rawValue)-\(interleaved)-\(sampleRate).caf")
                    let tail = try BufferedAudioWriter(format: format, url: tailURL,
                        startTime: AVAudioTime.seconds(forHostTime: timestamp))
                    let invalid = expectation(description: "Invalid input reported")
                    tail.onError = { _ in invalid.fulfill() }
                    time.mHostTime = timestamp
                    tail.enqueue(pcm.audioBufferList, timestamp: time)
                    tail.enqueue(pcm.audioBufferList, timestamp: AudioTimeStamp())
                    tail.finish()
                    wait(for: [invalid], timeout: 1)
                    XCTAssertEqual(try AVAudioFile(forReading: tailURL).length, 64)

                    let saved = try AVAudioFile(forReading: url)
                    XCTAssertEqual(saved.length, 288)
                    XCTAssertEqual(saved.fileFormat.sampleRate, sampleRate)
                    XCTAssertEqual(saved.fileFormat.channelCount, 2)
                    XCTAssertTrue(saved.fileFormat.isInterleaved)
                    let result = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: saved.processingFormat, frameCapacity: 288))
                    try saved.read(into: result)
                    XCTAssertEqual(result.frameLength, 288)
                    let channels = try XCTUnwrap(result.floatChannelData)
                    for frame in 0..<288 {
                        let silent = frame < 64 || (128..<192).contains(frame)
                        XCTAssertEqual(channels[0][frame], silent ? 0 : 0.25, accuracy: 0.00001)
                        XCTAssertEqual(channels[1][frame], silent ? 0 : -0.5, accuracy: 0.00001)
                    }
                }
            }
        }
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
        try Data("{}".utf8).write(to: second.session)
        XCTAssertEqual(try store.paths(startedAt: date, title: "Leadership Weekly").baseName,
                       "2026-08-02-leadership-weekly-3")

        let interrupted = first.directory.appending(path: ".2026-08-02-leadership-weekly-microphone-0002.part.caf")
        XCTAssertEqual(store.paths(forInterruptedFile: interrupted).baseName, first.baseName)
    }

    func testManifestEncodingMatchesVersionThreeContract() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appending(path: "fixture.json")
        let now = Date(timeIntervalSince1970: 1_775_260_800)
        let manifest = CaptureManifest(
            schemaVersion: 3,
            meetingID: UUID(uuidString: "E83C18C0-CF42-4AC1-B493-00F3C144FB1E")!,
            slug: "leadership-weekly",
            title: "Leadership Weekly",
            platform: "Zoom",
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
        XCTAssertEqual(object["schemaVersion"] as? Int, 3)
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

    func testFinalizationCreatesVerifiedProcessTracks() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let microphone1 = root.appending(path: ".fixture-microphone-0001.part.caf")
        let microphone2 = root.appending(path: ".fixture-microphone-0002.part.caf")
        let paths = RecordingPaths(directory: root, baseName: "fixture")
        let process1 = processTrack(paths, startedAt: Date(timeIntervalSince1970: 1_775_260_800.05))
        let process2 = processTrack(paths, startedAt: Date(timeIntervalSince1970: 1_775_260_800.15), processID: 43)
        let participants = process1.url
        try writeTone(to: microphone1, sampleRate: 44_100, channels: 1, duration: 0.20)
        try writeTone(to: microphone2, sampleRate: 48_000, channels: 2, duration: 0.20)
        try writeTone(to: participants, sampleRate: 48_000, channels: 2, duration: 0.50)
        try writeTone(to: process2.url, sampleRate: 44_100, channels: 2, duration: 0.30)
        let startedAt = Date(timeIntervalSince1970: 1_775_260_800)
        let temporary = root.appending(path: ".fixture.part.mka")
        let final = root.appending(path: "fixture.mka")

        let result = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt, endedAt: startedAt.addingTimeInterval(0.20)),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30), endedAt: startedAt.addingTimeInterval(0.50)),
            ],
            processTracks: [process1, process2],
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )

        XCTAssertEqual(result.container.file, "fixture.mka")
        XCTAssertEqual(result.container.format, "matroska")
        XCTAssertEqual(result.container.sha256.count, 64)
        XCTAssertEqual(result.tracks.map(\.role), [.microphone, .process, .process])
        XCTAssertEqual(result.tracks.map(\.streamIndex), [0, 1, 2])
        XCTAssertEqual(result.tracks.map(\.codec), ["opus", "opus", "opus"])
        XCTAssertEqual(result.tracks.map(\.channels), [1, 2, 2])
        XCTAssertEqual(result.tracks.map(\.bitrate), [96_000, 128_000, 128_000])
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: microphone1.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: participants.path))

        // Each track must decode through the full session, including apps that
        // stopped early. Later transcription windows must contain silence.
        for index in 0..<3 {
            let decoder = Process()
            decoder.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            decoder.arguments = ["ffmpeg",
                "-nostdin", "-v", "error", "-i", final.path, "-map", "0:a:\(index)",
                "-ac", "1", "-ar", "8000", "-f", "f32le", "-",
            ]
            let pipe = Pipe()
            decoder.standardOutput = pipe
            try decoder.run()
            let pcm = pipe.fileHandleForReading.readDataToEndOfFile()
            while decoder.isRunning { Thread.sleep(forTimeInterval: 0.01) }
            XCTAssertEqual(decoder.terminationStatus, 0)
            XCTAssertEqual(Double(pcm.count) / 4 / 8000, 0.55, accuracy: 0.002)
            if index == 2 {
                let tail: [Float] = pcm.withUnsafeBytes { bytes in
                    stride(from: Int(0.50 * 8000) * 4, to: bytes.count, by: 4).map {
                        bytes.loadUnaligned(fromByteOffset: $0, as: Float.self)
                    }
                }
                XCTAssertFalse(tail.isEmpty)
                XCTAssertTrue(tail.allSatisfy { abs($0) < 0.002 })
            }
        }

        let retried = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt, endedAt: startedAt.addingTimeInterval(0.20)),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30), endedAt: startedAt.addingTimeInterval(0.50)),
            ],
            processTracks: [process1, process2],
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )
        XCTAssertEqual(retried.tracks.count, 3)
        XCTAssertEqual(retried.tracks.compactMap(\.id), [process1.id, process2.id])
        XCTAssertEqual(retried.tracks.compactMap(\.process).map(\.processID), [42, 43])
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
    }

    func testRecoveryRejectsHeaderOnlyAudio() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let empty = root.appending(path: ".fixture-microphone.part.caf")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        _ = try AVAudioFile(forWriting: empty, settings: format.settings)

        XCTAssertFalse(try AudioFinalizer.containsAudio(empty))
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
        XCTAssertFalse(AppModel.allowsMaintenance(in: .recording(client)))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .starting))
        XCTAssertFalse(AppModel.allowsMaintenance(in: .stopping))
    }

    // Temporary release-review regression; fold into the lifecycle test after validation.
    func testReleaseReviewUnresponsiveWorkerIsBounded() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let executable = root.appending(path: "unresponsive-worker")
        try Data("#!/bin/sh\ntrap '' TERM\nexec /bin/sleep 12\n".utf8).write(to: executable)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let worker = try BackgroundWorkerClient(executableURL: executable)
        let start = ProcessInfo.processInfo.systemUptime
        do {
            try await worker.verifyCrashIsolation()
            XCTFail("An unresponsive helper must time out")
        } catch let error as BackgroundWorkerError {
            guard case let .operation(message) = error else {
                return XCTFail("Unexpected timeout error: \(error)")
            }
            XCTAssertTrue(message.contains("made no progress for 5 seconds"))
        }
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - start, 10)
    }

    func testWorkerCrashAndCancellationAreIsolated() async throws {
        // Temporary release-review stress; remove after resolving native shutdown.
        for iteration in 0..<30 {
            FileHandle.standardError.write(Data("lifecycle iteration=\(iteration) start\n".utf8))
            try await exerciseWorkerCrashAndCancellation()
            FileHandle.standardError.write(Data("lifecycle iteration=\(iteration) done\n".utf8))
        }
    }

    private func exerciseWorkerCrashAndCancellation() async throws {
        let worker = try workerClient()
        FileHandle.standardError.write(Data("lifecycle crash begin\n".utf8))
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
        FileHandle.standardError.write(Data("lifecycle crash done\n".utf8))

        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        // Keep the release helper's directory structure with a controlled tool.
        let helpers = root.appending(path: "Contents/Helpers")
        let tools = root.appending(path: "Contents/Resources/bin")
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: tools, withIntermediateDirectories: true)
        let detached = helpers.appending(path: "MeetingCaptureWorker")
        try FileManager.default.copyItem(at: worker.executableURL, to: detached)
        let tool = tools.appending(path: "ffmpeg")
        let script = """
        #!/bin/sh
        printf '%s' "$$" > "${0%/*}/../../../child.pid"
        exec /bin/sleep 10
        """
        try Data(script.utf8).write(to: tool)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: tool.path)
        let store = MeetingStore(root: root)
        let start = Date()
        let paths = try store.paths(startedAt: start, title: "cancellation")
        let input = paths.microphoneTemporary(segment: 1)
        try writeTone(to: input, sampleRate: 48_000, channels: 1, duration: 0.1)
        let original = try Data(contentsOf: input)
        let session = RecordingSession(
            microphoneSegments: [CapturedAudioSegment(url: input, startedAt: start, endedAt: start.addingTimeInterval(0.1))],
            processTracks: [], captureStartedAt: start, captureEndedAt: start.addingTimeInterval(0.1),
            paths: paths, trigger: CaptureTrigger(method: .manual, processID: nil, bundleID: nil, applicationName: nil),
            metadata: [], warnings: [], interruptions: []
        )
        try session.save()
        let childFile = root.appending(path: "child.pid")
        let cancellationWorker = try BackgroundWorkerClient(executableURL: detached)
        let operation = Task { try await cancellationWorker.finalize(session) }
        defer { operation.cancel() }
        FileHandle.standardError.write(Data("lifecycle cancel worker started\n".utf8))
        let deadline = ProcessInfo.processInfo.systemUptime + 15
        var childPID: Int32?
        while childPID == nil, ProcessInfo.processInfo.systemUptime < deadline {
            if let value = try? String(contentsOf: childFile, encoding: .utf8) { childPID = Int32(value) }
            if childPID == nil { try await Task.sleep(for: .milliseconds(20)) }
        }
        let child = try XCTUnwrap(childPID)
        FileHandle.standardError.write(Data("lifecycle cancel child started\n".utf8))
        XCTAssertEqual(kill(child, 0), 0)
        let sentinelArguments = [strdup("/bin/sleep"), strdup("30")]
        defer { sentinelArguments.forEach { free($0) } }
        var argv = sentinelArguments + [nil]
        var envp: [UnsafeMutablePointer<CChar>?] = [nil]
        var sentinel: pid_t = 0
        let launched = posix_spawn(&sentinel, "/bin/sleep", nil, nil, &argv, &envp)
        XCTAssertEqual(launched, 0)
        guard launched == 0, sentinel > 0 else { return }
        defer {
            if sentinel > 0 {
                _ = kill(sentinel, SIGTERM)
                var status: Int32 = 0
                while waitpid(sentinel, &status, 0) < 0 && errno == EINTR {}
            }
        }
        operation.cancel()
        FileHandle.standardError.write(Data("lifecycle cancel requested\n".utf8))
        let stopped = ProcessInfo.processInfo.systemUptime + 5
        do {
            _ = try await operation.value
            XCTFail("Cancellation must stop the worker and its tool")
        } catch is CancellationError {
        } catch {
            XCTFail("Unexpected cancellation error: \(error)")
        }
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime, stopped)
        FileHandle.standardError.write(Data("lifecycle cancel wait complete\n".utf8))
        while kill(child, 0) == 0, ProcessInfo.processInfo.systemUptime < stopped {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(kill(child, 0), -1, "The worker's tool must be reaped")
        var sentinelStatus: Int32 = 0
        var observed: pid_t
        repeat { observed = waitpid(sentinel, &sentinelStatus, WNOHANG) } while observed < 0 && errno == EINTR
        if observed != 0 { sentinel = 0 } // Do not signal a reaped or unowned PID.
        XCTAssertEqual(observed, 0, "Cancellation must leave unrelated processes running")
        XCTAssertTrue(RecordingClaim.isAvailable(paths))
        XCTAssertEqual(try Data(contentsOf: input), original)
        XCTAssertTrue(FileManager.default.fileExists(atPath: paths.session.path))
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

    func testJournalRecoveryPreservesGapsAndFailureStatus() async throws {
        for legacyOpenJournal in [false, true] {
            let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: root) }
            let store = MeetingStore(root: root)
            let start = Date()
            let paths = try store.paths(startedAt: start, title: "journal")
            let first = paths.microphoneTemporary(segment: 1)
            let second = paths.microphoneTemporary(segment: 2)
            try writeTone(to: first, sampleRate: 48_000, channels: 1, duration: 0.1)
            try writeTone(to: second, sampleRate: 48_000, channels: 1, duration: 0.1)
            let process = processTrack(paths, startedAt: start)
            try writeTone(to: process.url, sampleRate: 48_000, channels: 2, duration: 1)
            let otherProcess = processTrack(paths, startedAt: start.addingTimeInterval(0.2), processID: 43)
            try writeTone(to: otherProcess.url, sampleRate: 44_100, channels: 2, duration: 0.5)
            var session = RecordingSession(
                microphoneSegments: [
                    CapturedAudioSegment(url: first, startedAt: start, endedAt: start),
                    CapturedAudioSegment(url: second, startedAt: start.addingTimeInterval(0.4), endedAt: start.addingTimeInterval(0.4))
                ], processTracks: [process, otherProcess],
                captureStartedAt: start, captureEndedAt: legacyOpenJournal ? start : nil, paths: paths,
                trigger: CaptureTrigger(method: .manual, processID: nil, bundleID: nil, applicationName: "Test"),
                metadata: [], warnings: [], interruptions: []
            )
            try session.save()
            XCTAssertEqual(store.interruptedRecordings().map { $0.resolvingSymlinksInPath() }, [paths.session.resolvingSymlinksInPath()])
            let loaded = try RecordingSession.load(from: paths.session)
            XCTAssertEqual(loaded.microphoneSegments.map(\.startedAt), session.microphoneSegments.map(\.startedAt))
            XCTAssertEqual(loaded.captureEndedAt, session.captureEndedAt)
            let journalBeforeRecovery = try Data(contentsOf: paths.session)
            let remainingProcess = try Data(contentsOf: process.url)
            let worker = try workerClient()
            let manifestURL = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let manifest = try decoder.decode(CaptureManifest.self, from: Data(contentsOf: manifestURL))
            XCTAssertEqual(manifest.status, .incomplete)
            XCTAssertEqual(manifest.audio.count, 3)
            XCTAssertEqual(manifest.audio.compactMap(\.id), [process.id, otherProcess.id])
            XCTAssertEqual(manifest.audio.compactMap(\.process).map(\.processID), [42, 43])
            XCTAssertTrue(store.interruptedRecordings().isEmpty)

            // Simulate a crash halfway through cleanup after publication. Only one
            // raw input remains; recovery must reuse the complete committed archive.
            let committedArchive = try Data(contentsOf: paths.archiveFinal)
            let committedManifest = try Data(contentsOf: manifestURL)
            try journalBeforeRecovery.write(to: paths.session)
            try remainingProcess.write(to: process.url)
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            XCTAssertEqual(try Data(contentsOf: paths.archiveFinal), committedArchive)
            XCTAssertEqual(try Data(contentsOf: manifestURL), committedManifest)
            XCTAssertFalse(FileManager.default.fileExists(atPath: process.url.path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: paths.session.path))

            // A corrupt publication cannot authorize deleting the remaining inputs.
            try journalBeforeRecovery.write(to: paths.session)
            try remainingProcess.write(to: process.url)
            try Data("corrupt".utf8).write(to: paths.archiveFinal)
            do {
                _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
                XCTFail("Recovery must reject a checksum mismatch")
            } catch {}
            XCTAssertEqual(try Data(contentsOf: process.url), remainingProcess)
            XCTAssertEqual(try Data(contentsOf: paths.session), journalBeforeRecovery)
            try committedArchive.write(to: paths.archiveFinal)
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            let restored = root.appending(path: "recovered-microphone.caf")
            let decoderProcess = Process()
            decoderProcess.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            decoderProcess.arguments = ["ffmpeg", "-nostdin", "-v", "error", "-i", paths.archiveFinal.path,
                                 "-map", "0:a:0", "-c:a", "pcm_f32le", restored.path]
            try decoderProcess.run()
            while decoderProcess.isRunning { Thread.sleep(forTimeInterval: 0.01) }
            XCTAssertEqual(decoderProcess.terminationStatus, 0)
            let audio = try AVAudioFile(forReading: restored)
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: AVAudioFrameCount(audio.length)))
            try audio.read(into: buffer)
            let samples = try XCTUnwrap(buffer.floatChannelData)[0]
            XCTAssertGreaterThan(buffer.frameLength, 23_000)
            let middle = (10_000..<14_000).map { abs(samples[$0]) }.max() ?? 1
            let resumed = (20_000..<23_000).map { abs(samples[$0]) }.max() ?? 0
            XCTAssertLessThan(middle, 0.001)
            XCTAssertGreaterThan(resumed, 0.01)

            // A readable two-track recording with a capture error is still incomplete.
            let failedPaths = try store.paths(startedAt: start, title: "failed")
            let mic = failedPaths.microphoneTemporary(segment: 1)
            try writeTone(to: mic, sampleRate: 48_000, channels: 1, duration: 0.2)
            let failedProcess = processTrack(failedPaths, startedAt: start)
            try writeTone(to: failedProcess.url, sampleRate: 48_000, channels: 2, duration: 0.2)
            session = RecordingSession(
                microphoneSegments: [CapturedAudioSegment(url: mic, startedAt: start, endedAt: start.addingTimeInterval(0.1))],
                processTracks: [failedProcess],
                captureStartedAt: start, captureEndedAt: start.addingTimeInterval(0.1), paths: failedPaths,
                trigger: session.trigger, metadata: [], warnings: ["Capture stopped after a write error"], interruptions: [], captureFailed: true
            )
            try session.save()
            // Raw tail can extend past Stop; recovery must preserve that cutoff.
            let failedManifest = try await worker.recover(RecoverCaptureRequest(interruptedFile: failedPaths.session))
            let failed = try decoder.decode(CaptureManifest.self, from: Data(contentsOf: failedManifest))
            XCTAssertEqual(failed.audio.count, 2)
            XCTAssertEqual(failed.status, .incomplete)
            XCTAssertTrue(failed.audio.allSatisfy { abs($0.durationSeconds - 0.1) < 0.02 })
            XCTAssertFalse(failed.warnings.isEmpty)
        }
    }

    private func workerClient() throws -> BackgroundWorkerClient {
        let application: URL
        if let path = ProcessInfo.processInfo.environment["MEETING_CAPTURE_TEST_APP"] {
            application = URL(fileURLWithPath: path)
        } else {
            application = Bundle(for: MeetingStoreTests.self).bundleURL
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        }
        return try BackgroundWorkerClient(executableURL: application.appending(path: "Contents/Helpers/MeetingCaptureWorker"))
    }

    private func processTrack(_ paths: RecordingPaths, startedAt: Date, processID: Int32 = 42) -> CapturedProcessTrack {
        let id = UUID()
        return CapturedProcessTrack(id: id,
            process: AudioProcess(objectID: UInt32(processID), processID: processID,
                                  bundleID: "fixture.audio", name: "Fixture"),
            url: paths.processTemporary(id), startedAt: startedAt)
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
