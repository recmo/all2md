import Foundation

struct WorkerAccessRequest: Codable, Sendable {
    let root: URL
    let microphone: URL
    let applicationProcessID: Int32
}

struct WorkerAccessReport: Codable, Equatable, Sendable {
    let accessibility: String
}

struct FinalizeCaptureRequest: Codable, Sendable {
    let microphoneSegments: [CapturedAudioSegment]
    let participants: URL?
    let participantsStartedAt: Date?
    let captureStartedAt: Date
    let captureEndedAt: Date
    let paths: RecordingPaths
    let accessibilityWorkerProcessID: Int32?
    let trigger: CaptureTrigger
    let metadata: [MetadataEvent]
    let warnings: [String]
    let interruptions: [CaptureTimeRange]
}

struct RecoverCaptureRequest: Codable, Sendable {
    let interruptedFile: URL
}

struct AccessibilityProbeRequest: Codable, Sendable {
    let processID: Int32
    let bundleID: String?
    let applicationName: String
    let outputURL: URL
}

struct WorkerManifestResult: Codable, Sendable {
    let manifest: URL?
}

struct WorkerResponse<Value: Codable>: Codable {
    let value: Value?
    let error: String?

    static func success(_ value: Value) -> Self { Self(value: value, error: nil) }
    static func failure(_ error: Error) -> Self {
        Self(value: nil, error: error.localizedDescription)
    }
}
