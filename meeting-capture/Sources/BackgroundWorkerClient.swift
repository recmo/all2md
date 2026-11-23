import Foundation
import Darwin

enum BackgroundWorkerError: LocalizedError {
    case unavailable
    case launch(String)
    case crashed(command: String, reason: Process.TerminationReason, status: Int32)
    case invalidResponse
    case operation(String)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            "The Meeting Capture background worker is unavailable."
        case let .launch(message):
            "Could not launch the Meeting Capture background worker: \(message)"
        case let .crashed(command, reason, status):
            "Background \(command) worker terminated (\(reason == .uncaughtSignal ? "signal" : "exit") \(status)). Live recording was not affected."
        case .invalidResponse:
            "The Meeting Capture background worker returned an invalid response."
        case let .operation(message):
            message
        }
    }
}

struct BackgroundWorkerClient: Sendable {
    private static let archiveQueue = DispatchQueue(label: "ventures.wicked.MeetingCapture.archives", qos: .utility)
    let executableURL: URL

    init(executableURL: URL? = nil) throws {
        let candidate = executableURL ?? Bundle.main.bundleURL
            .appending(path: "Contents/Helpers/MeetingCaptureWorker")
        guard FileManager.default.isExecutableFile(atPath: candidate.path) else {
            throw BackgroundWorkerError.unavailable
        }
        self.executableURL = candidate
    }

    func finalize(_ request: RecordingSession) async throws -> URL {
        try await run(command: "finalize", request: request, timeout: 120, archive: true)
    }

    func checkAccess(_ request: WorkerAccessRequest) async throws -> WorkerAccessReport {
        try await run(command: "check-access", request: request, timeout: 15)
    }

    func checkMetadataAccess(_ request: WorkerAccessRequest) async throws -> WorkerAccessReport {
        try await run(command: "check-metadata-access", request: request, timeout: 5)
    }

    func checkPermissions(root: URL) async throws -> WorkerPermissionReport {
        try await run(command: "check-permissions", request: root, timeout: 5)
    }

    func recover(_ request: RecoverCaptureRequest) async throws -> URL {
        try await run(command: "recover", request: request, timeout: 120, archive: true)
    }

    func verifyCrashIsolation() async throws {
        let invocation = WorkerInvocation(
            executableURL: executableURL,
            arguments: ["crash-test"],
            responseURL: nil, timeout: 5
        )
        _ = try await execute(invocation, queue: .global(qos: .utility))
    }

    func startAccessibilityProbe(
        _ request: AccessibilityProbeRequest,
        onUnexpectedExit: @escaping @Sendable (BackgroundWorkerError) -> Void
    ) throws -> BackgroundWorkerProcess {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MeetingCaptureWorker-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let requestURL = directory.appending(path: "request.json")
        do { try JSONEncoder().encode(request).write(to: requestURL, options: .atomic) }
        catch { try? FileManager.default.removeItem(at: directory); throw error }
        let process = Process()
        process.executableURL = executableURL
        process.arguments = ["accessibility-probe", requestURL.path]
        process.qualityOfService = .utility
        // Only the recorder holds the write end. EOF also stops a probe whose
        // main thread is stuck in Accessibility when the recorder crashes.
        let lifetime = Pipe()
        guard fcntl(lifetime.fileHandleForWriting.fileDescriptor, F_SETFD, FD_CLOEXEC) == 0 else {
            try? FileManager.default.removeItem(at: directory)
            throw BackgroundWorkerError.launch("Could not isolate the metadata worker lifetime pipe")
        }
        defer { try? lifetime.fileHandleForReading.close() }
        process.standardInput = lifetime.fileHandleForReading
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let handle = BackgroundWorkerProcess(
            process: process, temporaryDirectory: directory,
            lifetimeWriter: lifetime.fileHandleForWriting, onUnexpectedExit: onUnexpectedExit
        )
        do { try process.run() }
        catch {
            try? lifetime.fileHandleForWriting.close()
            try? FileManager.default.removeItem(at: directory)
            throw BackgroundWorkerError.launch(error.localizedDescription)
        }
        return handle
    }

    private func run<Request: Encodable, Value: Codable>(
        command: String, request: Request, timeout: TimeInterval, archive: Bool = false
    ) async throws -> Value {
        try Task.checkCancellation()
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MeetingCaptureWorker-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let requestURL = directory.appending(path: "request.json")
        let responseURL = directory.appending(path: "response.json")
        try JSONEncoder().encode(request).write(to: requestURL, options: .atomic)
        let invocation = WorkerInvocation(
            executableURL: executableURL,
            arguments: [command, requestURL.path, responseURL.path], responseURL: responseURL,
            timeout: timeout, progressURL: archive ? directory.appending(path: "progress") : nil
        )
        let data = try await execute(invocation, queue: archive ? Self.archiveQueue : .global(qos: .utility))
        guard let data else { throw BackgroundWorkerError.invalidResponse }
        let response = try JSONDecoder().decode(WorkerResponse<Value>.self, from: data)
        if let error = response.error { throw BackgroundWorkerError.operation(error) }
        guard let value = response.value else { throw BackgroundWorkerError.invalidResponse }
        return value
    }

    private func execute(_ invocation: WorkerInvocation, queue: DispatchQueue) async throws -> Data? {
        let cancellation = WorkerCancellation()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.async {
                    do { continuation.resume(returning: try invocation.execute(cancellation: cancellation)) }
                    catch { continuation.resume(throwing: error) }
                }
            }
        } onCancel: { cancellation.cancel() }
    }
}

private final class WorkerCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    var isCancelled: Bool { lock.withLock { cancelled } }
    func cancel() { lock.withLock { cancelled = true } }
}

private struct WorkerInvocation: Sendable {
    let executableURL: URL
    let arguments: [String]
    let responseURL: URL?
    let timeout: TimeInterval
    var progressURL: URL? = nil

    func execute(cancellation: WorkerCancellation) throws -> Data? {
        if cancellation.isCancelled { throw CancellationError() }
        let process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        process.qualityOfService = .utility
        // A worker can fill a pipe before exiting, or leave an inherited pipe
        // open in a subprocess. Neither may defeat the startup deadline.
        guard let errorStream = tmpfile() else {
            throw BackgroundWorkerError.launch("Could not capture worker errors: \(String(cString: strerror(errno)))")
        }
        defer { fclose(errorStream) }
        let errorFile = FileHandle(fileDescriptor: fileno(errorStream), closeOnDealloc: false)
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errorFile
        // EOF asks the worker to stop its own tool. The worker signals only a
        // positive child PID while holding its spawn/reap lock.
        let lifetime = Pipe()
        defer { try? lifetime.fileHandleForWriting.close() }
        guard fcntl(lifetime.fileHandleForWriting.fileDescriptor, F_SETFD, FD_CLOEXEC) == 0 else {
            throw BackgroundWorkerError.launch("Could not create worker lifetime pipe")
        }
        process.standardInput = lifetime.fileHandleForReading
        if let progressURL {
            try Data().write(to: progressURL)
            var environment = ProcessInfo.processInfo.environment
            environment["MEETING_CAPTURE_PROGRESS_FILE"] = progressURL.path
            process.environment = environment
        }
        do { try process.run() }
        catch { throw BackgroundWorkerError.launch(error.localizedDescription) }
        try? lifetime.fileHandleForReading.close()
        var deadline = ProcessInfo.processInfo.systemUptime + timeout
        var lastProgress: String?
        while process.isRunning {
            if let progressURL,
               let progress = WorkerProgress.signature(at: progressURL),
               progress != lastProgress {
                lastProgress = progress
                deadline = ProcessInfo.processInfo.systemUptime + timeout
            }
            if cancellation.isCancelled || ProcessInfo.processInfo.systemUptime >= deadline {
                try? lifetime.fileHandleForWriting.close()
                process.waitUntilExit()
                if cancellation.isCancelled { throw CancellationError() }
                throw BackgroundWorkerError.operation("Background \(arguments.first ?? "unknown") made no progress for \(Int(timeout)) seconds. Original audio was kept; retry recovery.")
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        process.waitUntilExit()
        try errorFile.seek(toOffset: 0)
        let standardError = errorFile.readDataToEndOfFile()
        guard process.terminationStatus == 0 else {
            if process.terminationReason == .exit,
               let message = String(data: standardError, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               !message.isEmpty {
                throw BackgroundWorkerError.operation(message)
            }
            throw BackgroundWorkerError.crashed(
                command: arguments.first ?? "unknown",
                reason: process.terminationReason,
                status: process.terminationStatus
            )
        }
        guard let responseURL else { return nil }
        return try Data(contentsOf: responseURL)
    }
}

final class BackgroundWorkerProcess: @unchecked Sendable {
    private let lock = NSLock()
    private let process: Process
    private let lifetimeWriter: FileHandle
    private var expectedTermination = false

    init(
        process: Process,
        temporaryDirectory: URL,
        lifetimeWriter: FileHandle,
        onUnexpectedExit: @escaping @Sendable (BackgroundWorkerError) -> Void
    ) {
        self.process = process
        self.lifetimeWriter = lifetimeWriter
        process.terminationHandler = { [weak self] process in
            // The coordinator releases its handle immediately after stopping.
            // Cleanup and the lifetime pipe must outlive that handle.
            try? lifetimeWriter.close()
            try? FileManager.default.removeItem(at: temporaryDirectory)
            guard let self, !self.lock.withLock({ self.expectedTermination }),
                  process.terminationStatus != 0 else { return }
            onUnexpectedExit(.crashed(
                command: "accessibility-probe", reason: process.terminationReason,
                status: process.terminationStatus
            ))
        }
    }

    func terminate() {
        guard lock.withLock({
            if expectedTermination { return false }
            expectedTermination = true
            return true
        }) else { return }
        guard process.isRunning else { return }
        process.terminate()
        // Give the probe time to flush its final snapshot. Closing our pipe
        // then stops even a hung AX call, without signalling a saved PID.
        let writer = lifetimeWriter
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) { try? writer.close() }
    }
}
