import Darwin
import Foundation
import Synchronization

// Archive jobs are serial. Capture uses its own queue and never waits for them.
struct BackgroundWorkerClient: Sendable {
    private static let queue = DispatchQueue(label: "ventures.wicked.MeetingCapture.archives", qos: .utility)
    let executableURL: URL

    init(executableURL: URL? = nil) {
        self.executableURL = executableURL ?? Bundle.main.bundleURL.appending(path: "Contents/Helpers/MeetingCaptureWorker")
    }

    func finalize(_ session: RecordingSession) async throws -> URL {
        try await run("finalize", request: session)
    }

    func recover(_ request: RecoverCaptureRequest) async throws -> URL {
        try await run("recover", request: request)
    }

    private func run<Request: Encodable>(_ command: String, request: Request) async throws -> URL {
        let job = WorkerJob(executable: executableURL, command: command, request: try JSONEncoder().encode(request))
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                Self.queue.async { continuation.resume(with: Result { try job.run() }) }
            }
        } onCancel: { job.cancelled.store(true, ordering: .relaxed) }
    }
}

private final class WorkerJob: Sendable {
    let cancelled = Atomic<Bool>(false)
    private let executable: URL
    private let command: String
    private let request: Data

    init(executable: URL, command: String, request: Data) {
        self.executable = executable
        self.command = command
        self.request = request
    }

    func run() throws -> URL {
        if cancelled.load(ordering: .relaxed) { throw CancellationError() }
        let directory = FileManager.default.temporaryDirectory.appending(path: "MeetingCapture-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let input = directory.appending(path: "request.json")
        let output = directory.appending(path: "response.json")
        try request.write(to: input)

        func check(_ status: Int32) throws {
            guard status == 0 else { throw CaptureError.writerFailure(String(cString: strerror(status))) }
        }
        var actions: posix_spawn_file_actions_t? = nil
        try check(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }
        for descriptor in [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO] {
            try check(posix_spawn_file_actions_addopen(&actions, descriptor, "/dev/null",
                                                      descriptor == STDIN_FILENO ? O_RDONLY : O_WRONLY, 0))
        }
        var attributes: posix_spawnattr_t? = nil
        try check(posix_spawnattr_init(&attributes))
        defer { posix_spawnattr_destroy(&attributes) }
        try check(posix_spawnattr_setpgroup(&attributes, 0))
        try check(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETPGROUP)))
        let arguments = [executable.path, command, input.path, output.path]
        let strings = arguments.map { strdup($0) }
        defer { strings.forEach { free($0) } }
        var argv = strings + [nil]
        let environment = ProcessInfo.processInfo.environment.map { strdup("\($0.key)=\($0.value)") }
        defer { environment.forEach { free($0) } }
        var envp = environment + [nil]
        var pid: pid_t = 0
        try check(posix_spawn(&pid, executable.path, &actions, &attributes, &argv, &envp))
        guard pid > 0 else { throw CaptureError.writerFailure("Could not start audio processing") }

        var status = siginfo_t()
        func wait(_ options: Int32) throws {
            while waitid(P_PID, id_t(pid), &status, options) < 0 {
                if errno != EINTR { throw CaptureError.writerFailure("Could not wait for audio processing") }
            }
        }
        var stopped = false
        while true {
            try wait(WEXITED | WNOHANG | WNOWAIT)
            // Keep the group's leader unreaped until all encoders are stopped.
            // Its PID is still reserved, so cleanup cannot target a reused ID.
            if status.si_pid != 0 { break }
            if cancelled.load(ordering: .relaxed) { stopped = true; _ = kill(-pid, SIGKILL) }
            Thread.sleep(forTimeInterval: 0.1)
        }
        _ = kill(-pid, SIGKILL)
        try wait(WEXITED)
        if stopped { throw CancellationError() }
        guard status.si_code == CLD_EXITED, status.si_status == 0 else {
            throw CaptureError.writerFailure("Audio processing exited with status \(status.si_status)")
        }
        let response = try JSONDecoder().decode(WorkerResponse.self, from: Data(contentsOf: output))
        if let error = response.error { throw CaptureError.writerFailure(error) }
        guard let value = response.value else { throw CaptureError.writerFailure("No audio processing result") }
        return value
    }
}
