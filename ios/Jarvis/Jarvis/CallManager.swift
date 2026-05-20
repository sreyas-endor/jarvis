import AVFoundation
import CallKit
import Combine
import Foundation

/// Bridges the SwiftUI Call/Hang-up buttons to CallKit + WebRTC.
///
/// CallKit gives us three free things on the free Apple Developer tier:
///   1. Native iOS in-call UI on the lock screen.
///   2. AVAudioSession mode = .voiceChat automatically — iOS ducks Apple Music
///      and other audio sources while the call is up.
///   3. Routing to AirPods/Bluetooth handled by the system, no manual code.
///
/// We model the call as an *outgoing* CXCall ("Call Jarvis" button). CallKit
/// also supports inbound calls, but those require VoIP push (paid tier).
final class CallManager: NSObject, ObservableObject {
    @Published var statusText: String = "Tap to call Jarvis."
    @Published var isInCall: Bool = false
    @Published var speakerOn: Bool = false
    @Published var muted: Bool = false

    private let provider: CXProvider
    private let callController = CXCallController()
    private var currentCallUUID: UUID?
    private var webRTC: WebRTCClient?
    private var pendingServerURL: String?

    override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.maximumCallGroups = 1
        config.supportedHandleTypes = [.generic]
        // Showing as a real "call" makes iOS treat us as voiceChat audio —
        // that's what triggers Music ducking + lock-screen UI.
        self.provider = CXProvider(configuration: config)
        super.init()
        self.provider.setDelegate(self, queue: nil)
    }

    func startCall(serverURL: String) {
        guard currentCallUUID == nil else { return }
        let uuid = UUID()
        currentCallUUID = uuid
        pendingServerURL = serverURL

        let handle = CXHandle(type: .generic, value: "Jarvis")
        let startCall = CXStartCallAction(call: uuid, handle: handle)
        let transaction = CXTransaction(action: startCall)

        statusText = "Connecting…"
        callController.request(transaction) { [weak self] error in
            if let error = error {
                DispatchQueue.main.async {
                    self?.statusText = "Call failed: \(error.localizedDescription)"
                    self?.currentCallUUID = nil
                }
            }
        }
    }

    func endCall() {
        guard let uuid = currentCallUUID else { return }
        let endCall = CXEndCallAction(call: uuid)
        let transaction = CXTransaction(action: endCall)
        callController.request(transaction) { _ in }
    }

    func toggleSpeaker() {
        let newValue = !speakerOn
        WebRTCClient.setSpeakerphone(enabled: newValue)
        DispatchQueue.main.async {
            self.speakerOn = newValue
        }
    }

    func toggleMute() {
        let newValue = !muted
        webRTC?.setMuted(newValue)
        DispatchQueue.main.async {
            self.muted = newValue
        }
    }

    private func teardown(reason: String) {
        webRTC?.close()
        webRTC = nil
        currentCallUUID = nil
        pendingServerURL = nil
        DispatchQueue.main.async {
            self.isInCall = false
            self.speakerOn = false
            self.muted = false
            self.statusText = reason
        }
    }
}

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        teardown(reason: "Reset.")
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        guard let serverURL = pendingServerURL else {
            action.fail()
            return
        }
        // Tell CallKit the call is "starting"; the in-call UI appears now.
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)

        Task { @MainActor in
            do {
                let client = WebRTCClient()
                self.webRTC = client
                client.onConnectionStateChange = { [weak self] state in
                    DispatchQueue.main.async {
                        self?.statusText = "WebRTC: \(state)"
                    }
                }
                try await client.connect(serverURL: serverURL)

                provider.reportOutgoingCall(with: action.callUUID, connectedAt: nil)
                self.isInCall = true
                self.statusText = "Connected. Talk to Jarvis."
                action.fulfill()
            } catch {
                self.statusText = "Connect failed: \(error.localizedDescription)"
                action.fail()
                self.teardown(reason: "Connect failed.")
            }
        }
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        teardown(reason: "Call ended.")
        action.fulfill()
    }

    /// CallKit hands us a configured AVAudioSession when the call is active.
    /// WebRTC's RTCAudioSession is the one that actually plays/captures, but it
    /// shares the underlying AVAudioSession. Mode = .voiceChat is what triggers
    /// system audio ducking (Apple Music, podcasts, etc.).
    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        WebRTCClient.activateAudioSession(audioSession)
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        WebRTCClient.deactivateAudioSession(audioSession)
    }
}
