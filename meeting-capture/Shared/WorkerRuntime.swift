import Darwin
import Foundation

// One worker runs one tool at a time. Spawn, signal and reap share this lock,
// so cancellation can only address our own child before its PID is reusable.
enum WorkerTools {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var child: pid_t = 0
    nonisolated(unsafe) private static var cancelled = false

    static func spawn(_ executable: String, actions: inout posix_spawn_file_actions_t?,
                      argv: inout [UnsafeMutablePointer<CChar>?], envp: inout [UnsafeMutablePointer<CChar>?]) throws -> pid_t {
        try lock.withLock {
            guard !cancelled else { throw CancellationError() }
            guard child == 0 else { throw CaptureError.writerFailure("A worker tool is already running") }
            var pid: pid_t = 0
            let status = posix_spawn(&pid, executable, &actions, nil, &argv, &envp)
            guard status == 0 else { throw CaptureError.writerFailure("Could not launch tool: \(String(cString: strerror(status)))") }
            child = pid
            return pid
        }
    }

    static func wait(_ pid: pid_t) throws -> Int32 {
        while true {
            let result: Int32? = try lock.withLock {
                var status: Int32 = 0
                let result = waitpid(pid, &status, WNOHANG)
                if result == pid {
                    child = 0
                    if cancelled { throw CancellationError() }
                    return status
                }
                if result < 0 && errno != EINTR {
                    if errno == ECHILD { child = 0 }
                    throw CaptureError.writerFailure("Could not wait for tool")
                }
                return nil
            }
            if let result { return result }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    static func checkCancellation() throws {
        try lock.withLock { if cancelled { throw CancellationError() } }
    }

    static func cancel() {
        lock.withLock {
            cancelled = true
            if child > 0 { _ = kill(child, SIGTERM) }
        }
        Thread.sleep(forTimeInterval: 2)
        lock.withLock {
            // wait() cannot have reaped this PID without clearing child first.
            if child > 0 { _ = kill(child, SIGKILL) }
        }
    }
}

enum WorkerLifetime {
    static func monitor(onExit: @escaping @Sendable () -> Void = {}) {
        DispatchQueue.global(qos: .utility).async {
            _ = FileHandle.standardInput.readDataToEndOfFile()
            WorkerTools.cancel()
            onExit()
            _exit(EXIT_SUCCESS)
        }
    }
}

// A timer would keep a hung worker alive. Update only when work advances.
enum WorkerProgress {
    static let url = ProcessInfo.processInfo.environment["MEETING_CAPTURE_PROGRESS_FILE"].map { URL(fileURLWithPath: $0) }
    static func touch() {
        if let url { try? Data("work=\(DispatchTime.now().uptimeNanoseconds)".utf8).write(to: url) }
    }

    static func signature(at url: URL) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let end = try? handle.seekToEnd() else { return nil }
        try? handle.seek(toOffset: end > 4096 ? end - 4096 : 0)
        guard let data = try? handle.read(upToCount: 4096), let text = String(data: data, encoding: .utf8) else { return nil }
        // ffmpeg appends reports even when the timestamp has not advanced.
        // File modification alone is therefore not evidence of progress.
        if let time = text.split(separator: "\n").last(where: { $0.hasPrefix("out_time_us=") }) { return String(time) }
        return text.hasPrefix("work=") ? text : nil
    }
}
