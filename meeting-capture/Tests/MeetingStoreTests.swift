import Foundation
import AVFoundation
import XCTest
@testable import MeetingCapture

final class MeetingStoreTests: XCTestCase {
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
    }

    func testManifestEncodingMatchesVersionOneContract() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appending(path: "fixture.json")
        let now = Date(timeIntervalSince1970: 1_775_260_800)
        let manifest = CaptureManifest(
            schemaVersion: 1,
            meetingID: UUID(uuidString: "E83C18C0-CF42-4AC1-B493-00F3C144FB1E")!,
            slug: "leadership-weekly",
            title: "Leadership Weekly",
            platform: "Zoom",
            calendarEventID: nil,
            startedAt: now,
            endedAt: now.addingTimeInterval(60),
            timeZone: "Europe/Warsaw",
            trigger: CaptureTrigger(method: .audioProcess, processID: 42, bundleID: "us.zoom.xos", applicationName: "zoom.us"),
            audio: [AudioTrack(role: .microphone, file: "microphone.flac", format: "flac", sampleRate: 48_000, channels: 1, durationSeconds: 60, sha256: String(repeating: "a", count: 64))],
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
        XCTAssertEqual(object["schemaVersion"] as? Int, 1)
        XCTAssertEqual(object["slug"] as? String, "leadership-weekly")
        XCTAssertNotNil(object["trigger"])
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

    func testCAFToFLACFinalizationIsLosslessAndChecksummed() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let source = root.appending(path: ".fixture-microphone.part.caf")
        let destination = root.appending(path: "fixture-microphone.flac")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let file = try AVAudioFile(forWriting: source, settings: format.settings)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4_800))
        buffer.frameLength = 4_800
        for frame in 0..<Int(buffer.frameLength) { buffer.floatChannelData?[0][frame] = sin(Float(frame) / 20) * 0.1 }
        try file.write(from: buffer)

        let track = try AudioFinalizer.convertToFLAC(source: source, destination: destination, role: .microphone)
        XCTAssertEqual(track.format, "flac")
        XCTAssertEqual(track.channels, 1)
        XCTAssertEqual(track.durationSeconds, 0.1, accuracy: 0.001)
        XCTAssertEqual(track.sha256.count, 64)
        XCTAssertTrue(FileManager.default.fileExists(atPath: destination.path))
    }
}
