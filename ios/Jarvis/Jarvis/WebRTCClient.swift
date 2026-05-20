import AVFoundation
import Foundation
import WebRTC

/// Thin wrapper around RTCPeerConnection for one Jarvis call.
///
/// Lifecycle:
///   1. connect(serverURL:) creates the peer connection, adds the local mic
///      track, generates an SDP offer, waits for ICE gathering to complete,
///      POSTs the offer to /api/offer on the Mac, applies the answer.
///   2. WebRTC handles audio in/out from then on. Remote audio plays through
///      RTCAudioSession (which CallKit configures to .voiceChat).
///   3. close() tears down the peer connection.
///
/// We let WebRTC manage the audio session itself (useManualAudio = false,
/// the default). CallKit already swings AVAudioSession into .voiceChat
/// mode in provider(_:didActivate:); WebRTC sees that and starts capture.
/// Manual-audio mode is the more "correct" CallKit integration but ships
/// with subtle timing bugs around isAudioEnabled vs. the audio unit start;
/// the auto path is more forgiving and works fine for a personal app.
final class WebRTCClient: NSObject {
    static let factory: RTCPeerConnectionFactory = {
        RTCInitializeSSL()
        let videoEncoder = RTCDefaultVideoEncoderFactory()
        let videoDecoder = RTCDefaultVideoDecoderFactory()
        return RTCPeerConnectionFactory(encoderFactory: videoEncoder, decoderFactory: videoDecoder)
    }()

    private var peerConnection: RTCPeerConnection?
    private var localAudioTrack: RTCAudioTrack?

    var onConnectionStateChange: ((String) -> Void)?

    /// CallKit gives us a configured AVAudioSession. Tell RTCAudioSession the
    /// system session is now active, then re-apply the category with our
    /// preferred output routing: AirPods/Bluetooth when present, otherwise
    /// the iPhone loudspeaker (not the small earpiece receiver, which is
    /// what voiceChat mode picks by default on a real phone call).
    static func activateAudioSession(_ audioSession: AVAudioSession) {
        let rtcSession = RTCAudioSession.sharedInstance()
        rtcSession.lockForConfiguration()
        rtcSession.audioSessionDidActivate(audioSession)
        do {
            // .defaultToSpeaker = route to the loudspeaker when nothing
            // else is connected. AirPods / wired headset / car Bluetooth
            // take precedence automatically via the AVAudioSession
            // route picker. videoChat mode (not voiceChat) keeps the
            // category from forcing the small receiver — voiceChat tells
            // iOS "phone call" semantics, which on real phones pins the
            // earpiece even with .defaultToSpeaker set. videoChat keeps
            // the AEC/AGC but lets the speaker route win.
            try rtcSession.setCategory(
                AVAudioSession.Category.playAndRecord,
                mode: AVAudioSession.Mode.videoChat,
                options: [
                    .allowBluetooth,
                    .allowBluetoothA2DP,
                    .defaultToSpeaker,
                ]
            )
        } catch {
            print("audio session category configure failed: \(error)")
        }
        rtcSession.isAudioEnabled = true
        rtcSession.unlockForConfiguration()
    }

    static func deactivateAudioSession(_ audioSession: AVAudioSession) {
        let rtcSession = RTCAudioSession.sharedInstance()
        rtcSession.lockForConfiguration()
        rtcSession.isAudioEnabled = false
        rtcSession.audioSessionDidDeactivate(audioSession)
        rtcSession.unlockForConfiguration()
    }

    /// Mute or unmute the local mic. Cheaper than toggling the audio
    /// session — we just flip the track's isEnabled flag, which keeps
    /// the SRTP path open and avoids re-negotiating WebRTC state.
    func setMuted(_ muted: Bool) {
        localAudioTrack?.isEnabled = !muted
    }

    /// Flip between hands-free loudspeaker and the default route (earpiece
    /// when nothing is plugged in, AirPods / wired headset when they are).
    /// Mirrors the Speaker button in the iOS phone-call UI.
    static func setSpeakerphone(enabled: Bool) {
        let rtcSession = RTCAudioSession.sharedInstance()
        rtcSession.lockForConfiguration()
        do {
            try rtcSession.overrideOutputAudioPort(enabled ? .speaker : .none)
        } catch {
            print("overrideOutputAudioPort failed: \(error)")
        }
        rtcSession.unlockForConfiguration()
    }

    func connect(serverURL: String) async throws {
        let config = RTCConfiguration()
        config.iceServers = [RTCIceServer(urlStrings: ["stun:stun.l.google.com:19302"])]
        config.sdpSemantics = .unifiedPlan

        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let pc = WebRTCClient.factory.peerConnection(with: config, constraints: constraints, delegate: self) else {
            throw NSError(domain: "Jarvis", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to create RTCPeerConnection"])
        }
        self.peerConnection = pc

        // Local mic. pc.add() in unified-plan creates a sendRecv transceiver
        // for the track automatically.
        let audioConstraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        let audioSource = WebRTCClient.factory.audioSource(with: audioConstraints)
        let audioTrack = WebRTCClient.factory.audioTrack(with: audioSource, trackId: "audio0")
        self.localAudioTrack = audioTrack
        audioTrack.isEnabled = true
        pc.add(audioTrack, streamIds: ["stream0"])

        let offerConstraints = RTCMediaConstraints(
            mandatoryConstraints: [
                "OfferToReceiveAudio": "true",
                "OfferToReceiveVideo": "false",
            ],
            optionalConstraints: nil
        )
        let offer: RTCSessionDescription = try await withCheckedThrowingContinuation { cont in
            pc.offer(for: offerConstraints) { sdp, err in
                if let sdp = sdp { cont.resume(returning: sdp) }
                else { cont.resume(throwing: err ?? NSError(domain: "Jarvis", code: -2)) }
            }
        }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            pc.setLocalDescription(offer) { err in
                if let err = err { cont.resume(throwing: err) } else { cont.resume() }
            }
        }

        // Wait for ICE gathering to finish (non-trickle: simplest signaling).
        try await waitForICEGatheringComplete()

        guard let url = URL(string: serverURL.trimmingCharacters(in: .whitespaces) + "/api/offer") else {
            throw NSError(domain: "Jarvis", code: -3, userInfo: [NSLocalizedDescriptionKey: "Bad server URL"])
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.addValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = [
            "sdp": pc.localDescription?.sdp ?? offer.sdp,
            "type": "offer",
        ]
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let detail = String(data: data, encoding: .utf8) ?? "<empty>"
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw NSError(domain: "Jarvis", code: -4, userInfo: [NSLocalizedDescriptionKey: "Signaling HTTP \(code): \(detail)"])
        }
        let answerDict = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        guard let answerSDP = answerDict["sdp"] as? String, let answerType = answerDict["type"] as? String else {
            throw NSError(domain: "Jarvis", code: -5, userInfo: [NSLocalizedDescriptionKey: "Malformed answer: \(answerDict)"])
        }
        let typeEnum: RTCSdpType = answerType == "answer" ? .answer : .prAnswer
        let answer = RTCSessionDescription(type: typeEnum, sdp: answerSDP)
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            pc.setRemoteDescription(answer) { err in
                if let err = err { cont.resume(throwing: err) } else { cont.resume() }
            }
        }
    }

    private func waitForICEGatheringComplete() async throws {
        guard let pc = peerConnection else { return }
        if pc.iceGatheringState == .complete { return }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            iceGatheringContinuation = cont
            // Safety timeout — if gathering hangs (rare on cellular), we still
            // ship whatever we've got after 3 seconds.
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.resumeICEGatheringIfPending()
            }
        }
    }

    private var iceGatheringContinuation: CheckedContinuation<Void, Error>?

    private func resumeICEGatheringIfPending() {
        if let cont = iceGatheringContinuation {
            iceGatheringContinuation = nil
            cont.resume()
        }
    }

    func close() {
        peerConnection?.close()
        peerConnection = nil
        localAudioTrack = nil
    }
}

extension WebRTCClient: RTCPeerConnectionDelegate {
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
        onConnectionStateChange?("ice=\(newState.rawValue)")
    }
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {
        if newState == .complete {
            DispatchQueue.main.async { [weak self] in
                self?.resumeICEGatheringIfPending()
            }
        }
    }
    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCPeerConnectionState) {
        onConnectionStateChange?("pc=\(newState.rawValue)")
    }
}
