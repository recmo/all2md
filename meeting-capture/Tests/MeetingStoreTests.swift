import Foundation
@preconcurrency import AVFoundation
import XCTest
@testable import MeetingCapture

final class MeetingStoreTests: XCTestCase {
    func testFinalizationCreatesMicrophoneAndSystemTracks() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let microphone1 = root.appending(path: ".fixture-microphone-0001.part.caf")
        let microphone2 = root.appending(path: ".fixture-microphone-0002.part.caf")
        let paths = RecordingPaths(directory: root, baseName: "fixture")
        let system1 = CapturedAudioSegment(url: paths.systemTemporary(segment: 1), startedAt: Date(timeIntervalSince1970: 1_775_260_800.05))
        let system2 = CapturedAudioSegment(url: paths.systemTemporary(segment: 2), startedAt: Date(timeIntervalSince1970: 1_775_260_800.25))
        try writeTone(to: microphone1, sampleRate: 44_100, channels: 1, duration: 0.20)
        try writeTone(to: microphone2, sampleRate: 48_000, channels: 2, duration: 0.20)
        try writeTone(to: system1.url, sampleRate: 48_000, channels: 2, duration: 0.10)
        try writeTone(to: system2.url, sampleRate: 44_100, channels: 2, duration: 0.20)
        let startedAt = Date(timeIntervalSince1970: 1_775_260_800)
        let temporary = root.appending(path: ".fixture.part.mka")
        let final = root.appending(path: "fixture.mka")

        let result = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30)),
            ],
            systemSegments: [system1, system2],
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )

        XCTAssertEqual(result.container.file, "fixture.mka")
        XCTAssertEqual(result.container.format, "matroska")
        XCTAssertEqual(result.container.sha256.count, 64)
        XCTAssertEqual(result.tracks.map(\.role), [.microphone, .system])
        XCTAssertEqual(result.tracks.map(\.streamIndex), [0, 1])
        XCTAssertEqual(result.tracks.map(\.codec), ["opus", "opus"])
        XCTAssertEqual(result.tracks.map(\.channels), [1, 2])
        XCTAssertEqual(result.tracks.map(\.bitrate), [96_000, 128_000])
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: microphone1.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: system1.url.path))

        // Both sources keep their timing across segment restarts and extend
        // with silence to the recording cutoff.
        for index in 0..<2 {
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
            let samples: [Float] = pcm.withUnsafeBytes { bytes in
                stride(from: 0, to: bytes.count, by: 4).map {
                    bytes.loadUnaligned(fromByteOffset: $0, as: Float.self)
                }
            }
            let gap = index == 0 ? 0.23..<0.27 : 0.18..<0.22
            XCTAssertTrue(samples[Int(gap.lowerBound * 8000)..<Int(gap.upperBound * 8000)].allSatisfy { abs($0) < 0.002 })
            XCTAssertTrue(samples[Int(0.53 * 8000)...].allSatisfy { abs($0) < 0.002 })
            XCTAssertGreaterThan(samples[Int(0.32 * 8000)..<Int(0.38 * 8000)].map { abs($0) }.max() ?? 0, 0.01)
        }

        let retried = try AudioFinalizer.createArchive(
            microphoneSegments: [
                CapturedAudioSegment(url: microphone1, startedAt: startedAt),
                CapturedAudioSegment(url: microphone2, startedAt: startedAt.addingTimeInterval(0.30)),
            ],
            systemSegments: [system1, system2],
            captureStartedAt: startedAt,
            captureEndedAt: startedAt.addingTimeInterval(0.55),
            temporaryDestination: temporary,
            finalDestination: final
        )
        XCTAssertEqual(retried.tracks.count, 2)
        XCTAssertTrue(FileManager.default.fileExists(atPath: final.path))
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
            let system = CapturedAudioSegment(url: paths.systemTemporary(segment: 1), startedAt: start)
            try writeTone(to: system.url, sampleRate: 48_000, channels: 2, duration: 1)
            let resumedSystem = CapturedAudioSegment(url: paths.systemTemporary(segment: 2), startedAt: start.addingTimeInterval(1.2))
            try writeTone(to: resumedSystem.url, sampleRate: 44_100, channels: 2, duration: 0.5)
            var session = RecordingSession(
                microphoneSegments: [
                    CapturedAudioSegment(url: first, startedAt: start),
                    CapturedAudioSegment(url: second, startedAt: start.addingTimeInterval(0.4))
                ], systemSegments: [system, resumedSystem],
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
            let remainingSystem = try Data(contentsOf: system.url)
            let worker = workerClient()
            let manifestURL = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let manifest = try decoder.decode(CaptureManifest.self, from: Data(contentsOf: manifestURL))
            XCTAssertEqual(manifest.status, .incomplete)
            XCTAssertEqual(manifest.audio.map(\.role), [.microphone, .system])
            XCTAssertTrue(manifest.audio.allSatisfy { abs($0.durationSeconds - 1.7) < 0.02 })
            XCTAssertTrue(store.interruptedRecordings().isEmpty)

            // Simulate a crash halfway through cleanup after publication. Only one
            // raw input remains; recovery must reuse the complete committed archive.
            let committedArchive = try Data(contentsOf: paths.archiveFinal)
            let committedManifest = try Data(contentsOf: manifestURL)
            try journalBeforeRecovery.write(to: paths.session)
            try remainingSystem.write(to: system.url)
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            XCTAssertEqual(try Data(contentsOf: paths.archiveFinal), committedArchive)
            XCTAssertEqual(try Data(contentsOf: manifestURL), committedManifest)
            XCTAssertFalse(FileManager.default.fileExists(atPath: system.url.path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: paths.session.path))

            // A corrupt publication cannot authorize deleting the remaining inputs.
            try journalBeforeRecovery.write(to: paths.session)
            try remainingSystem.write(to: system.url)
            try Data("corrupt".utf8).write(to: paths.archiveFinal)
            do {
                _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
                XCTFail("Recovery must reject a checksum mismatch")
            } catch {}
            XCTAssertEqual(try Data(contentsOf: system.url), remainingSystem)
            XCTAssertEqual(try Data(contentsOf: paths.session), journalBeforeRecovery)
            try committedArchive.write(to: paths.archiveFinal)
            _ = try await worker.recover(RecoverCaptureRequest(interruptedFile: paths.session))
            let restored = root.appending(path: "recovered-microphone.caf")
            let decoderProcess = Process()
            decoderProcess.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            decoderProcess.arguments = ["ffmpeg", "-nostdin", "-v", "error", "-i", paths.archiveFinal.path,
                                 "-map", "0:a:0", "-c:a", "pcm_f32le", restored.path]
            try decoderProcess.run()
            while decoderProcess.isRunning { try await Task.sleep(for: .milliseconds(10)) }
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
            let failedSystem = CapturedAudioSegment(url: failedPaths.systemTemporary(segment: 1), startedAt: start)
            try writeTone(to: failedSystem.url, sampleRate: 48_000, channels: 2, duration: 0.2)
            session = RecordingSession(
                microphoneSegments: [CapturedAudioSegment(url: mic, startedAt: start)],
                systemSegments: [failedSystem],
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

    private func workerClient() -> BackgroundWorkerClient {
        let application = Bundle(for: MeetingStoreTests.self).bundleURL
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return BackgroundWorkerClient(executableURL: application.appending(path: "Contents/Helpers/MeetingCaptureWorker"))
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
