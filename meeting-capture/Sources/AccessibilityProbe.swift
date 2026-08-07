@preconcurrency import ApplicationServices
import AppKit
import Foundation

struct AccessibilitySnapshotNode: Codable, Equatable, Sendable {
    let path: String
    let attributes: [String: String]
}

struct AccessibilityAttributeChange: Codable, Equatable, Sendable {
    let path: String
    let attribute: String
    let before: String?
    let after: String?
}

struct AccessibilityTreeDiff: Codable, Equatable, Sendable {
    let added: [AccessibilitySnapshotNode]
    let removed: [String]
    let changed: [AccessibilityAttributeChange]

    var isEmpty: Bool { added.isEmpty && removed.isEmpty && changed.isEmpty }
}

private func accessibilityProbeCallback(
    _ observer: AXObserver,
    _ element: AXUIElement,
    _ notification: CFString,
    _ reference: UnsafeMutableRawPointer?
) {
    guard let reference else { return }
    let probe = Unmanaged<AccessibilityProbe>.fromOpaque(reference).takeUnretainedValue()
    MainActor.assumeIsolated {
        probe.receive(notification: notification as String, element: element)
    }
}

@MainActor
final class AccessibilityProbe {
    private struct Snapshot {
        let nodes: [AccessibilitySnapshotNode]
        let truncated: Bool
    }

    let outputURL: URL
    let applicationName: String

    private let processID: pid_t
    private let applicationElement: AXUIElement
    private let maximumDepth = 24
    private let maximumNodes = 3_000
    private var observer: AXObserver?
    private var fileHandle: FileHandle?
    private var previousSnapshot: Snapshot?
    private var observedElementHashes = Set<CFHashCode>()
    private var pendingSnapshot: Task<Void, Never>?
    private var fallbackTimer: Timer?
    private var stopped = false

    init(client: AudioClient, outputURL: URL) throws {
        guard client.processID > 0 else {
            throw ProbeError.invalidProcess
        }
        let options = ["AXTrustedCheckOptionPrompt": false] as CFDictionary
        guard AXIsProcessTrustedWithOptions(options) else {
            throw ProbeError.accessibilityPermissionRequired
        }

        processID = client.processID
        applicationName = client.applicationName
        applicationElement = AXUIElementCreateApplication(client.processID)
        self.outputURL = outputURL
        _ = FileManager.default.createFile(atPath: outputURL.path, contents: nil)
        fileHandle = try FileHandle(forWritingTo: outputURL)

        var createdObserver: AXObserver?
        let observerError = AXObserverCreate(client.processID, accessibilityProbeCallback, &createdObserver)
        guard observerError == .success, let createdObserver else {
            try? fileHandle?.close()
            throw ProbeError.observerCreationFailed(observerError.rawValue)
        }
        observer = createdObserver
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(createdObserver), .commonModes)

        write([
            "type": "probeStarted",
            "schemaVersion": 1,
            "process": [
                "pid": Int(client.processID),
                "bundleID": client.bundleID.map { $0 as Any } ?? NSNull(),
                "applicationName": client.applicationName,
            ],
            "operatingSystem": ProcessInfo.processInfo.operatingSystemVersionString,
            "maximumDepth": maximumDepth,
            "maximumNodes": maximumNodes,
        ])
        let initial = captureSnapshot()
        previousSnapshot = initial
        writeSnapshot(initial, reason: "initial")
        fallbackTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.captureDiff(reason: "periodicRescan") }
        }
    }

    func stop() {
        guard !stopped else { return }
        stopped = true
        pendingSnapshot?.cancel()
        pendingSnapshot = nil
        fallbackTimer?.invalidate()
        fallbackTimer = nil
        captureDiff(reason: "final")
        write(["type": "probeStopped"])
        if let observer {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        }
        observer = nil
        try? fileHandle?.close()
        fileHandle = nil
    }

    func receive(notification: String, element: AXUIElement) {
        guard !stopped else { return }
        write([
            "type": "notification",
            "notification": notification,
            "element": elementSummary(element),
        ])
        pendingSnapshot?.cancel()
        pendingSnapshot = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            self?.captureDiff(reason: notification)
        }
    }

    nonisolated static func difference(
        from before: [AccessibilitySnapshotNode],
        to after: [AccessibilitySnapshotNode]
    ) -> AccessibilityTreeDiff {
        let old = Dictionary(uniqueKeysWithValues: before.map { ($0.path, $0) })
        let new = Dictionary(uniqueKeysWithValues: after.map { ($0.path, $0) })
        let oldPaths = Set(old.keys)
        let newPaths = Set(new.keys)
        let added = newPaths.subtracting(oldPaths).sorted().compactMap { new[$0] }
        let removed = oldPaths.subtracting(newPaths).sorted()
        var changed: [AccessibilityAttributeChange] = []
        for path in oldPaths.intersection(newPaths).sorted() {
            guard let oldNode = old[path], let newNode = new[path] else { continue }
            let attributes = Set(oldNode.attributes.keys).union(newNode.attributes.keys)
            for attribute in attributes.sorted() where oldNode.attributes[attribute] != newNode.attributes[attribute] {
                changed.append(AccessibilityAttributeChange(
                    path: path,
                    attribute: attribute,
                    before: oldNode.attributes[attribute],
                    after: newNode.attributes[attribute]
                ))
            }
        }
        return AccessibilityTreeDiff(added: added, removed: removed, changed: changed)
    }

    private func captureDiff(reason: String) {
        guard !stopped || reason == "final" else { return }
        let snapshot = captureSnapshot()
        guard let previousSnapshot else {
            self.previousSnapshot = snapshot
            writeSnapshot(snapshot, reason: reason)
            return
        }
        let diff = Self.difference(from: previousSnapshot.nodes, to: snapshot.nodes)
        self.previousSnapshot = snapshot
        guard !diff.isEmpty || previousSnapshot.truncated != snapshot.truncated else { return }
        write([
            "type": "treeDiff",
            "reason": reason,
            "added": diff.added.map(Self.nodeObject),
            "removed": diff.removed,
            "changed": diff.changed.map { change -> [String: Any] in
                return [
                    "path": change.path,
                    "attribute": change.attribute,
                    "before": change.before ?? NSNull(),
                    "after": change.after ?? NSNull(),
                ]
            },
            "truncated": snapshot.truncated,
        ])
    }

    private func captureSnapshot() -> Snapshot {
        var nodes: [AccessibilitySnapshotNode] = []
        var truncated = false

        func visit(_ element: AXUIElement, path: String, depth: Int) {
            guard nodes.count < maximumNodes else { truncated = true; return }
            guard depth <= maximumDepth else { truncated = true; return }
            observe(element)
            nodes.append(AccessibilitySnapshotNode(path: path, attributes: attributes(of: element)))
            for (index, child) in children(of: element).enumerated() {
                visit(child, path: "\(path)/\(index)", depth: depth + 1)
                if nodes.count >= maximumNodes { break }
            }
        }

        visit(applicationElement, path: "0", depth: 0)
        return Snapshot(nodes: nodes, truncated: truncated)
    }

    private func observe(_ element: AXUIElement) {
        guard let observer else { return }
        let hash = CFHash(element)
        guard observedElementHashes.insert(hash).inserted else { return }
        let notifications = [
            kAXCreatedNotification,
            kAXFocusedUIElementChangedNotification,
            kAXFocusedWindowChangedNotification,
            kAXLayoutChangedNotification,
            kAXSelectedChildrenChangedNotification,
            kAXSelectedRowsChangedNotification,
            kAXTitleChangedNotification,
            kAXUIElementDestroyedNotification,
            kAXValueChangedNotification,
            kAXWindowCreatedNotification,
        ]
        let reference = Unmanaged.passUnretained(self).toOpaque()
        for notification in notifications {
            _ = AXObserverAddNotification(observer, element, notification as CFString, reference)
        }
    }

    private func attributes(of element: AXUIElement) -> [String: String] {
        var copiedNames: CFArray?
        guard AXUIElementCopyAttributeNames(element, &copiedNames) == .success,
              let names = copiedNames as? [String] else { return [:] }
        var result: [String: String] = [:]
        for name in names.sorted() {
            var copiedValue: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, name as CFString, &copiedValue) == .success,
                  let copiedValue else { continue }
            result[name] = describe(copiedValue)
        }
        return result
    }

    private func children(of element: AXUIElement) -> [AXUIElement] {
        var copiedValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &copiedValue) == .success,
              let values = copiedValue as? [Any] else { return [] }
        return values.compactMap { value in
            guard CFGetTypeID(value as CFTypeRef) == AXUIElementGetTypeID() else { return nil }
            return unsafeDowncast(value as AnyObject, to: AXUIElement.self)
        }
    }

    private func describe(_ value: CFTypeRef) -> String {
        let typeID = CFGetTypeID(value)
        if typeID == AXUIElementGetTypeID() { return "<AXUIElement>" }
        if typeID == AXValueGetTypeID() {
            return describeAXValue(unsafeDowncast(value, to: AXValue.self))
        }
        if let string = value as? String { return string }
        if let number = value as? NSNumber { return number.stringValue }
        if let array = value as? [Any] {
            let values = array.prefix(20).map { item -> String in
                let reference = item as CFTypeRef
                return CFGetTypeID(reference) == AXUIElementGetTypeID() ? "<AXUIElement>" : describe(reference)
            }
            return "[\(values.joined(separator: ", "))]\(array.count > 20 ? " … (\(array.count) values)" : "")"
        }
        return String(describing: value)
    }

    private func describeAXValue(_ value: AXValue) -> String {
        switch AXValueGetType(value) {
        case .cgPoint:
            var point = CGPoint.zero
            guard AXValueGetValue(value, .cgPoint, &point) else { return "<CGPoint>" }
            return "{x:\(point.x),y:\(point.y)}"
        case .cgSize:
            var size = CGSize.zero
            guard AXValueGetValue(value, .cgSize, &size) else { return "<CGSize>" }
            return "{width:\(size.width),height:\(size.height)}"
        case .cgRect:
            var rect = CGRect.zero
            guard AXValueGetValue(value, .cgRect, &rect) else { return "<CGRect>" }
            return "{x:\(rect.origin.x),y:\(rect.origin.y),width:\(rect.width),height:\(rect.height)}"
        case .cfRange:
            var range = CFRange()
            guard AXValueGetValue(value, .cfRange, &range) else { return "<CFRange>" }
            return "{location:\(range.location),length:\(range.length)}"
        default:
            return "<AXValue>"
        }
    }

    private func elementSummary(_ element: AXUIElement) -> [String: String] {
        let values = attributes(of: element)
        let keys = [kAXRoleAttribute, kAXSubroleAttribute, kAXIdentifierAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute]
        return Dictionary(uniqueKeysWithValues: keys.compactMap { key in values[key].map { (key, $0) } })
    }

    private func writeSnapshot(_ snapshot: Snapshot, reason: String) {
        write([
            "type": "treeSnapshot",
            "reason": reason,
            "nodes": snapshot.nodes.map(Self.nodeObject),
            "truncated": snapshot.truncated,
        ])
    }

    private func write(_ fields: [String: Any]) {
        guard let fileHandle else { return }
        var record = fields
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        record["timestamp"] = formatter.string(from: Date())
        guard JSONSerialization.isValidJSONObject(record),
              let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]),
              var line = String(data: data, encoding: .utf8)?.data(using: .utf8) else { return }
        line.append(0x0A)
        do {
            try fileHandle.write(contentsOf: line)
            try fileHandle.synchronize()
        } catch {
            // The probe is diagnostic; capture must continue if its log becomes unavailable.
        }
    }

    private static func nodeObject(_ node: AccessibilitySnapshotNode) -> [String: Any] {
        ["path": node.path, "attributes": node.attributes]
    }

}

enum ProbeError: LocalizedError {
    case invalidProcess
    case accessibilityPermissionRequired
    case observerCreationFailed(Int32)

    var errorDescription: String? {
        switch self {
        case .invalidProcess:
            "The meeting application is no longer running."
        case .accessibilityPermissionRequired:
            "Accessibility permission is required. Grant it in System Settings to probe future recordings."
        case let .observerCreationFailed(code):
            "Could not observe the application's Accessibility tree (AX error \(code))."
        }
    }
}
