@preconcurrency import AVFoundation
@preconcurrency import ScreenCaptureKit
import CoreMedia
import CoreGraphics
import Foundation

final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private var stream: SCStream?
    private var audioFile: SystemAudioFile?
    private var deliveryError: Error?
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.system-audio")
    private(set) var firstSampleAt: Date?
    var onLevel: (@Sendable (Float) -> Void)?

    func start(processID: pid_t, bundleID: String?, to url: URL) async throws {
        try await startCapture(processID: processID, bundleID: bundleID, to: url, prompt: false)
    }

    func startAuthorizationCheck(to url: URL) async throws {
        try await startCapture(processID: nil, bundleID: nil, to: url, prompt: true)
    }

    private func startCapture(processID: pid_t?, bundleID: String?, to url: URL, prompt: Bool) async throws {
        firstSampleAt = nil
        guard CGPreflightScreenCaptureAccess() || (prompt && CGRequestScreenCaptureAccess()) else {
            throw CaptureError.screenRecordingPermissionRequired
        }

        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            if !CGPreflightScreenCaptureAccess() || error.localizedDescription.localizedCaseInsensitiveContains("TCC") {
                throw CaptureError.screenRecordingPermissionRequired
            }
            throw error
        }
        let identities = content.applications.map {
            CaptureApplicationIdentity(
                processID: $0.processID,
                bundleID: $0.bundleIdentifier,
                applicationName: $0.applicationName,
                isUserApplication: true
            )
        }
        guard let display = content.displays.first else { throw CaptureError.noDisplay }
        let filter: SCContentFilter
        if let processID {
            guard let index = Self.matchingApplicationIndex(processID: processID, bundleID: bundleID, candidates: identities) else {
                throw CaptureError.triggeringApplicationUnavailable
            }
            filter = SCContentFilter(display: display, including: [content.applications[index]], exceptingWindows: [])
        } else {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        }
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2

        queue.sync {
            audioFile = SystemAudioFile(url: url)
            deliveryError = nil
        }

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        self.stream = stream
        try await stream.startCapture()
    }

    static func matchingApplicationIndex(
        processID: pid_t,
        bundleID: String?,
        candidates: [CaptureApplicationIdentity]
    ) -> Int? {
        if let exact = candidates.firstIndex(where: { $0.processID == processID }) { return exact }
        guard let bundleID else { return nil }
        return candidates.firstIndex(where: { $0.bundleID == bundleID })
    }

    func stop() async throws {
        var stopError: Error?
        if let stream {
            do { try await stream.stopCapture() } catch { stopError = error }
        }
        stream = nil
        // Close on the delivery queue so no callback can write during teardown.
        let failure = queue.sync {
            audioFile?.close()
            audioFile = nil
            let failure = deliveryError
            deliveryError = nil
            return failure
        }
        if let failure { stopError = failure }
        if let stopError { throw stopError }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid,
              CMSampleBufferGetNumSamples(sampleBuffer) > 0,
              deliveryError == nil, let audioFile else { return }
        do {
            try audioFile.write(sampleBuffer)
            if firstSampleAt == nil { firstSampleAt = Date() }
            onLevel?(0.35)
        } catch {
            deliveryError = CaptureError.writerFailure("System audio PCM write: \(CaptureError.details(error))")
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        queue.async {
            if self.deliveryError == nil { self.deliveryError = error }
        }
    }
}

// Confined to the recorder's delivery queue. Use the actual incoming format for
// buffers, and interleaved PCM on disk; CAF cannot store planar PCM directly.
final class SystemAudioFile {
    private let url: URL
    private var file: AVAudioFile?

    init(url: URL) { self.url = url }

    func write(_ sampleBuffer: CMSampleBuffer) throws {
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frames > 0, frames <= Int(Int32.max),
              let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              streamDescription.pointee.mFormatID == kAudioFormatLinearPCM,
              let format = AVAudioFormat(streamDescription: streamDescription),
              format.sampleRate > 0, format.channelCount > 0,
              format.commonFormat != .otherFormat else {
            throw CaptureError.writerFailure("Invalid system audio PCM buffer")
        }
        if file == nil {
            guard let diskFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                                sampleRate: format.sampleRate,
                                                channels: format.channelCount, interleaved: true) else {
                throw CaptureError.writerFailure("Invalid system audio file format")
            }
            file = try AVAudioFile(forWriting: url, settings: diskFormat.settings,
                                   commonFormat: format.commonFormat, interleaved: format.isInterleaved)
        }
        guard let file,
              file.processingFormat.sampleRate == format.sampleRate,
              file.processingFormat.channelCount == format.channelCount,
              file.processingFormat.commonFormat == format.commonFormat,
              file.processingFormat.isInterleaved == format.isInterleaved else {
            throw CaptureError.writerFailure("System audio format changed during recording")
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                           frameCapacity: AVAudioFrameCount(frames)) else {
            throw CaptureError.writerFailure("Could not allocate system audio PCM buffer")
        }
        buffer.frameLength = AVAudioFrameCount(frames)
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList
        )
        guard status == noErr else {
            throw CaptureError.writerFailure("Could not copy system audio PCM frames (OSStatus \(status))")
        }
        try file.write(from: buffer)
    }

    func close() { file = nil }
}
