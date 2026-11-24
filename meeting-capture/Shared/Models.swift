import Foundation

struct AudioClient: Equatable, Sendable {
    let audioObjectID: UInt32
    let processID: pid_t
    let bundleID: String?
    let applicationName: String
    let inputDevices: [AudioInputDevice]

    var primaryInputDevice: AudioInputDevice? { inputDevices.first }

    var inputDeviceSummary: String {
        inputDevices.isEmpty ? "Unknown microphone" : inputDevices.map(\.name).joined(separator: ", ")
    }
}

struct AudioInputDevice: Equatable, Sendable {
    let id: UInt32
    let uid: String?
    let name: String

    var manifestValue: String {
        guard let uid, !uid.isEmpty else { return name }
        return "\(name) [\(uid)]"
    }
}

enum TriggerMethod: String, Codable, Sendable { case audioProcess, manual }

struct CaptureTrigger: Codable, Equatable, Sendable {
    let method: TriggerMethod
    let processID: Int32?
    let bundleID: String?
    let applicationName: String?
}

struct AudioTrack: Codable, Equatable, Sendable {
    enum Role: String, Codable, Sendable { case microphone, system }
    let role: Role
    let streamIndex: Int
    let codec: String
    let sampleRate: Double
    let channels: Int
    let durationSeconds: Double
    let bitrate: Int
}

struct AudioContainer: Codable, Equatable, Sendable {
    let file: String
    let format: String
    let sha256: String
}

struct CapturedAudioSegment: Codable, Equatable, Sendable {
    let url: URL
    let startedAt: Date
}

struct MicrophoneChange: Codable, Equatable, Sendable {
    let timestamp: Date
    let device: String
}

struct CaptureManifest: Codable, Equatable, Sendable {
    enum Status: String, Codable, Sendable { case complete, incomplete }
    let schemaVersion: Int
    let meetingID: UUID
    let slug: String
    let title: String?
    let platform: String?
    let startedAt: Date
    let endedAt: Date
    let timeZone: String
    let trigger: CaptureTrigger
    let container: AudioContainer
    let audio: [AudioTrack]
    let microphoneChanges: [MicrophoneChange]
    let warnings: [String]
    let status: Status
}

struct RecordingPaths: Codable, Sendable {
    let directory: URL
    let baseName: String
    func microphoneTemporary(segment: Int) -> URL {
        directory.appending(path: ".\(baseName)-microphone-\(String(format: "%04d", segment)).part.caf")
    }
    func systemTemporary(segment: Int) -> URL {
        directory.appending(path: ".\(baseName)-system-\(String(format: "%04d", segment)).part.caf")
    }
    var archiveTemporary: URL { directory.appending(path: ".\(baseName).part.mka") }
    var archiveFinal: URL { directory.appending(path: "\(baseName).mka") }
    var manifest: URL { directory.appending(path: "\(baseName)-capture.json") }
    var session: URL { directory.appending(path: ".\(baseName).session.json") }
}

struct RecordingSession: Codable, Sendable {
    var microphoneSegments: [CapturedAudioSegment]
    var systemSegments: [CapturedAudioSegment]
    let captureStartedAt: Date
    var captureEndedAt: Date?
    let paths: RecordingPaths
    let trigger: CaptureTrigger
    var microphoneChanges: [MicrophoneChange]
    var warnings: [String]
    var captureFailed = false

    func save() throws {
        try JSONEncoder().encode(self).write(to: paths.session, options: .atomic)
        let handle = try FileHandle(forWritingTo: paths.session)
        defer { try? handle.close() }
        try handle.synchronize()
    }

    static func load(from url: URL) throws -> Self {
        try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
    }
}

extension RecordingSession {
    init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        microphoneSegments = try values.decode([CapturedAudioSegment].self, forKey: .microphoneSegments)
        captureStartedAt = try values.decode(Date.self, forKey: .captureStartedAt)
        captureEndedAt = try values.decodeIfPresent(Date.self, forKey: .captureEndedAt)
        paths = try values.decode(RecordingPaths.self, forKey: .paths)
        trigger = try values.decode(CaptureTrigger.self, forKey: .trigger)
        warnings = try values.decode([String].self, forKey: .warnings)
        captureFailed = try values.decode(Bool.self, forKey: .captureFailed)
        if values.contains(.systemSegments) {
            systemSegments = try values.decode([CapturedAudioSegment].self, forKey: .systemSegments)
            microphoneChanges = try values.decode([MicrophoneChange].self, forKey: .microphoneChanges)
        } else {
            let previous = try PreviousCaptureAudio(from: decoder)
            systemSegments = previous.processTracks
            microphoneChanges = previous.metadata.filter { $0.kind == "microphoneDevice" }
                .map { MicrophoneChange(timestamp: $0.timestamp, device: $0.value) }
            if captureEndedAt == captureStartedAt { captureEndedAt = nil }
        }
    }

    private struct PreviousCaptureAudio: Decodable {
        struct Metadata: Decodable {
            let kind: String
            let timestamp: Date
            let value: String
        }
        let processTracks: [CapturedAudioSegment]
        let metadata: [Metadata]
    }
}

struct RecoverCaptureRequest: Codable, Sendable {
    let interruptedFile: URL
}

struct WorkerResponse: Codable {
    let value: URL?
    let error: String?

    static func success(_ value: URL) -> Self { Self(value: value, error: nil) }
    static func failure(_ error: Error) -> Self {
        Self(value: nil, error: error.localizedDescription)
    }
}
