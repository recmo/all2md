@preconcurrency import AVFoundation
import CoreAudio
import Foundation
import Synchronization

// Audio callbacks run synchronously. Copy into preallocated slots and return;
// only this separate queue may allocate PCMFile buffers or touch the filesystem.
final class BufferedAudioWriter: @unchecked Sendable {
    private let diagnosticID: String
    private let file: PCMFile
    private let format: AVAudioFormat
    private final class Slot {
        let buffer: AVAudioPCMBuffer
        var timestamp: UInt64 = 0
        init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }
    }
    private let slots: [Slot]
    // One producer (serial callbacks for one source), one consumer (writer queue).
    // Release/acquire publishes slots and prevents reuse until writing completes.
    // Bit 0 owns the producer state; bit 1 closes admission during shutdown.
    private let inputState = Atomic<UInt8>(0)
    private let readIndex = Atomic<Int>(0)
    private let writeIndex = Atomic<Int>(0)
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.pcm-writer")
    private let source: DispatchSourceUserDataOr
    private let finished = DispatchSemaphore(value: 0)
    private var failed = false // Storage failure; writer queue only.
    private var invalidInputReported = false // Writer queue only.
    private var overflowReported = false // Writer queue only.
    private var pendingFrames = 0 // Producer only, unpublished until a slot fills.
    private let lastCallback = Atomic<UInt64>(0)
    private let droppedFrames = Atomic<UInt64>(0)
    var callbackAge: TimeInterval? {
        let last = lastCallback.load(ordering: .relaxed)
        guard last != 0 else { return nil }
        let now = AudioGetCurrentHostTime()
        return AVAudioTime.seconds(forHostTime: now >= last ? now - last : 0)
    }
    var diagnosticSummary: String {
        let write = writeIndex.load(ordering: .acquiring)
        let queued = max(0, write - readIndex.load(ordering: .acquiring))
        return "file=\(diagnosticID) queued=\(queued)/\(slots.count) dropped=\(droppedFrames.load(ordering: .relaxed)) callbackAge=\(callbackAge ?? -1)"
    }
    private let meter = Atomic<UInt32>(0)
    var level: Float {
        guard let age = callbackAge, age < 0.5 else { return 0 }
        return Float(meter.load(ordering: .relaxed)) / 10000
    }
    var onError: (@Sendable (Error) -> Void)?
    var onWarning: (@Sendable (String) -> Void)?

    init(format: AVAudioFormat, url: URL, startTime: Double) throws {
        diagnosticID = url.lastPathComponent
        self.format = format
        file = PCMFile(url: url, startTime: startTime)
        // Pack small HAL callbacks into blocks instead of spending a whole slot
        // on each callback. Thirty seconds of stereo Float32 at 48 kHz is 11 MiB.
        let blockFrames = 4096
        let slotCount = Int(ceil(format.sampleRate * 30 / Double(blockFrames))) + 1
        slots = try (0..<slotCount).map { _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(blockFrames)) else {
                throw CaptureError.writerFailure("Could not allocate audio queue")
            }
            return Slot(buffer)
        }
        source = DispatchSource.makeUserDataOrSource(queue: queue)
        source.setEventHandler { [weak self] in
            guard let self else { return }
            let events = self.source.data
            if events & 2 != 0, !self.overflowReported {
                self.overflowReported = true
                CaptureDiagnostics.log("audio.frames.dropped \(self.diagnosticSummary)")
                self.onWarning?("The audio writer could not accept some frames. Recording continued; missing audio is silence.")
            }
            if events & 4 != 0, !self.invalidInputReported {
                self.invalidInputReported = true
                let error = CaptureError.writerFailure("Invalid audio buffer or timestamp")
                CaptureDiagnostics.error("audio.input.failed file=\(self.diagnosticID) \(error.localizedDescription)")
                self.onError?(error)
            }
            self.drain()
            if events & 8 != 0 {
                self.file.close()
                self.source.cancel()
                self.finished.signal()
            }
        }
        source.resume()
    }

    func enqueue(_ input: UnsafePointer<AudioBufferList>, timestamp: AudioTimeStamp) {
        let admission = inputState.compareExchange(expected: 0, desired: 1, ordering: .acquiring)
        guard admission.exchanged else {
            // A late callback cannot touch producer state after finish starts.
            // Concurrent callbacks drop input instead of racing over a slot.
            if admission.original == 1 {
                let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
                let bytesPerFrame = format.streamDescription.pointee.mBytesPerFrame
                if bytesPerFrame > 0, let bytes = buffers.first?.mDataByteSize {
                    let frames = bytes / bytesPerFrame
                    droppedFrames.wrappingAdd(UInt64(frames), ordering: .relaxed)
                }
                source.or(data: 2)
            }
            return
        }
        defer { inputState.bitwiseAnd(~UInt8(1), ordering: .releasing) }
        let incoming = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        // HAL advertises byte counts even when a disabled stream has no data.
        guard incoming.contains(where: { $0.mData != nil && $0.mDataByteSize > 0 }) else { return }
        guard timestamp.mFlags.contains(.hostTimeValid) else { source.or(data: 4); return }
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        let bytes = Int(incoming[0].mDataByteSize)
        let bufferCount = format.isInterleaved ? 1 : Int(format.channelCount)
        guard bytesPerFrame > 0, bytes % bytesPerFrame == 0, incoming.count == bufferCount else {
            source.or(data: 4); return
        }
        for channel in incoming.indices {
            guard incoming[channel].mDataByteSize == bytes else {
                source.or(data: 4); return
            }
        }
        let frames = bytes / bytesPerFrame
        lastCallback.store(AudioGetCurrentHostTime(), ordering: .relaxed)
        let time = AVAudioTime.seconds(forHostTime: timestamp.mHostTime)
        if pendingFrames > 0 {
            let slot = slots[writeIndex.load(ordering: .relaxed) % slots.count]
            let expected = AVAudioTime.seconds(forHostTime: slot.timestamp) + Double(pendingFrames) / format.sampleRate
            // Keep gaps and overlaps on their original clock positions.
            if abs(time - expected) * format.sampleRate > 0.5 { publishPending() }
        }
        var offset = 0
        while offset < frames {
            let write = writeIndex.load(ordering: .relaxed)
            guard write - readIndex.load(ordering: .acquiring) < slots.count else {
                // Never block HAL or poison queued audio. Later timestamps let
                // PCMFile fill the dropped interval with silence after recovery.
                droppedFrames.wrappingAdd(UInt64(frames - offset), ordering: .relaxed)
                source.or(data: 2); return
            }
            let slot = slots[write % slots.count]
            let buffer = slot.buffer
            if pendingFrames == 0 {
                slot.timestamp = timestamp.mHostTime + AVAudioTime.hostTime(forSeconds: Double(offset) / format.sampleRate)
            }
            let count = min(frames - offset, Int(buffer.frameCapacity) - pendingFrames)
            let destination = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            for channel in incoming.indices {
                let target = destination[channel].mData!.advanced(by: pendingFrames * bytesPerFrame)
                if let data = incoming[channel].mData {
                    target.copyMemory(from: data.advanced(by: offset * bytesPerFrame), byteCount: count * bytesPerFrame)
                } else {
                    target.initializeMemory(as: UInt8.self, repeating: 0, count: count * bytesPerFrame)
                }
            }
            pendingFrames += count
            offset += count
            if pendingFrames == Int(buffer.frameCapacity) { publishPending() }
        }
    }

    private func publishPending() {
        guard pendingFrames > 0 else { return }
        let write = writeIndex.load(ordering: .relaxed)
        slots[write % slots.count].buffer.frameLength = AVAudioFrameCount(pendingFrames)
        pendingFrames = 0
        writeIndex.store(write + 1, ordering: .releasing)
        source.or(data: 1)
    }

    private func drain() {
        while true {
            let read = readIndex.load(ordering: .relaxed)
            guard read < writeIndex.load(ordering: .acquiring) else { return }
            let slot = slots[read % slots.count]
            if !failed {
                do {
                    try file.write(slot.buffer, at: AVAudioTime.seconds(forHostTime: slot.timestamp))
                    meter.store(UInt32(Self.level(slot.buffer) * 10000), ordering: .relaxed)
                } catch { fail(CaptureError.storageFailure(CaptureError.details(error))) }
            }
            // This slot stays owned by the writer until disk I/O completes.
            readIndex.store(read + 1, ordering: .releasing)
        }
    }

    private func fail(_ error: Error) {
        guard !failed else { return }
        failed = true
        CaptureDiagnostics.error("audio.writer.failed file=\(diagnosticID) \(CaptureError.details(error))")
        onError?(error)
    }

    private static func level(_ buffer: AVAudioPCMBuffer) -> Float {
        let count = Int(buffer.frameLength) * (buffer.format.isInterleaved ? Int(buffer.format.channelCount) : 1)
        guard count > 0 else { return 0 }
        let buffers = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        var sum: Double = 0
        for channel in buffers {
            guard let data = channel.mData else { continue }
            for index in 0..<count {
                let value: Double
                switch buffer.format.commonFormat {
                case .pcmFormatFloat32: value = Double(data.assumingMemoryBound(to: Float.self)[index])
                case .pcmFormatFloat64: value = data.assumingMemoryBound(to: Double.self)[index]
                case .pcmFormatInt16: value = Double(data.assumingMemoryBound(to: Int16.self)[index]) / 32768
                case .pcmFormatInt32: value = Double(data.assumingMemoryBound(to: Int32.self)[index]) / 2147483648
                default: return 0
                }
                if value.isFinite { sum += value * value }
            }
        }
        return Float(min(1, sqrt(sum / Double(count * buffers.count)) * 4))
    }

    func finish() {
        // Stop accepting callbacks, then wait for any copy already in flight.
        // Only the source's control queue calls finish.
        guard inputState.bitwiseOr(2, ordering: .acquiringAndReleasing).oldValue < 2 else { return }
        while inputState.load(ordering: .acquiring) & 1 != 0 { Thread.sleep(forTimeInterval: 0.001) }
        publishPending()
        source.or(data: 8)
        finished.wait()
    }
}
