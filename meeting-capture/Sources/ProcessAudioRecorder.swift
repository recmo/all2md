@preconcurrency import AVFoundation
import CoreAudio
import CoreMedia
import Foundation
import Synchronization

// Private, unmuted taps capture process output without requiring a window.
final class ProcessAudioRecorder: @unchecked Sendable {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var formatListener: AudioObjectPropertyListenerBlock?
    private var writer: BufferedAudioWriter?
    private var deliveryError: Error?
    private var stopFailure: Error? // Control queue only.
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.system-audio")
    private var sampleAt: Date?
    // Read on the control thread, after callbacks are stopped or during capture.
    var diagnosticSummary: String { writer?.diagnosticSummary ?? "writer=none" }
    private var warning: String?
    var captureWarning: String? { lock.withLock { warning } }
    var firstSampleAt: Date? { lock.withLock { sampleAt } }
    var level: Float { writer?.level ?? 0 }
    var onError: (@Sendable (Error) -> Void)?

    func start(processObject: AudioObjectID, to url: URL, startTime: CMTime) throws {
        guard processObject != kAudioObjectUnknown else { throw CaptureError.triggeringApplicationUnavailable }
        try startCapture(description: CATapDescription(stereoMixdownOfProcesses: [processObject]),
                         to: url, startTime: startTime)
    }

    func startAuthorizationCheck(to url: URL) throws {
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
        CaptureDiagnostics.log("tap.start track=\(url.lastPathComponent) \(CaptureDiagnostics.devices())")
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
            CaptureDiagnostics.log("tap.format tap=\(tapID) format=\(format)")
            let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                guard let self else { return }
                // A new layout needs a new segment. Fail visibly rather than
                // interpreting callbacks using the previous device format.
                var current = AudioStreamBasicDescription()
                var currentSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
                var currentAddress = Self.formatAddress()
                let status = AudioObjectGetPropertyData(self.tapID, &currentAddress, 0, nil,
                                                        &currentSize, &current)
                CaptureDiagnostics.log("tap.format.changed tap=\(self.tapID) OSStatus=\(status) rate=\(current.mSampleRate) channels=\(current.mChannelsPerFrame) bytesPerFrame=\(current.mBytesPerFrame) flags=\(current.mFormatFlags)")
                if status != noErr || AVAudioFormat(streamDescription: &current)?.isEqual(format) != true {
                    self.reportFailure(CaptureError.processTapFailure("Audio process tap format changed."))
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
            let writer = try BufferedAudioWriter(format: format, url: url, startTime: startTime.seconds)
            writer.onSample = { [weak self] in
                guard let self else { return }
                self.lock.withLock { if self.sampleAt == nil { self.sampleAt = Date() } }
            }
            writer.onError = { [weak self] in self?.reportFailure($0) }
            writer.onWarning = { [weak self] message in
                guard let self else { return }
                self.lock.withLock { self.warning = message }
            }
            self.writer = writer
            lock.withLock { deliveryError = nil; sampleAt = nil; warning = nil }
            try check(AudioObjectAddPropertyListenerBlock(tapID, &address, queue, listener), "Watch tap format")
            formatListener = listener
            try check(AudioDeviceCreateIOProcIDWithBlock(&ioProcID, deviceID, queue) {
                _, input, timestamp, _, _ in
                writer.enqueue(input, timestamp: timestamp.pointee)
            }, "Create tap audio callback")
            try check(AudioDeviceStart(deviceID, ioProcID), "Start audio process tap")
        } catch {
            let startupError = error
            do { try stopCapture() }
            catch {
                if !CaptureError.isProcessTapFailure(error) { throw error }
            }
            throw startupError
        }
    }

    func stop(processExited: Bool = false) throws {
        do { try stopCapture() }
        catch where processExited && CaptureError.isProcessTapFailure(error) {
            // HAL can invalidate a tap when its process exits. Saved PCM is intact.
        }
    }

    // Release HAL resources before the control queue waits for any file writer.
    func stopInput() {
        if tapID != kAudioObjectUnknown || deviceID != kAudioObjectUnknown {
            CaptureDiagnostics.log("tap.stop tap=\(tapID) device=\(deviceID)")
        }
        // Stop callbacks before destroying their device or closing their file.
        var failure: Error?
        func cleanup(_ status: OSStatus, _ operation: String) {
            CaptureDiagnostics.log("tap.cleanup operation=\(operation) tap=\(tapID) device=\(deviceID) OSStatus=\(status)")
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
        stopFailure = stopFailure ?? failure
    }

    private func stopCapture() throws {
        stopInput()
        // Callbacks have stopped; retain every valid copied sample before close.
        writer?.finish()
        if let writer { CaptureDiagnostics.log("tap.finished \(writer.diagnosticSummary)") }
        writer = nil
        let failure = lock.withLock {
            defer { deliveryError = nil }
            return deliveryError ?? stopFailure
        }
        stopFailure = nil
        if let failure { throw failure }
    }

    private func reportFailure(_ error: Error) {
        let first = lock.withLock {
            // A storage failure must survive an earlier source or process error.
            if let previous = deliveryError {
                if case CaptureError.storageFailure = previous { return false }
                guard case CaptureError.storageFailure = error else { return false }
            }
            deliveryError = error
            return true
        }
        if first {
            CaptureDiagnostics.error("tap.failed \(CaptureError.details(error))")
            onError?(error)
        }
    }

    private static func formatAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                   mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    private func check(_ status: OSStatus, _ operation: String) throws {
        CaptureDiagnostics.log("tap.operation operation=\(operation) tap=\(tapID) device=\(deviceID) OSStatus=\(status)")
        guard status == noErr else {
            throw CaptureError.systemAudioCaptureFailure("\(operation) failed (OSStatus \(status))")
        }
    }
}
