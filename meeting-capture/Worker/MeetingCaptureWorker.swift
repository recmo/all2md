import Darwin
import Foundation

@main
enum MeetingCaptureWorker {
    static func main() {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments == ["keep-running"] { try keepRunning(); return }
            guard arguments.count == 3 else { throw CaptureError.writerFailure("Invalid worker arguments") }
            let data = try Data(contentsOf: URL(fileURLWithPath: arguments[1]))
            let response: WorkerResponse
            do {
                let result: URL
                switch arguments[0] {
                case "finalize": result = try WorkerOperations.finalize(JSONDecoder().decode(RecordingSession.self, from: data))
                case "recover": result = try WorkerOperations.recover(JSONDecoder().decode(RecoverCaptureRequest.self, from: data))
                default: throw CaptureError.writerFailure("Unknown worker operation")
                }
                response = .success(result)
            } catch { response = .failure(error) }
            try JSONEncoder().encode(response).write(to: URL(fileURLWithPath: arguments[2]), options: .atomic)
        } catch {
            FileHandle.standardError.write(Data("MeetingCapture: \(error.localizedDescription)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }

    private static func keepRunning() throws {
        // Swift cannot import libproc's composed PROC_PIDPATHINFO_MAXSIZE macro.
        let capacity = 4 * Int(MAXPATHLEN)
        var path = [CChar](repeating: 0, count: capacity)
        guard proc_pidpath(getpid(), &path, UInt32(capacity)) > 0 else {
            throw CaptureError.writerFailure("Could not locate application")
        }
        let app = URL(fileURLWithFileSystemRepresentation: path, isDirectory: false, relativeTo: nil)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        // LaunchServices reuses an open instance. Become the waiting launcher
        // so launchd receives its exit directly, without another process waiter.
        let command: [String] = ["/usr/bin/open", "-g", "-W", app.path]
        let arguments = command.map { strdup($0) }
        defer { arguments.forEach { free($0) } }
        var argv = arguments + [nil]
        execv("/usr/bin/open", &argv)
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
}
