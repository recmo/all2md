import Foundation

enum CaptureError: LocalizedError {
    case noMicrophone
    case systemAudioCaptureFailure(String)
    case microphonePermissionRequired
    case triggeringApplicationUnavailable
    case deviceSelectionFailure(String)
    case writerFailure(String)

    static func details(_ error: Error) -> String {
        var current: NSError? = error as NSError
        var messages: [String] = []
        // Bound traversal in case a framework supplies a cyclic error chain.
        for _ in 0..<5 {
            guard let error = current else { break }
            var message = "\(error.localizedDescription) [\(error.domain): \(error.code)]"
            if let reason = error.localizedFailureReason { message += " \(reason)" }
            messages.append(message)
            current = error.userInfo[NSUnderlyingErrorKey] as? NSError
        }
        return messages.joined(separator: "\nCaused by: ")
    }

    var errorDescription: String? {
        switch self {
        case .noMicrophone: "No usable microphone is available."
        case let .systemAudioCaptureFailure(message): "\(message). Check Meeting Capture in Privacy & Security > Screen & System Audio Recording, then retry the recording check."
        case .microphonePermissionRequired: "Allow Meeting Capture in Privacy & Security > Microphone, then retry the recording check."
        case .triggeringApplicationUnavailable: "The triggering audio process is no longer available. Retry while the call is active."
        case let .deviceSelectionFailure(message): "Could not select microphone: \(message)"
        case let .writerFailure(message): "Audio writer failed: \(message)"
        }
    }
}
