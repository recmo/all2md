@preconcurrency import AVFoundation
@preconcurrency import ScreenCaptureKit
import CoreMedia
import CoreGraphics
import Foundation

final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private var stream: SCStream?
    private var writer: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var sessionStarted = false
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.system-audio")
    private(set) var firstSampleAt: Date?
    var onLevel: (@Sendable (Float) -> Void)?

    func start(processID: pid_t, bundleID: String?, to url: URL) async throws {
        firstSampleAt = nil
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
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
        guard let index = Self.matchingApplicationIndex(processID: processID, bundleID: bundleID, candidates: identities) else {
            throw CaptureError.triggeringApplicationUnavailable
        }
        let application = content.applications[index]
        guard let display = content.displays.first else { throw CaptureError.noDisplay }

        let filter = SCContentFilter(display: display, including: [application], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2

        let writer = try AVAssetWriter(outputURL: url, fileType: .caf)
        let input = try Self.makeWriterInput()
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw CaptureError.writerFailure("cannot add PCM input") }
        writer.add(input)
        guard writer.startWriting() else {
            throw CaptureError.writerFailure(writer.error?.localizedDescription ?? "could not start")
        }
        self.writer = writer
        writerInput = input

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        self.stream = stream
        try await stream.startCapture()
    }

    static func makeWriterInput() throws -> AVAssetWriterInput {
        // ScreenCaptureKit already delivers PCM. Passing an explicit PCM output
        // dictionary aborts in AVAssetWriterInput on macOS 26.6; passthrough
        // preserves the sample-buffer format and keeps capture initialization safe.
        // Passthrough requires a source hint before a CAF writer will accept it.
        var streamDescription = AudioStreamBasicDescription(
            mSampleRate: 48_000,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagsNativeFloatPacked,
            mBytesPerPacket: 8,
            mFramesPerPacket: 1,
            mBytesPerFrame: 8,
            mChannelsPerFrame: 2,
            mBitsPerChannel: 32,
            mReserved: 0
        )
        var formatDescription: CMAudioFormatDescription?
        let status = CMAudioFormatDescriptionCreate(
            allocator: kCFAllocatorDefault,
            asbd: &streamDescription,
            layoutSize: 0,
            layout: nil,
            magicCookieSize: 0,
            magicCookie: nil,
            extensions: nil,
            formatDescriptionOut: &formatDescription
        )
        guard status == noErr, let formatDescription else {
            throw CaptureError.writerFailure("could not create PCM source format hint (\(status))")
        }
        return AVAssetWriterInput(
            mediaType: .audio,
            outputSettings: nil,
            sourceFormatHint: formatDescription
        )
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
        if let stream { try await stream.stopCapture() }
        stream = nil
        writerInput?.markAsFinished()
        if let writer {
            await writer.finishWriting()
            if writer.status == .failed {
                throw CaptureError.writerFailure(writer.error?.localizedDescription ?? "finalization failed")
            }
        }
        writer = nil
        writerInput = nil
        sessionStarted = false
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, let writer, let writerInput else { return }
        if !sessionStarted {
            writer.startSession(atSourceTime: sampleBuffer.presentationTimeStamp)
            sessionStarted = true
            firstSampleAt = Date()
        }
        if writerInput.isReadyForMoreMediaData { writerInput.append(sampleBuffer) }
        onLevel?(CMSampleBufferGetNumSamples(sampleBuffer) > 0 ? 0.35 : 0)
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {}
}
