import AppKit
@preconcurrency import CoreAudio
import Foundation

@MainActor
final class AudioActivityMonitor {
    var onClientsChanged: (([AudioClient]) -> Void)?
    private var timer: Timer?
    private var deviceListeners: [AudioObjectID: AudioObjectPropertyListenerBlock] = [:]
    private let ownPID = ProcessInfo.processInfo.processIdentifier

    func start() {
        guard timer == nil else { return }
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        for (objectID, listener) in deviceListeners { removeDeviceListener(listener, from: objectID) }
        deviceListeners.removeAll()
    }

    private func poll() {
        let objectIDs = processObjectIDs()
        synchronizeDeviceListeners(for: objectIDs)
        let clients = objectIDs.compactMap(audioClient).filter { $0.processID != ownPID }
        if clients.isEmpty, defaultInputIsRunning() {
            let device = defaultInputDevice().flatMap(audioInputDevice)
            onClientsChanged?([AudioClient(
                audioObjectID: 0,
                processID: 0,
                bundleID: nil,
                applicationName: "Unknown microphone client",
                inputDevices: device.map { [$0] } ?? []
            )])
        } else {
            onClientsChanged?(clients)
        }
    }

    private func synchronizeDeviceListeners(for objectIDs: [AudioObjectID]) {
        let currentIDs = Set(objectIDs)
        let staleIDs = deviceListeners.keys.filter { !currentIDs.contains($0) }
        for objectID in staleIDs {
            guard let listener = deviceListeners.removeValue(forKey: objectID) else { continue }
            removeDeviceListener(listener, from: objectID)
        }
        for objectID in objectIDs where deviceListeners[objectID] == nil {
            var address = devicePropertyAddress()
            let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                Task { @MainActor in self?.poll() }
            }
            if AudioObjectAddPropertyListenerBlock(objectID, &address, .main, listener) == noErr {
                deviceListeners[objectID] = listener
            }
        }
    }

    private func removeDeviceListener(_ listener: @escaping AudioObjectPropertyListenerBlock, from objectID: AudioObjectID) {
        var address = devicePropertyAddress()
        AudioObjectRemovePropertyListenerBlock(objectID, &address, .main, listener)
    }

    private func devicePropertyAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyDevices,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private func processObjectIDs() -> [AudioObjectID] {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
        var values = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &values) == noErr else { return [] }
        return values
    }

    private func audioClient(for objectID: AudioObjectID) -> AudioClient? {
        guard uint32Property(kAudioProcessPropertyIsRunningInput, objectID: objectID) == 1,
              let pidValue = pidProperty(objectID: objectID) else { return nil }
        let app = NSRunningApplication(processIdentifier: pidValue)
        return AudioClient(
            audioObjectID: objectID,
            processID: pidValue,
            bundleID: app?.bundleIdentifier,
            applicationName: app?.localizedName ?? "Process \(pidValue)",
            inputDevices: inputDevices(processObjectID: objectID)
        )
    }

    private func inputDevices(processObjectID: AudioObjectID) -> [AudioInputDevice] {
        var address = devicePropertyAddress()
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(processObjectID, &address, 0, nil, &size) == noErr else { return [] }
        var deviceIDs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(processObjectID, &address, 0, nil, &size, &deviceIDs) == noErr else { return [] }
        return deviceIDs.compactMap(audioInputDevice)
    }

    private func audioInputDevice(_ deviceID: AudioDeviceID) -> AudioInputDevice? {
        guard deviceID != kAudioObjectUnknown else { return nil }
        let name = stringProperty(kAudioObjectPropertyName, objectID: deviceID) ?? "Audio device \(deviceID)"
        let uid = stringProperty(kAudioDevicePropertyDeviceUID, objectID: deviceID)
        return AudioInputDevice(id: deviceID, uid: uid, name: name)
    }

    private func stringProperty(_ selector: AudioObjectPropertySelector, objectID: AudioObjectID) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value?.takeRetainedValue() as String?
    }

    private func uint32Property(_ selector: AudioObjectPropertySelector, objectID: AudioObjectID) -> UInt32? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) == noErr ? value : nil
    }

    private func pidProperty(objectID: AudioObjectID) -> pid_t? {
        var address = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyPID, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value = pid_t(0)
        var size = UInt32(MemoryLayout<pid_t>.size)
        return AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) == noErr ? value : nil
    }

    private func defaultInputIsRunning() -> Bool {
        guard let device = defaultInputDevice() else { return false }
        var runningAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var running: UInt32 = 0
        var runningSize = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(device, &runningAddress, 0, nil, &runningSize, &running) == noErr && running != 0
    }

    private func defaultInputDevice() -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr,
              device != kAudioObjectUnknown else { return nil }
        return device
    }
}
