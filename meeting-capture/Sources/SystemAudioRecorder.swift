@preconcurrency import AVFoundation
import CoreAudio
import CoreMedia
import Foundation

// Private, unmuted taps capture process output without requiring a window.
final class SystemAudioRecorder: @unchecked Sendable {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var formatListener: AudioObjectPropertyListenerBlock?
    private var writer: TapPCMWriter?
    private var deliveryError: Error?
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.system-audio")
    private var sampleAt: Date?
    var firstSampleAt: Date? { lock.withLock { sampleAt } }
    var onLevel: (@Sendable (Float) -> Void)?
    var onError: (@Sendable (Error) -> Void)?

    func start(processObject: AudioObjectID, to url: URL, startTime: CMTime) async throws {
        guard processObject != kAudioObjectUnknown else { throw CaptureError.triggeringApplicationUnavailable }
        try startCapture(description: CATapDescription(stereoMixdownOfProcesses: [processObject]),
                         to: url, startTime: startTime)
    }

    func startAuthorizationCheck(to url: URL) async throws {
        // The disposable check uses the system mix, excluding this recorder.
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                                                mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var pid = ProcessInfo.processInfo.processIdentifier
        var ownObject = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                            UInt32(MemoryLayout<pid_t>.size), &pid, &size, &ownObject),
                  "Find recorder audio process")
        let excluded = ownObject == kAudioObjectUnknown ? [] : [ownObject]
        try startCapture(description: CATapDescription(stereoGlobalTapButExcludeProcesses: excluded),
                         to: url, startTime: CMClockGetTime(CMClockGetHostTimeClock()))
    }

    private func startCapture(description: CATapDescription, to url: URL, startTime: CMTime) throws {
        description.name = "Meeting Capture audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        do {
            try check(AudioHardwareCreateProcessTap(description, &tapID), "Create audio process tap")
            var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                                    mScope: kAudioObjectPropertyScopeGlobal,
                                                    mElement: kAudioObjectPropertyElementMain)
            var asbd = AudioStreamBasicDescription()
            var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            try check(AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &asbd), "Read tap format")
            guard let format = AVAudioFormat(streamDescription: &asbd),
                  format.sampleRate > 0, format.channelCount > 0,
                  format.commonFormat != .otherFormat else {
                throw CaptureError.writerFailure("Invalid audio process tap format")
            }
            let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                guard let self else { return }
                // A new layout needs a new segment. Fail visibly rather than
                // interpreting callbacks using the previous device format.
                var current = AudioStreamBasicDescription()
                var currentSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
                var currentAddress = Self.formatAddress()
                let status = AudioObjectGetPropertyData(self.tapID, &currentAddress, 0, nil,
                                                        &currentSize, &current)
                if status != noErr || AVAudioFormat(streamDescription: &current)?.isEqual(format) != true {
                    self.reportFailure(CaptureError.processTapFailure("Audio process tap format changed; restart recording."))
                }
            }
            let aggregate: [String: Any] = [
                kAudioAggregateDeviceNameKey: "Meeting Capture audio tap",
                kAudioAggregateDeviceUIDKey: UUID().uuidString,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceTapListKey: [[
                    kAudioSubTapUIDKey: description.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]],
            ]
            try check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &deviceID), "Create tap device")
            let writer = try TapPCMWriter(format: format, url: url, startTime: startTime.seconds)
            writer.onSample = { [weak self] in
                guard let self else { return }
                self.lock.withLock { if self.sampleAt == nil { self.sampleAt = Date() } }
                self.onLevel?(0.35)
            }
            writer.onError = { [weak self] in self?.reportFailure($0) }
            self.writer = writer
            lock.withLock { deliveryError = nil; sampleAt = nil }
            try check(AudioObjectAddPropertyListenerBlock(tapID, &address, queue, listener), "Watch tap format")
            formatListener = listener
            try check(AudioDeviceCreateIOProcIDWithBlock(&ioProcID, deviceID, queue) {
                _, input, timestamp, _, _ in
                writer.enqueue(input, timestamp: timestamp.pointee)
            }, "Create tap audio callback")
            try check(AudioDeviceStart(deviceID, ioProcID), "Start audio process tap")
        } catch {
            try? stopCapture()
            throw error
        }
    }

    func stop(processExited: Bool = false) async throws {
        do { try stopCapture() }
        catch where processExited && CaptureError.isProcessTapFailure(error) {
            // HAL can invalidate a tap when its process exits. Saved PCM is intact.
        }
    }

    private func stopCapture() throws {
        // Stop callbacks before destroying their device or closing their file.
        var failure: Error?
        func cleanup(_ status: OSStatus, _ operation: String) {
            if status != noErr, failure == nil {
                failure = CaptureError.processTapFailure("\(operation) (OSStatus \(status))")
            }
        }
        if let formatListener {
            var address = Self.formatAddress()
            cleanup(AudioObjectRemovePropertyListenerBlock(tapID, &address, queue, formatListener), "Remove tap format observer")
            self.formatListener = nil
            queue.sync {}
        }
        if let ioProcID {
            cleanup(AudioDeviceStop(deviceID, ioProcID), "Stop tap")
            cleanup(AudioDeviceDestroyIOProcID(deviceID, ioProcID), "Destroy tap callback")
            self.ioProcID = nil
            queue.sync {}
        }
        if deviceID != kAudioObjectUnknown {
            cleanup(AudioHardwareDestroyAggregateDevice(deviceID), "Destroy tap device")
            deviceID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            cleanup(AudioHardwareDestroyProcessTap(tapID), "Destroy process tap")
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        // The callback has stopped; drain copied samples before closing the file.
        writer?.finish()
        writer = nil
        lock.withLock {
            failure = deliveryError ?? failure
            deliveryError = nil
        }
        if let failure { throw failure }
    }

    private func reportFailure(_ error: Error) {
        let first = lock.withLock {
            // A real writer failure must survive a concurrent process-exit error.
            if let previous = deliveryError,
               !CaptureError.isProcessTapFailure(previous) || CaptureError.isProcessTapFailure(error) { return false }
            deliveryError = error
            return true
        }
        if first { onError?(error) }
    }

    private static func formatAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                   mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    private func check(_ status: OSStatus, _ operation: String) throws {
        guard status == noErr else {
            throw CaptureError.systemAudioCaptureFailure("\(operation) failed (OSStatus \(status))")
        }
    }
}

// HAL invokes IO blocks synchronously. Copy into preallocated slots and return;
// only this separate queue may allocate PCMFile buffers or touch the filesystem.
final class TapPCMWriter: @unchecked Sendable {
    private let file: PCMFile
    private let buffers: [AVAudioPCMBuffer]
    private var timestamps: [UInt64]
    private var readIndex = 0
    private var writeIndex = 0
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.pcm-writer")
    private let source: DispatchSourceUserDataOr
    private var failed = false // Writer queue only.
    var onSample: (@Sendable () -> Void)?
    var onError: (@Sendable (Error) -> Void)?

    init(format: AVAudioFormat, url: URL, startTime: Double) throws {
        file = PCMFile(url: url, startTime: startTime)
        // Bounded storage: 32 callbacks of up to 8192 frames each.
        buffers = try (0..<32).map { _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 8192) else {
                throw CaptureError.writerFailure("Could not allocate process audio queue")
            }
            return buffer
        }
        timestamps = Array(repeating: 0, count: buffers.count)
        source = DispatchSource.makeUserDataOrSource(queue: queue)
        source.setEventHandler { [weak self] in
            guard let self else { return }
            let events = self.source.data
            if events & 2 != 0 { self.fail("Process audio queue overflow; audio could not be saved fast enough") }
            if events & 4 != 0 { self.fail("Invalid process audio buffer or timestamp") }
            self.drain()
        }
        source.resume()
    }

    func enqueue(_ input: UnsafePointer<AudioBufferList>, timestamp: AudioTimeStamp) {
        let incoming = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard incoming.contains(where: { $0.mDataByteSize > 0 }) else { return }
        guard timestamp.mFlags.contains(.hostTimeValid) else { source.or(data: 4); return }
        // Never wait on the writer, even while it briefly advances a ring index.
        guard lock.try() else { source.or(data: 2); return }
        defer { lock.unlock() }
        guard writeIndex - readIndex < buffers.count else { source.or(data: 2); return }
        let index = writeIndex % buffers.count
        let buffer = buffers[index]
        let bytesPerFrame = Int(buffer.format.streamDescription.pointee.mBytesPerFrame)
        let destination = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        let bytes = Int(incoming[0].mDataByteSize)
        guard bytesPerFrame > 0, bytes % bytesPerFrame == 0,
              bytes / bytesPerFrame <= Int(buffer.frameCapacity), incoming.count == destination.count else {
            source.or(data: 4); return
        }
        for channel in incoming.indices {
            guard incoming[channel].mDataByteSize == bytes,
                  let from = incoming[channel].mData, let to = destination[channel].mData else {
                source.or(data: 4); return
            }
            to.copyMemory(from: from, byteCount: bytes)
        }
        buffer.frameLength = AVAudioFrameCount(bytes / bytesPerFrame)
        timestamps[index] = timestamp.mHostTime
        writeIndex += 1
        source.or(data: 1)
    }

    private func drain() {
        while let index = lock.withLock({ readIndex < writeIndex ? readIndex % buffers.count : nil }) {
            if !failed {
                do {
                    try file.write(buffers[index], at: AVAudioTime.seconds(forHostTime: timestamps[index]))
                    onSample?()
                } catch { fail("Process tap PCM write: \(CaptureError.details(error))") }
            }
            // This slot stays owned by the writer until disk I/O completes.
            lock.withLock { readIndex += 1 }
        }
    }

    private func fail(_ message: String) {
        guard !failed else { return }
        failed = true
        onError?(CaptureError.writerFailure(message))
    }

    func finish() {
        queue.sync {
            // Read pending flags too: a final overflow may precede stop().
            let events = source.data
            if events & 2 != 0 { fail("Process audio queue overflow; audio could not be saved fast enough") }
            if events & 4 != 0 { fail("Invalid process audio buffer or timestamp") }
            drain()
            file.close()
            source.cancel()
        }
    }
}

// Both capture paths use the same timestamp-aware PCM writer. Callers serialize
// access on their delivery queue or lock; the file always stores interleaved PCM.
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
