import Foundation
import AVFoundation
import XCTest
@testable import MeetingCapture

final class MeetingStoreTests: XCTestCase {
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
