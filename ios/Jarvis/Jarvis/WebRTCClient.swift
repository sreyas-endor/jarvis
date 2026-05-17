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
///      RTCAudioSession (which CallKit has already configured to .voiceChat).
///   3. close() tears down the peer connection.
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

    static func activateAudioSession() {
        let rtcSession = RTCAudioSession.sharedInstance()
        rtcSession.lockForConfiguration()
        do {
            // CallKit already set category/mode on the underlying AVAudioSession;
            // we just need RTCAudioSession to acknowledge the system audio
            // session is "active" so it routes capture/playback through it.
            try rtcSession.setCategory(AVAudioSession.Category.playAndRecord, mode: AVAudioSession.Mode.voiceChat, options: [.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker])
            try rtcSession.setActive(true)
        } catch {
            print("RTCAudioSession activate failed: \(error)")
        }
        rtcSession.unlockForConfiguration()
    }

    static func deactivateAudioSession() {
        let rtcSession = RTCAudioSession.sharedInstance()
        rtcSession.lockForConfiguration()
        try? rtcSession.setActive(false)
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

        // Local mic
        let audioConstraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        let audioSource = WebRTCClient.factory.audioSource(with: audioConstraints)
        let audioTrack = WebRTCClient.factory.audioTrack(with: audioSource, trackId: "audio0")
        self.localAudioTrack = audioTrack
        pc.add(audioTrack, streamIds: ["stream0"])

        // Make sure we negotiate to *receive* audio from the bot too.
        let transceiverInit = RTCRtpTransceiverInit()
        transceiverInit.direction = .sendRecv
        _ = pc.addTransceiver(of: .audio, init: transceiverInit)

        // Offer
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

        // POST to Mac. Use pc.localDescription (which now has the final SDP
        // with all gathered candidates baked in) rather than the offer we
        // generated before gathering.
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
