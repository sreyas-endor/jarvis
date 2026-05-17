import AVFoundation
import SwiftUI

@main
struct JarvisApp: App {
    @StateObject private var callManager = CallManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(callManager)
                .task {
                    // Trigger the iOS microphone permission prompt on first
                    // launch. WebRTC won't request mic permission on its own
                    // when CallKit is in the loop, and without explicit
                    // permission the OS silently refuses capture and the
                    // call connects with no audio uplink.
                    _ = await AVAudioApplication.requestRecordPermission()
                }
        }
    }
}
