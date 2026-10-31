import Darwin
import Foundation

@main
enum MeetingCaptureWorker {
    @MainActor
    static func main() {
        do {
            // Keep tools launched by this worker in a group the app can stop
            // when a startup check exceeds its deadline.
            guard getpgrp() == getpid() || setpgid(0, 0) == 0 else {
                throw WorkerMainError.processGroup
            }
            let arguments = Array(CommandLine.arguments.dropFirst())
            guard let command = arguments.first else { throw WorkerMainError.invalidArguments }
            switch command {
            case "check-access":
                try runRequest(WorkerAccessRequest.self, arguments: arguments, operation: WorkerOperations.checkAccess)
            case "check-metadata-access":
                try runRequest(WorkerAccessRequest.self, arguments: arguments, operation: WorkerOperations.checkMetadataAccess)
            case "finalize":
                try runRequest(FinalizeCaptureRequest.self, arguments: arguments, operation: WorkerOperations.finalize)
            case "recover":
                try runRequest(RecoverCaptureRequest.self, arguments: arguments, operation: WorkerOperations.recover)
            case "accessibility-probe":
                try runAccessibilityProbe(arguments: arguments)
            case "crash-test":
                raise(SIGSEGV)
            default:
                throw WorkerMainError.invalidArguments
            }
        } catch {
            FileHandle.standardError.write(Data("MeetingCaptureWorker: \(error.localizedDescription)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }

    private static func runRequest<Request: Decodable, Value: Codable>(
        _ requestType: Request.Type,
        arguments: [String],
        operation: (Request) throws -> Value
    ) throws {
        guard arguments.count == 3 else { throw WorkerMainError.invalidArguments }
        let requestURL = URL(fileURLWithPath: arguments[1])
        let responseURL = URL(fileURLWithPath: arguments[2])
        let request = try JSONDecoder().decode(Request.self, from: Data(contentsOf: requestURL))
        let response: WorkerResponse<Value>
        do { response = .success(try operation(request)) }
        catch { response = .failure(error) }
        try JSONEncoder().encode(response).write(to: responseURL, options: .atomic)
    }

    @MainActor
    private static func runAccessibilityProbe(arguments: [String]) throws {
        guard arguments.count == 2 else { throw WorkerMainError.invalidArguments }
        let requestURL = URL(fileURLWithPath: arguments[1])
        let requestDirectory = requestURL.deletingLastPathComponent()
        let removeRequest: @Sendable () -> Void = {
            _ = unlink(requestURL.path)
            _ = rmdir(requestDirectory.path)
        }
        defer { removeRequest() }
        DispatchQueue.global(qos: .utility).async {
            _ = FileHandle.standardInput.readDataToEndOfFile()
            removeRequest()
            // Do not wait for the main queue: an AX call may be hung there.
            // Exiting closes the log and releases its publication lock.
            _exit(EXIT_SUCCESS)
        }
        let request = try JSONDecoder().decode(AccessibilityProbeRequest.self, from: Data(contentsOf: requestURL))
        removeRequest()
        // Keep this inode in place so recovery and the writer always lock the
        // same file, including across recorder crashes.
        let lockURL = request.outputURL.appendingPathExtension("lock")
        let lock = open(lockURL.path, O_CREAT | O_RDWR | O_CLOEXEC, mode_t(0o600))
        guard lock >= 0 else { throw WorkerMainError.artifactClaim }
        defer { close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw WorkerMainError.artifactClaim }
        let client = AudioClient(
            audioObjectID: 0,
            processID: request.processID,
            bundleID: request.bundleID,
            applicationName: request.applicationName,
            inputDevices: []
        )
        let probe = try AccessibilityProbe(client: client, outputURL: request.outputURL)
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        let interruption = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let stop = {
            MainActor.assumeIsolated {
                probe.stop()
                CFRunLoopStop(CFRunLoopGetMain())
            }
        }
        termination.setEventHandler(handler: stop)
        interruption.setEventHandler(handler: stop)
        termination.resume()
        interruption.resume()
        RunLoop.main.run()
        probe.stop()
    }
}

private enum WorkerMainError: LocalizedError {
    case invalidArguments
    case processGroup
    case artifactClaim

    var errorDescription: String? {
        switch self {
        case .invalidArguments: "invalid worker arguments"
        case .processGroup: "could not isolate worker subprocesses"
        case .artifactClaim: "could not claim the Accessibility log"
        }
    }
}
