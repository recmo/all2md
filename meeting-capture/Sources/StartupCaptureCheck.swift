@preconcurrency import AVFoundation
import CoreMedia
import Foundation

struct StartupCaptureReport: Equatable, Sendable {
    let systemAudioObserved: Bool
    var accessibility: String = "Accessibility: not checked."
}

@MainActor
enum StartupCaptureCheck {
    static func run() async throws -> StartupCaptureReport {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw CaptureError.microphonePermissionRequired
            }
        default: throw CaptureError.microphonePermissionRequired
        }
        try validateStorage(MeetingStore().root)
        let worker = try BackgroundWorkerClient()

        // Exercise the same recording implementations and TCC identity as live
        // capture. These files never enter the meeting store or recovery queue.
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MeetingCapture-check-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                              attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let microphoneURL = directory.appending(path: "microphone.caf")
        let systemURL = directory.appending(path: "system.caf")
        let microphone = MicrophoneRecorder()
        let system = SystemAudioRecorder()
        do {
            try await system.startAuthorizationCheck(to: systemURL)
            let startTime = CMClockGetTime(CMClockGetHostTimeClock())
            try microphone.start(to: microphoneURL, deviceID: nil, startedAt: Date(), startTime: startTime)
            try await Task.sleep(for: .seconds(1.5))
            _ = microphone.stop()
            try await system.stop()
            let microphoneFrames = try AVAudioFile(forReading: microphoneURL).length
            try validateMicrophoneFrames(microphoneFrames)
            let observed = system.firstSampleAt != nil
            if observed {
                guard try AVAudioFile(forReading: systemURL).length > 0 else {
                    throw CaptureError.writerFailure("The system-audio check received samples but could not save them. Retry the check.")
                }
            }
            let request = WorkerAccessRequest(
                root: MeetingStore().root, microphone: microphoneURL,
                applicationProcessID: ProcessInfo.processInfo.processIdentifier
            )
            _ = try await worker.checkAccess(request)
            let accessibility: String
            do { accessibility = try await worker.checkMetadataAccess(request).accessibility }
            catch { accessibility = "Accessibility: optional check failed. \(error.localizedDescription)" }
            return StartupCaptureReport(systemAudioObserved: observed, accessibility: accessibility)
        } catch {
            _ = microphone.stop()
            try? await system.stop()
            throw error
        }
    }

    static func validateMicrophoneFrames(_ frames: AVAudioFramePosition) throws {
        guard frames > 0 else {
            throw CaptureError.writerFailure("The microphone check produced no audio frames. Check Microphone access and your input device, then retry.")
        }
    }

    static func validateStorage(_ root: URL) throws {
        do {
            try MeetingStore(root: root).validateAccess()
        } catch {
            throw CaptureError.writerFailure("Cannot save to \(root.path). Check folder access and available disk space, then retry. \(error.localizedDescription)")
        }
    }
}
