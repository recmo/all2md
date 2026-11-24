import AppKit
@preconcurrency import CoreAudio
import Darwin
import Foundation

@MainActor
final class AudioActivityMonitor {
    var onClientsChanged: (([AudioClient]) -> Void)?
    private var timer: Timer?
    private var lastClients: [AudioClient]?
    private var lastPollError: String?
    private let ownPID = ProcessInfo.processInfo.processIdentifier

    func start() {
        guard timer == nil else { return }
        CaptureDiagnostics.log("monitor.start")
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
    }

    func stop() {
        CaptureDiagnostics.log("monitor.stop")
        timer?.invalidate()
        timer = nil
        lastClients = nil
    }

    private func poll() {
        let objectIDs: [AudioObjectID]
        do {
            objectIDs = try processObjectIDs()
            if lastPollError != nil { CaptureDiagnostics.log("monitor.poll.recovered") }
            lastPollError = nil
        } catch {
            let message = CaptureError.details(error)
            if message != lastPollError { CaptureDiagnostics.error("monitor.poll.failed \(message)") }
            lastPollError = message
            return
        }
        var seen = Set<pid_t>()
        let clients = objectIDs
            .compactMap { audioClient(for: $0) }
            .filter { $0.processID != ownPID && seen.insert($0.processID).inserted }
            .sorted { $0.applicationName.localizedStandardCompare($1.applicationName) == .orderedAscending }
        if clients != lastClients {
            let details = clients.map { "pid=\($0.processID) object=\($0.audioObjectID) app=\($0.applicationName) inputs=\($0.inputDeviceSummary)" }.joined(separator: "; ")
            CaptureDiagnostics.log("monitor.inputs [\(details)]")
            lastClients = clients
        }
        onClientsChanged?(clients)
    }

    private func processObjectIDs() throws -> [AudioObjectID] {
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

    private func audioClient(for objectID: AudioObjectID) -> AudioClient? {
        guard uint32Property(kAudioProcessPropertyIsRunningInput, objectID: objectID) == 1,
              let pidValue = uint32Property(kAudioProcessPropertyPID, objectID: objectID) else { return nil }
        let bundleID = stringProperty(kAudioProcessPropertyBundleID, objectID: objectID)
        let app = ProcessApplicationResolver.resolve(processID: pid_t(pidValue), bundleID: bundleID)
        return AudioClient(
            audioObjectID: objectID,
            processID: app?.processIdentifier ?? pid_t(pidValue),
            bundleID: app?.bundleIdentifier ?? bundleID,
            applicationName: app?.localizedName ?? "Process \(pidValue)",
            inputDevices: inputDevices(processObjectID: objectID)
        )
    }

    private func inputDevices(processObjectID: AudioObjectID) -> [AudioInputDevice] {
        var address = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyDevices,
                                                mScope: kAudioObjectPropertyScopeInput,
                                                mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(processObjectID, &address, 0, nil, &size)
        guard sizeStatus == noErr else { return [] }
        var deviceIDs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(processObjectID, &address, 0, nil, &size, &deviceIDs)
        guard status == noErr else { return [] }
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

}

@MainActor
enum ProcessApplicationResolver {
    static func resolve(processID: pid_t, bundleID: String?) -> NSRunningApplication? {
        var current = processID
        for _ in 0..<12 where current > 1 {
            if let app = NSRunningApplication(processIdentifier: current), app.activationPolicy != .prohibited { return app }
            var info = proc_bsdinfo()
            let size = MemoryLayout<proc_bsdinfo>.size
            guard proc_pidinfo(current, PROC_PIDTBSDINFO, 0, &info, Int32(size)) == size else { break }
            current = pid_t(info.pbi_ppid)
        }
        return NSWorkspace.shared.runningApplications.first {
            guard $0.activationPolicy != .prohibited, let owner = $0.bundleIdentifier, let bundleID else { return false }
            return owner == bundleID || bundleID.hasPrefix(owner + ".")
        }
    }
}
