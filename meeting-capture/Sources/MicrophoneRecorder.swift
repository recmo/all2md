@preconcurrency import AVFoundation
import AudioToolbox
import CoreAudio
import CoreMedia
import Foundation

final class MicrophoneRecorder: @unchecked Sendable {
    private var engine: AVAudioEngine?
    private var writer: BufferedAudioWriter?
    private let lock = NSLock()
    private var writeFailure: Error?
    private var warning: String?
    var onError: (@Sendable (Error) -> Void)?
    var failure: Error? { lock.withLock { writeFailure } }
    var captureWarning: String? { lock.withLock { warning } }
    var callbackAge: TimeInterval? { writer?.callbackAge }
    var level: Float { writer?.level ?? 0 }
    var isRunning: Bool { engine?.isRunning == true }
    var diagnosticSummary: String { "running=\(isRunning) \(writer?.diagnosticSummary ?? "writer=none")" }

    func start(to url: URL, deviceID: AudioDeviceID?, startTime: CMTime) throws {
        stop()
        lock.withLock { writeFailure = nil; warning = nil }
        let engine = AVAudioEngine() // Resolve disconnected devices through a fresh engine.
        self.engine = engine
        let onError = self.onError
        do {
            let input = engine.inputNode
            if let deviceID {
                guard let audioUnit = input.audioUnit else {
                    throw CaptureError.deviceSelectionFailure("audio input unit is unavailable")
                }
                var selectedDeviceID = deviceID
                let status = AudioUnitSetProperty(
                    audioUnit,
                    kAudioOutputUnitProperty_CurrentDevice,
                    kAudioUnitScope_Global,
                    0,
                    &selectedDeviceID,
                    UInt32(MemoryLayout<AudioDeviceID>.size)
                )
                guard status == noErr else {
                    throw CaptureError.deviceSelectionFailure("Core Audio error \(status) selecting device \(deviceID)")
                }
            }

            let inputFormat = input.outputFormat(forBus: 0)
            guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
                throw CaptureError.noMicrophone
            }
            CaptureDiagnostics.log("microphone.format device=\(deviceID ?? 0) format=\(inputFormat)")
            let writer = try BufferedAudioWriter(format: inputFormat, url: url, startTime: startTime.seconds)
            writer.onError = { [weak self] error in
                guard let self else { return }
                self.lock.withLock { self.writeFailure = error }
                onError?(error)
            }
            writer.onWarning = { [weak self] message in
                guard let self else { return }
                self.lock.withLock { self.warning = message }
            }
            self.writer = writer
            input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, time in
                var timestamp = AudioTimeStamp()
                timestamp.mHostTime = time.hostTime
                if time.isHostTimeValid { timestamp.mFlags = .hostTimeValid }
                writer.enqueue(buffer.audioBufferList, timestamp: timestamp)
            }
            engine.prepare()
            try engine.start()
            CaptureDiagnostics.log("microphone.started device=\(deviceID ?? 0)")
        } catch { stop(); throw error }
    }

    func stopInput() {
        engine?.stop()
        if writer != nil { engine?.inputNode.removeTap(onBus: 0) }
        engine = nil
    }

    func stop() {
        stopInput()
        writer?.finish()
        writer = nil
    }
}
