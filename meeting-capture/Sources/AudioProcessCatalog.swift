@preconcurrency import CoreAudio
import Foundation

// No observers or UI state: a snapshot read on the capture control queue.
struct AudioProcessCatalog {
    func outputProcesses() throws -> (all: Set<UInt32>, active: [AudioProcess]) {
        let objects = try processObjectIDs()
        let ownPID = ProcessInfo.processInfo.processIdentifier
        let active = objects.compactMap { objectID -> AudioProcess? in
            guard uint32(kAudioProcessPropertyIsRunningOutput, objectID) == 1,
                  let rawPID = uint32(kAudioProcessPropertyPID, objectID), rawPID > 0, rawPID <= UInt32(Int32.max),
                  Int32(rawPID) != ownPID else { return nil }
            return AudioProcess(objectID: objectID, processID: Int32(rawPID),
                bundleID: string(kAudioProcessPropertyBundleID, objectID),
                name: string(kAudioObjectPropertyName, objectID) ?? "Process \(rawPID)")
        }.sorted { $0.processID < $1.processID }
        return (Set(objects), active)
    }

    func processObjectIDs() throws -> [AudioObjectID] {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size)
        guard sizeStatus == noErr else {
            throw CaptureError.writerFailure("Cannot enumerate audio processes (OSStatus \(sizeStatus))")
        }
        var values = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &values)
        guard status == noErr else {
            throw CaptureError.writerFailure("Cannot read audio processes (OSStatus \(status))")
        }
        values = Array(values.prefix(Int(size) / MemoryLayout<AudioObjectID>.size))
        return values
    }

    private func uint32(_ selector: AudioObjectPropertySelector, _ object: AudioObjectID) -> UInt32? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }

    private func string(_ selector: AudioObjectPropertySelector, _ object: AudioObjectID) -> String? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value?.takeRetainedValue() as String?
    }
}
