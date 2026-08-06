import AppKit
import CoreAudio
import Darwin
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
        var seen = Set<pid_t>()
        let clients = processObjectIDs()
            .compactMap(audioClient)
            .filter { $0.processID != ownPID && seen.insert($0.processID).inserted }
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
        let bundleID = bundleIDProperty(objectID: objectID)
        let app = ProcessApplicationResolver.resolve(processID: pidValue, bundleID: bundleID)
        return AudioClient(
            audioObjectID: objectID,
            processID: app?.processID ?? pidValue,
            bundleID: app?.bundleID ?? bundleID,
            applicationName: app?.applicationName ?? "Process \(pidValue)"
        )
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
