import Foundation

struct RecordingSession: Codable, Sendable {
    var microphoneSegments: [CapturedAudioSegment]
    var processTracks: [CapturedProcessTrack]
    let captureStartedAt: Date
    var captureEndedAt: Date?
    let paths: RecordingPaths
    let trigger: CaptureTrigger
    var metadata: [MetadataEvent]
    var warnings: [String]
    var interruptions: [CaptureTimeRange]
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
