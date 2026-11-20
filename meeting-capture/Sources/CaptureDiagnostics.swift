import CoreAudio
import Foundation
import OSLog
import Synchronization

// Call only from control/writer queues, never from an audio callback.
enum CaptureDiagnostics {
    private static let logger = Logger(subsystem: "ventures.wicked.MeetingCapture", category: "audio")

    static func log(_ message: String) { logger.notice("\(message, privacy: .public)") }
    static func error(_ message: String) { logger.error("\(message, privacy: .public)") }

    static func devices() -> String {
        "defaultInput={\(device(defaultDevice(kAudioHardwarePropertyDefaultInputDevice)))} " +
        "defaultOutput={\(device(defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)))}"
    }

    static func device(_ id: AudioDeviceID) -> String {
        guard id != kAudioObjectUnknown else { return "none" }
        var uid: Unmanaged<CFString>?
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID,
                                                mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &uid)
        let identity = status == noErr ? (uid?.takeRetainedValue() as String? ?? "unknown") : "OSStatus:\(status)"
        var rate: Float64 = 0
        var frames: UInt32 = 0
        var alive: UInt32 = 0
        let rateStatus = read(id, kAudioDevicePropertyNominalSampleRate, into: &rate)
        let framesStatus = read(id, kAudioDevicePropertyBufferFrameSize, into: &frames)
        let aliveStatus = read(id, kAudioDevicePropertyDeviceIsAlive, into: &alive)
        return "id=\(id) uid=\(identity) rate=\(rate) rateStatus=\(rateStatus) bufferFrames=\(frames) bufferStatus=\(framesStatus) alive=\(alive) aliveStatus=\(aliveStatus)"
    }

    private static func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID {
        var id = AudioDeviceID(kAudioObjectUnknown)
        let status = read(AudioObjectID(kAudioObjectSystemObject), selector, into: &id)
        if status != noErr { error("device.default selector=\(selector) OSStatus=\(status)") }
        return id
    }

    private static func read<T: BitwiseCopyable>(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector, into value: inout T) -> OSStatus {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<T>.size)
        return AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value)
    }
}

// Audio callbacks update only atomic integers; formatting/logging happens later.
// Counters are cumulative within a capture segment, not a measure of audibility.
final class AudioCallbackStatistics: @unchecked Sendable {
    private let callbacks = Atomic<UInt64>(0)
    private let frames = Atomic<UInt64>(0)
    private let lastCallback = Atomic<UInt64>(0)
    private let lastSample = Atomic<UInt64>(0)

    func record(frames count: UInt32, hostTime: UInt64) {
        callbacks.wrappingAdd(1, ordering: .relaxed)
        frames.wrappingAdd(UInt64(count), ordering: .relaxed)
        lastSample.store(hostTime, ordering: .relaxed)
        lastCallback.store(AudioGetCurrentHostTime(), ordering: .relaxed)
    }

    func reset() {
        callbacks.store(0, ordering: .relaxed)
        frames.store(0, ordering: .relaxed)
        lastSample.store(0, ordering: .relaxed)
        lastCallback.store(0, ordering: .relaxed)
    }

    var summary: String {
        let last = lastCallback.load(ordering: .relaxed)
        let now = AudioGetCurrentHostTime()
        let age = last == 0 ? "none" : String(format: "%.3f", Double(AudioConvertHostTimeToNanos(now >= last ? now - last : 0)) / 1e9)
        return "callbacks=\(callbacks.load(ordering: .relaxed)) frames=\(frames.load(ordering: .relaxed)) lastCallbackAgeSeconds=\(age) lastSampleHostTime=\(lastSample.load(ordering: .relaxed))"
    }
}
