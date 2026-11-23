import AppKit
@preconcurrency import CoreAudio
import Darwin
import Foundation

@MainActor
final class AudioActivityMonitor {
    var onClientsChanged: (([AudioClient]) -> Void)?
    private var timer: Timer?
    private var deviceListeners: [AudioObjectID: AudioObjectPropertyListenerBlock] = [:]
    private var lastClients: [AudioClient]?
    private var lastPollError: String?
    private var propertyErrors: [AudioObjectID: [String: OSStatus]] = [:]
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
        for (objectID, listener) in deviceListeners { removeDeviceListener(listener, from: objectID) }
        deviceListeners.removeAll()
        lastClients = nil
        propertyErrors.removeAll()
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
        synchronizeDeviceListeners(for: objectIDs)
        let clients = runningClients(from: objectIDs, selector: kAudioProcessPropertyIsRunningInput)
        if clients != lastClients {
            let details = clients.map { "pid=\($0.processID) object=\($0.audioObjectID) app=\($0.applicationName) inputs=\($0.inputDeviceSummary)" }.joined(separator: "; ")
            CaptureDiagnostics.log("monitor.inputs [\(details)]")
            lastClients = clients
        }
        onClientsChanged?(clients)
    }

    private func runningClients(
        from objectIDs: [AudioObjectID],
        selector: AudioObjectPropertySelector
    ) -> [AudioClient] {
        var seen = Set<pid_t>()
        return objectIDs
            .compactMap { audioClient(for: $0, runningSelector: selector) }
            .filter { $0.processID != ownPID && seen.insert($0.processID).inserted }
            .sorted { $0.applicationName.localizedStandardCompare($1.applicationName) == .orderedAscending }
    }

    private func synchronizeDeviceListeners(for objectIDs: [AudioObjectID]) {
        let currentIDs = Set(objectIDs)
        let staleIDs = deviceListeners.keys.filter { !currentIDs.contains($0) }
        for objectID in staleIDs {
            guard let listener = deviceListeners.removeValue(forKey: objectID) else { continue }
            removeDeviceListener(listener, from: objectID)
        }
        propertyErrors = propertyErrors.filter { currentIDs.contains($0.key) }
        for objectID in objectIDs where deviceListeners[objectID] == nil {
            var address = devicePropertyAddress()
            let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                Task { @MainActor in self?.poll() }
            }
            let status = AudioObjectAddPropertyListenerBlock(objectID, &address, .main, listener)
            if propertyStatus(status, operation: "watch-input-devices", objectID: objectID) {
                deviceListeners[objectID] = listener
            }
        }
    }

    private func removeDeviceListener(_ listener: @escaping AudioObjectPropertyListenerBlock, from objectID: AudioObjectID) {
        var address = devicePropertyAddress()
        _ = propertyStatus(AudioObjectRemovePropertyListenerBlock(objectID, &address, .main, listener),
                           operation: "unwatch-input-devices", objectID: objectID)
    }

    private func devicePropertyAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyDevices,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private func processObjectIDs() throws -> [AudioObjectID] { try AudioProcessCatalog().processObjectIDs() }

    private func audioClient(
        for objectID: AudioObjectID,
        runningSelector: AudioObjectPropertySelector
    ) -> AudioClient? {
        guard uint32Property(runningSelector, objectID: objectID) == 1,
              let pidValue = pidProperty(objectID: objectID) else { return nil }
        let bundleID = bundleIDProperty(objectID: objectID)
        let app = ProcessApplicationResolver.resolve(processID: pidValue, bundleID: bundleID)
        return AudioClient(
            audioObjectID: objectID,
            processID: app?.processID ?? pidValue,
            bundleID: app?.bundleID ?? bundleID,
            applicationName: app?.applicationName ?? "Process \(pidValue)",
            inputDevices: inputDevices(processObjectID: objectID)
        )
    }

    private func inputDevices(processObjectID: AudioObjectID) -> [AudioInputDevice] {
        var address = devicePropertyAddress()
        var size: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(processObjectID, &address, 0, nil, &size)
        guard propertyStatus(sizeStatus, operation: "input-device-count", objectID: processObjectID) else { return [] }
        var deviceIDs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(processObjectID, &address, 0, nil, &size, &deviceIDs)
        guard propertyStatus(status, operation: "input-devices", objectID: processObjectID) else { return [] }
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

    private func bundleIDProperty(objectID: AudioObjectID) -> String? {
        var address = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyBundleID, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) == noErr,
              let value else { return nil }
        return value.takeRetainedValue() as String
    }

    // A vanished or unsupported process object must not flood the log at 2 Hz.
    private func propertyStatus(_ status: OSStatus, operation: String, objectID: AudioObjectID) -> Bool {
        let previous = propertyErrors[objectID]?[operation]
        if status == noErr {
            propertyErrors[objectID]?[operation] = nil
            if previous != nil { CaptureDiagnostics.log("monitor.property.recovered \(operation) object=\(objectID)") }
            return true
        }
        if previous != status { CaptureDiagnostics.error("monitor.property.failed \(operation) object=\(objectID) OSStatus=\(status)") }
        propertyErrors[objectID, default: [:]][operation] = status
        return false
    }
}

enum ProcessApplicationResolver {
    static func resolve(processID: pid_t, bundleID: String?) -> CaptureApplicationIdentity? {
        let applications = NSWorkspace.shared.runningApplications.map {
            CaptureApplicationIdentity(
                processID: $0.processIdentifier,
                bundleID: $0.bundleIdentifier,
                applicationName: $0.localizedName ?? "Process \($0.processIdentifier)",
                isUserApplication: $0.activationPolicy != .prohibited
            )
        }
        return resolve(
            processID: processID,
            bundleID: bundleID,
            applications: applications,
            parentPID: parentPID
        )
    }

    static func resolve(
        processID: pid_t,
        bundleID: String?,
        applications: [CaptureApplicationIdentity],
        parentPID: (pid_t) -> pid_t?
    ) -> CaptureApplicationIdentity? {
        let byPID = Dictionary(uniqueKeysWithValues: applications.map { ($0.processID, $0) })
        var current = processID
        var visited = Set<pid_t>()
        var processFallback: CaptureApplicationIdentity?

        while current > 1, visited.insert(current).inserted, visited.count <= 12 {
            if let application = byPID[current] {
                processFallback = processFallback ?? application
                if application.isUserApplication { return application }
            }
            guard let parent = parentPID(current) else { break }
            current = parent
        }

        if let bundleID {
            if let exact = applications.first(where: { $0.isUserApplication && $0.bundleID == bundleID }) {
                return exact
            }
            if let owner = applications.first(where: {
                guard $0.isUserApplication, let candidate = $0.bundleID else { return false }
                return bundleID.hasPrefix(candidate + ".")
            }) {
                return owner
            }
        }
        return processFallback
    }

    private static func parentPID(_ processID: pid_t) -> pid_t? {
        var info = proc_bsdinfo()
        let size = MemoryLayout<proc_bsdinfo>.size
        guard proc_pidinfo(processID, PROC_PIDTBSDINFO, 0, &info, Int32(size)) == size,
              info.pbi_ppid > 0 else { return nil }
        return pid_t(info.pbi_ppid)
    }
}
