import AVFoundation
import Foundation
import Speech

/// Speak to the swarm: press the microphone, talk, and your words appear in the box as you go; a short pause sends them. Uses macOS's own
/// speech recognition (on this computer when it can), so the Swarm app needs no audio stack of its own.
@MainActor
final class SwarmSpeech: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    @Published var level: Double = 0                 // 0...1, drives the ribbons while you speak
    var problem: ((String) -> Void)?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silence: Timer?
    private var onFinal: ((String) -> Void)?
    private let pause: TimeInterval = 1.4            // this much quiet after you speak = you are done

    func toggle(onFinal: @escaping (String) -> Void) {
        if listening { finish(send: true) } else { begin(onFinal) }
    }

    private func begin(_ onFinal: @escaping (String) -> Void) {
        self.onFinal = onFinal
        SFSpeechRecognizer.requestAuthorization { status in
            AVCaptureDevice.requestAccess(for: .audio) { micOK in
                Task { @MainActor in
                    guard status == .authorized, micOK else {
                        self.problem?("Allow Speech Recognition and Microphone for Prometheus Swarm in System Settings > Privacy & Security.")
                        return
                    }
                    self.start()
                }
            }
        }
    }

    private func start() {
        guard let recognizer, recognizer.isAvailable else { problem?("Speech recognition isn't available right now."); return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        request = req
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { problem?("No microphone is available."); return }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            req.append(buffer)
            var sum: Float = 0
            if let ch = buffer.floatChannelData?[0] {
                let n = Int(buffer.frameLength)
                for i in 0..<n { sum += ch[i] * ch[i] }
                let rms = sqrt(sum / Float(max(n, 1)))
                Task { @MainActor in self?.level = min(1, Double(rms) * 8) }
            }
        }
        engine.prepare()
        do { try engine.start() } catch { problem?("Couldn't start the microphone."); cleanup(); return }
        heard = ""
        listening = true
        armSilence()
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            Task { @MainActor in
                guard let self, self.listening else { return }
                if let result {
                    self.heard = result.bestTranscription.formattedString
                    self.armSilence()
                    if result.isFinal { self.finish(send: true) }
                } else if error != nil {
                    self.finish(send: !self.heard.isEmpty)
                }
            }
        }
    }

    private func armSilence() {
        silence?.invalidate()
        silence = Timer.scheduledTimer(withTimeInterval: pause, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.finish(send: true) }
        }
    }

    private func finish(send: Bool) {
        guard listening else { return }
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        cleanup()
        if send, !text.isEmpty { onFinal?(text) }
    }

    private func cleanup() {
        silence?.invalidate(); silence = nil
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio(); request = nil
        task?.cancel(); task = nil
        listening = false
        level = 0
    }
}
