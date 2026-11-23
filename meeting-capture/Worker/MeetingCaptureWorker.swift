import Darwin
import Foundation

@main
enum MeetingCaptureWorker {
    @MainActor
    static func main() {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            guard let command = arguments.first else { throw WorkerMainError.invalidArguments }
            switch command {
            case "keep-running":
                guard arguments.count == 1 else { throw WorkerMainError.invalidArguments }
                try keepRunning()
            case "check-permissions":
                try runRequest(URL.self, arguments: arguments, operation: WorkerOperations.checkPermissions)
            case "check-access":
                try runRequest(WorkerAccessRequest.self, arguments: arguments, operation: WorkerOperations.checkAccess)
            case "check-metadata-access":
                try runRequest(WorkerAccessRequest.self, arguments: arguments, operation: WorkerOperations.checkMetadataAccess)
            case "finalize":
                try runRequest(RecordingSession.self, arguments: arguments, operation: WorkerOperations.finalize)
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

    private static func keepRunning() throws {
        // Swift cannot import libproc's composed PROC_PIDPATHINFO_MAXSIZE macro.
        let capacity = 4 * Int(MAXPATHLEN)
        var path = [CChar](repeating: 0, count: capacity)
        guard proc_pidpath(getpid(), &path, UInt32(capacity)) > 0 else {
            throw WorkerMainError.invalidArguments
        }
        let app = URL(fileURLWithFileSystemRepresentation: path, isDirectory: false, relativeTo: nil)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        // LaunchServices reuses an open instance. Become the waiting launcher
        // so launchd receives its exit directly, without another process waiter.
        let arguments = ["/usr/bin/open", "-g", "-W", app.path].map { strdup($0) }
        defer { arguments.forEach { free($0) } }
        var argv = arguments + [nil]
        execv("/usr/bin/open", &argv)
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }

    private static func runRequest<Request: Decodable, Value: Codable>(
        _ requestType: Request.Type,
        arguments: [String],
        operation: (Request) throws -> Value
    ) throws {
        guard arguments.count == 3 else { throw WorkerMainError.invalidArguments }
        WorkerLifetime.monitor()
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
        WorkerLifetime.monitor(onExit: removeRequest)
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
    case artifactClaim

    var errorDescription: String? {
        switch self {
        case .invalidArguments: "invalid worker arguments"
        case .artifactClaim: "could not claim the Accessibility log"
        }
    }
}
