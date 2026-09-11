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
    let executableURL: URL

    init(executableURL: URL? = nil) throws {
        let candidate = executableURL ?? Bundle.main.bundleURL
            .appending(path: "Contents/Helpers/MeetingCaptureWorker")
        guard FileManager.default.isExecutableFile(atPath: candidate.path) else {
            throw BackgroundWorkerError.unavailable
        }
        self.executableURL = candidate
    }

    func finalize(_ request: FinalizeCaptureRequest) async throws -> URL? {
        let result: WorkerManifestResult = try await run(command: "finalize", request: request)
        return result.manifest
    }

    func checkAccess(_ request: WorkerAccessRequest) async throws -> WorkerAccessReport {
        try await run(command: "check-access", request: request)
    }

    func checkMetadataAccess(_ request: WorkerAccessRequest) async throws -> WorkerAccessReport {
        try await run(command: "check-metadata-access", request: request, timeout: 5)
    }

    func recover(_ request: RecoverCaptureRequest) async throws -> URL? {
        let result: WorkerManifestResult = try await run(command: "recover", request: request)
        return result.manifest
    }

    func verifyCrashIsolation() async throws {
        let invocation = WorkerInvocation(
            executableURL: executableURL,
            arguments: ["crash-test"],
            responseURL: nil
        )
        _ = try await Task.detached { try invocation.execute() }.value
    }

    @MainActor
    func startAccessibilityProbe(
        _ request: AccessibilityProbeRequest,
        onUnexpectedExit: @escaping @MainActor (BackgroundWorkerError) -> Void
    ) throws -> BackgroundWorkerProcess {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MeetingCaptureWorker-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let requestURL = directory.appending(path: "request.json")
        try JSONEncoder().encode(request).write(to: requestURL, options: .atomic)
        let process = Process()
        process.executableURL = executableURL
        process.arguments = ["accessibility-probe", requestURL.path]
        process.qualityOfService = .utility
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let handle = BackgroundWorkerProcess(process: process, temporaryDirectory: directory, onUnexpectedExit: onUnexpectedExit)
        do { try process.run() }
        catch {
            try? FileManager.default.removeItem(at: directory)
            throw BackgroundWorkerError.launch(error.localizedDescription)
        }
        return handle
    }

    private func run<Request: Encodable, Value: Codable>(command: String, request: Request, timeout: TimeInterval? = nil) async throws -> Value {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MeetingCaptureWorker-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let requestURL = directory.appending(path: "request.json")
        let responseURL = directory.appending(path: "response.json")
        try JSONEncoder().encode(request).write(to: requestURL, options: .atomic)
        let invocation = WorkerInvocation(
            executableURL: executableURL,
            arguments: [command, requestURL.path, responseURL.path],
            responseURL: responseURL,
            timeout: timeout
        )
        defer { try? FileManager.default.removeItem(at: directory) }
        let data = try await Task.detached { try invocation.execute() }.value
        guard let data else { throw BackgroundWorkerError.invalidResponse }
        let response = try JSONDecoder().decode(WorkerResponse<Value>.self, from: data)
        if let error = response.error { throw BackgroundWorkerError.operation(error) }
        guard let value = response.value else { throw BackgroundWorkerError.invalidResponse }
        return value
    }
}

private struct WorkerInvocation: Sendable {
    let executableURL: URL
    let arguments: [String]
    let responseURL: URL?
    var timeout: TimeInterval? = nil

    func execute() throws -> Data? {
        let process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        process.qualityOfService = .utility
        let errorPipe = Pipe()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errorPipe
        do { try process.run() }
        catch { throw BackgroundWorkerError.launch(error.localizedDescription) }
        if let timeout {
            let deadline = ProcessInfo.processInfo.systemUptime + timeout
            while process.isRunning, ProcessInfo.processInfo.systemUptime < deadline { Thread.sleep(forTimeInterval: 0.02) }
            if process.isRunning {
                // This is our own, still-unreaped child, so its PID cannot be reused.
                kill(process.processIdentifier, SIGKILL)
                process.waitUntilExit()
                throw BackgroundWorkerError.operation("Optional metadata permission check timed out.")
            }
        }
        process.waitUntilExit()
        let standardError = errorPipe.fileHandleForReading.readDataToEndOfFile()
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

@MainActor
final class BackgroundWorkerProcess {
    private let process: Process
    private let temporaryDirectory: URL
    private var expectedTermination = false

    init(
        process: Process,
        temporaryDirectory: URL,
        onUnexpectedExit: @escaping @MainActor (BackgroundWorkerError) -> Void
    ) {
        self.process = process
        self.temporaryDirectory = temporaryDirectory
        process.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self else { return }
                try? FileManager.default.removeItem(at: self.temporaryDirectory)
                guard !self.expectedTermination, process.terminationStatus != 0 else { return }
                onUnexpectedExit(.crashed(
                    command: "accessibility-probe",
                    reason: process.terminationReason,
                    status: process.terminationStatus
                ))
            }
        }
    }

    @discardableResult
    func terminate() -> Int32? {
        expectedTermination = true
        let processID = process.processIdentifier
        guard process.isRunning else { return nil }
        process.terminate()
        return processID
    }
}
