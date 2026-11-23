@preconcurrency import AVFoundation
import Foundation

// Both capture paths use the same timestamp-aware PCM writer. Callers serialize
// access on their writer queue; the file always stores interleaved PCM.
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
