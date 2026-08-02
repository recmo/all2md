import Foundation

struct AudioClient: Identifiable, Equatable, Sendable {
    let audioObjectID: UInt32
    let processID: pid_t
    let bundleID: String?
    let applicationName: String
    var id: pid_t { processID }
}

enum TriggerMethod: String, Codable, Sendable { case audioProcess, deviceRunning, manual }

struct CaptureTrigger: Codable, Equatable, Sendable {
    let method: TriggerMethod
    let processID: Int32?
    let bundleID: String?
    let applicationName: String?
}

struct AudioTrack: Codable, Equatable, Sendable {
    enum Role: String, Codable, Sendable { case microphone, participants }
    let role: Role
    let file: String
    let format: String
    let sampleRate: Double
    let channels: Int
    let durationSeconds: Double
    let sha256: String
}

struct CaptureTimeRange: Codable, Equatable, Sendable {
    let startedAt: Date
    let endedAt: Date
    let reason: String
}

struct MetadataEvent: Codable, Equatable, Sendable {
    enum Kind: String, Codable, Sendable { case windowTitle, participant, activeSpeaker, platform, note }
    let timestamp: Date
    let kind: Kind
    let value: String
    let confidence: Double?
}

struct CaptureManifest: Codable, Equatable, Sendable {
    enum Status: String, Codable, Sendable { case complete, incomplete, failed }
    let schemaVersion: Int
    let meetingID: UUID
    let slug: String
    let title: String?
    let platform: String?
    let calendarEventID: String?
    let startedAt: Date
    let endedAt: Date
    let timeZone: String
    let trigger: CaptureTrigger
    let audio: [AudioTrack]
    let interruptions: [CaptureTimeRange]
    let metadataEvents: [MetadataEvent]
    let warnings: [String]
    let status: Status
}

struct RecordingPaths: Sendable {
    let directory: URL
    let baseName: String
    var microphoneTemporary: URL { directory.appending(path: ".\(baseName)-microphone.part.caf") }
    var participantsTemporary: URL { directory.appending(path: ".\(baseName)-participants.part.caf") }
    var microphoneFinal: URL { directory.appending(path: "\(baseName)-microphone.flac") }
    var participantsFinal: URL { directory.appending(path: "\(baseName)-participants.flac") }
    var manifest: URL { directory.appending(path: "\(baseName)-capture.json") }
}
