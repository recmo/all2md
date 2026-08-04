import AppKit
import CoreAudio
import Foundation

@MainActor
final class AudioActivityMonitor {
    var onClientsChanged: (([AudioClient]) -> Void)?
    private var timer: Timer?
    private let ownPID = ProcessInfo.processInfo.processIdentifier

    func start() {
        guard timer == nil else { return }
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
    }

    func stop() { timer?.invalidate(); timer = nil }

    private func poll() {
        let clients = processObjectIDs().compactMap(audioClient).filter { $0.processID != ownPID }
        if clients.isEmpty, defaultInputIsRunning() {
            onClientsChanged?([AudioClient(audioObjectID: 0, processID: 0, bundleID: nil, applicationName: "Unknown microphone client")])
        } else {
            onClientsChanged?(clients)
        }
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
        return AudioClient(audioObjectID: objectID, processID: pidValue, bundleID: app?.bundleIdentifier, applicationName: app?.localizedName ?? "Process \(pidValue)")
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
        var defaultAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var device = AudioDeviceID(0)
        var deviceSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &defaultAddress, 0, nil, &deviceSize, &device) == noErr else { return false }
        var runningAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var running: UInt32 = 0
        var runningSize = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(device, &runningAddress, 0, nil, &runningSize, &running) == noErr && running != 0
    }
}
