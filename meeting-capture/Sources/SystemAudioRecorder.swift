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
    var onError: (@Sendable (Error) -> Void)?

    func start(processID: pid_t, bundleID: String?, to url: URL, startTime: CMTime) async throws {
        try await startCapture(processID: processID, bundleID: bundleID, to: url, prompt: false, startTime: startTime)
    }

    func startAuthorizationCheck(to url: URL) async throws {
        try await startCapture(processID: nil, bundleID: nil, to: url, prompt: true,
                               startTime: CMClockGetTime(CMClockGetHostTimeClock()))
    }

    private func startCapture(processID: pid_t?, bundleID: String?, to url: URL, prompt: Bool, startTime: CMTime) async throws {
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

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        queue.sync {
            firstSampleAt = nil
            audioFile = SystemAudioFile(url: url, startTime: startTime)
            deliveryError = nil
            self.stream = stream
        }
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
        if let stream = queue.sync(execute: { self.stream }) {
            do { try await stream.stopCapture() } catch { stopError = error }
        }
        // Close on the delivery queue so no callback can write during teardown.
        let failure = queue.sync {
            stream = nil
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
        guard stream === self.stream, type == .audio, sampleBuffer.isValid,
              CMSampleBufferGetNumSamples(sampleBuffer) > 0,
              deliveryError == nil, let audioFile else { return }
        do {
            try audioFile.write(sampleBuffer)
            if firstSampleAt == nil { firstSampleAt = Date() }
            onLevel?(0.35)
        } catch {
            reportFailure(CaptureError.writerFailure("System audio PCM write: \(CaptureError.details(error))"))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        queue.async {
            guard stream === self.stream else { return }
            self.reportFailure(error)
        }
    }

    private func reportFailure(_ error: Error) {
        guard deliveryError == nil else { return }
        deliveryError = error
        onError?(error)
    }
}

// Both capture paths use the same timestamp-aware PCM writer. Callers serialize
// access on their delivery queue or lock; the file always stores interleaved PCM.
final class SystemAudioFile {
    private let file: PCMFile

    init(url: URL, startTime: CMTime? = nil) {
        file = PCMFile(url: url, startTime: startTime?.seconds)
    }

    func write(_ sampleBuffer: CMSampleBuffer) throws {
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        let timestamp = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard frames > 0, frames <= Int(Int32.max), timestamp.isValid, timestamp.isNumeric,
              let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              streamDescription.pointee.mFormatID == kAudioFormatLinearPCM,
              let format = AVAudioFormat(streamDescription: streamDescription),
              format.commonFormat != .otherFormat,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
            throw CaptureError.writerFailure("Invalid system audio PCM buffer or timestamp")
        }
        buffer.frameLength = AVAudioFrameCount(frames)
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList
        )
        guard status == noErr else {
            throw CaptureError.writerFailure("Could not copy system audio PCM frames (OSStatus \(status))")
        }
        try file.write(buffer, at: timestamp.seconds)
    }

    func close() { file.close() }
}

final class PCMFile {
    private let url: URL
    private var file: AVAudioFile?
    private var startTime: Double?

    init(url: URL, startTime: Double? = nil) {
        self.url = url
        self.startTime = startTime
    }

    func write(_ input: AVAudioPCMBuffer, at time: Double) throws {
        guard input.frameLength > 0 else { return }
        let format = input.format
        guard time.isFinite, format.sampleRate > 0, format.channelCount > 0,
              format.commonFormat != .otherFormat else {
            throw CaptureError.writerFailure("Invalid PCM format or timestamp")
        }
        if file == nil {
            guard let diskFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                                sampleRate: format.sampleRate,
                                                channels: format.channelCount, interleaved: true) else {
                throw CaptureError.writerFailure("Invalid audio file format")
            }
            file = try AVAudioFile(forWriting: url, settings: diskFormat.settings,
                                   commonFormat: format.commonFormat, interleaved: format.isInterleaved)
        }
        guard let file, file.processingFormat.sampleRate == format.sampleRate,
              file.processingFormat.channelCount == format.channelCount,
              file.processingFormat.commonFormat == format.commonFormat,
              file.processingFormat.isInterleaved == format.isInterleaved else {
            throw CaptureError.writerFailure("Audio format changed without starting a new segment")
        }
        let origin = startTime ?? time
        startTime = origin
        let position = (time - origin) * format.sampleRate
        guard position.isFinite, abs(position) < Double(Int64.max / 2) else {
            throw CaptureError.writerFailure("Invalid audio sample position")
        }
        let target = AVAudioFramePosition(position.rounded())
        if target > file.framePosition {
            guard let silence = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4096) else {
                throw CaptureError.writerFailure("Could not allocate silence buffer")
            }
            silence.frameLength = silence.frameCapacity
            for buffer in UnsafeMutableAudioBufferListPointer(silence.mutableAudioBufferList) {
                buffer.mData?.initializeMemory(as: UInt8.self, repeating: 0, count: Int(buffer.mDataByteSize))
            }
            while file.framePosition < target {
                silence.frameLength = AVAudioFrameCount(min(4096, target - file.framePosition))
                try file.write(from: silence)
            }
        }
        let offset = min(Int64(input.frameLength), max(0, file.framePosition - target))
        let remaining = input.frameLength - AVAudioFrameCount(offset)
        guard remaining > 0 else { return }
        if offset == 0, format.isEqual(file.processingFormat) {
            try file.write(from: input)
            return
        }
        guard let output = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: remaining) else {
            throw CaptureError.writerFailure("Could not allocate audio buffer")
        }
        output.frameLength = remaining
        let source = UnsafeMutableAudioBufferListPointer(input.mutableAudioBufferList)
        let destination = UnsafeMutableAudioBufferListPointer(output.mutableAudioBufferList)
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        guard source.count == destination.count else { throw CaptureError.writerFailure("PCM buffer layout mismatch") }
        for index in source.indices {
            guard let from = source[index].mData, let to = destination[index].mData,
                  Int(source[index].mDataByteSize) >= (Int(offset) + Int(remaining)) * bytesPerFrame else {
                throw CaptureError.writerFailure("Incomplete PCM audio buffer")
            }
            to.copyMemory(from: from.advanced(by: Int(offset) * bytesPerFrame), byteCount: Int(remaining) * bytesPerFrame)
        }
        try file.write(from: output)
    }

    func close() { file = nil }
}
