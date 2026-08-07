import Foundation

struct AudioClient: Identifiable, Equatable, Sendable {
    let audioObjectID: UInt32
    let processID: pid_t
    let bundleID: String?
    let applicationName: String
    let inputDevices: [AudioInputDevice]
    var id: pid_t { processID }

    var primaryInputDevice: AudioInputDevice? { inputDevices.first }

    var inputDeviceSummary: String {
        inputDevices.isEmpty ? "Unknown microphone" : inputDevices.map(\.name).joined(separator: ", ")
    }
}

struct AudioInputDevice: Identifiable, Equatable, Sendable {
    let id: UInt32
    let uid: String?
    let name: String

    var manifestValue: String {
        guard let uid, !uid.isEmpty else { return name }
        return "\(name) [\(uid)]"
    }
}

struct CaptureApplicationIdentity: Equatable, Sendable {
    let processID: pid_t
    let bundleID: String?
    let applicationName: String
    let isUserApplication: Bool
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

struct AccessibilityArtifact: Codable, Equatable, Sendable {
    let file: String
    let format: String
    let sha256: String
}

struct CapturedAudioSegment: Equatable, Sendable {
    let url: URL
    let startedAt: Date
    let endedAt: Date
}

struct CaptureTimeRange: Codable, Equatable, Sendable {
    let startedAt: Date
    let endedAt: Date
    let reason: String
}

struct MetadataEvent: Codable, Equatable, Sendable {
    enum Kind: String, Codable, Sendable { case windowTitle, participant, activeSpeaker, platform, microphoneDevice, note }
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
    let container: AudioContainer
    let accessibility: AccessibilityArtifact?
    let audio: [AudioTrack]
    let interruptions: [CaptureTimeRange]
    let metadataEvents: [MetadataEvent]
    let warnings: [String]
    let status: Status
}

struct RecordingPaths: Sendable {
    let directory: URL
    let baseName: String
    func microphoneTemporary(segment: Int) -> URL {
        directory.appending(path: ".\(baseName)-microphone-\(String(format: "%04d", segment)).part.caf")
    }
    var participantsTemporary: URL { directory.appending(path: ".\(baseName)-participants.part.caf") }
    var archiveTemporary: URL { directory.appending(path: ".\(baseName).part.mka") }
    var archiveFinal: URL { directory.appending(path: "\(baseName).mka") }
    var accessibilityTemporary: URL { directory.appending(path: ".\(baseName)-accessibility.part.jsonl") }
    var accessibilityFinal: URL { directory.appending(path: "\(baseName)-accessibility.jsonl") }
    var manifest: URL { directory.appending(path: "\(baseName)-capture.json") }
}
