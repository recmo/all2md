import Foundation
import Darwin

struct MeetingStore: Sendable {
    let root: URL

    init(root: URL? = nil) {
        self.root = root ?? FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "Documents/Meetings", directoryHint: .isDirectory)
    }

    func paths(startedAt: Date, title: String?) throws -> RecordingPaths {
        let calendar = Calendar.current
        let year = String(format: "%04d", calendar.component(.year, from: startedAt))
        let month = String(format: "%02d", calendar.component(.month, from: startedAt))
        let day = String(format: "%04d-%02d-%02d", calendar.component(.year, from: startedAt), calendar.component(.month, from: startedAt), calendar.component(.day, from: startedAt))
        let directory = root.appending(path: year, directoryHint: .isDirectory).appending(path: month, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let stem = "\(day)-\(Self.slug(title ?? "meeting"))"
        var candidate = stem
        var suffix = 2
        while Self.recordingExists(named: candidate, in: directory) {
            candidate = "\(stem)-\(suffix)"
            suffix += 1
        }
        return RecordingPaths(directory: directory, baseName: candidate)
    }

    func write(_ manifest: CaptureManifest, to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(manifest).write(to: url, options: .atomic)
    }

    func interruptedRecordings(excluding claimed: Set<URL> = []) -> [URL] {
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else { return [] }
        let identities = Set(claimed.map { $0.deletingLastPathComponent().resolvingSymlinksInPath().appending(path: $0.lastPathComponent) })
        return enumerator.compactMap { $0 as? URL }.filter {
            guard $0.lastPathComponent.hasSuffix(".part.caf") else { return false }
            let paths = paths(forInterruptedFile: $0)
            let identity = paths.directory.resolvingSymlinksInPath().appending(path: paths.manifest.lastPathComponent)
            return !identities.contains(identity) && RecordingClaim.isAvailable(paths)
        }
    }

    func paths(forInterruptedFile file: URL) -> RecordingPaths {
        var name = file.lastPathComponent
        if name.hasPrefix(".") { name.removeFirst() }
        let markers = ["-microphone-", "-microphone.part.caf", "-participants.part.caf"]
        let baseName = markers.compactMap { marker in
            name.range(of: marker, options: .backwards).map { String(name[..<$0.lowerBound]) }
        }.first ?? file.deletingPathExtension().lastPathComponent
        return RecordingPaths(directory: file.deletingLastPathComponent(), baseName: baseName)
    }

    static func slug(_ value: String) -> String {
        let folded = value.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: .current)
        let slugCharacters = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789")
        let pieces = folded.lowercased().components(separatedBy: slugCharacters.inverted).filter { !$0.isEmpty }
        let slug = String(pieces.joined(separator: "-").prefix(80))
        return slug.isEmpty ? "meeting" : slug
    }

    private static func recordingExists(named baseName: String, in directory: URL) -> Bool {
        let manager = FileManager.default
        if ["\(baseName)-capture.json", "\(baseName).mka", "\(baseName)-accessibility.jsonl", "\(baseName)-microphone.flac", "\(baseName)-participants.flac"]
            .contains(where: { manager.fileExists(atPath: directory.appending(path: $0).path) }) {
            return true
        }
        guard let contents = try? manager.contentsOfDirectory(atPath: directory.path) else { return false }
        return contents.contains { $0.hasPrefix(".\(baseName)-") || $0 == ".\(baseName).part.mka" }
    }
}

// Keep the lock file in place: unlinking it would let another process lock a
// different inode for the same recording. The OS releases flock on process exit.
final class RecordingClaim {
    private let descriptor: Int32

    init(_ paths: RecordingPaths) throws {
        let url = paths.directory.appending(path: ".\(paths.baseName).lock")
        let opened = open(url.path, O_CREAT | O_RDWR | O_CLOEXEC, mode_t(0o600))
        guard opened >= 0 else { throw CaptureError.writerFailure("Cannot claim recording") }
        guard flock(opened, LOCK_EX | LOCK_NB) == 0 else {
            close(opened)
            throw CaptureError.writerFailure("Recording is already being captured or processed")
        }
        descriptor = opened
    }

    deinit { close(descriptor) }

    static func isAvailable(_ paths: RecordingPaths) -> Bool {
        guard let claim = try? RecordingClaim(paths) else { return false }
        return withExtendedLifetime(claim) { true }
    }
}
