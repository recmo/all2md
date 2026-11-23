@preconcurrency import ApplicationServices
import Foundation

// Optional metadata must impose very little work on the meeting application.
// Read only its focused-window title, with a short per-message deadline.
@MainActor
final class AccessibilityProbe {
    private let application: AXUIElement
    private let handle: FileHandle
    private var timer: Timer?
    private var title: String?
    private var stopped = false

    init(client: AudioClient, outputURL: URL) throws {
        guard client.processID > 0 else { throw ProbeError.invalidProcess }
        guard AXIsProcessTrusted() else { throw ProbeError.accessibilityPermissionRequired }
        application = AXUIElementCreateApplication(client.processID)
        AXUIElementSetMessagingTimeout(application, 0.2)
        guard FileManager.default.createFile(atPath: outputURL.path, contents: nil) else {
            throw CaptureError.writerFailure("Could not create optional metadata log")
        }
        handle = try FileHandle(forWritingTo: outputURL)
        title = windowTitle()
        write([
            "type": "probeStarted", "schemaVersion": 1,
            "windowTitle": title.map { $0 as Any } ?? NSNull(),
            "process": ["pid": Int(client.processID),
                        "bundleID": client.bundleID.map { $0 as Any } ?? NSNull(),
                        "applicationName": client.applicationName],
        ])
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
    }

    func stop() {
        guard !stopped else { return }
        stopped = true
        timer?.invalidate()
        timer = nil
        write(["type": "probeStopped"])
        try? handle.synchronize()
        try? handle.close()
    }

    private func poll() {
        guard !stopped, let next = windowTitle(), next != title else { return }
        title = next
        write(["type": "windowTitle", "windowTitle": next])
    }

    private func windowTitle() -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(application, kAXFocusedWindowAttribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        let window = unsafeDowncast(value, to: AXUIElement.self)
        AXUIElementSetMessagingTimeout(window, 0.2)
        var title: CFTypeRef?
        guard AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &title) == .success else { return nil }
        return title as? String
    }

    private func write(_ fields: [String: Any]) {
        var record = fields
        record["timestamp"] = ISO8601DateFormatter().string(from: Date())
        guard var data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) else { return }
        data.append(0x0A)
        // Metadata is optional. Its failure never interrupts the audio writer.
        try? handle.write(contentsOf: data)
    }
}

enum ProbeError: LocalizedError {
    case invalidProcess
    case accessibilityPermissionRequired

    var errorDescription: String? {
        switch self {
        case .invalidProcess: "The meeting application is no longer running."
        case .accessibilityPermissionRequired: "Accessibility permission is required for optional window titles."
        }
    }
}
