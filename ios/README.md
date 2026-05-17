# Jarvis iOS app

Native iOS client. SwiftUI + CallKit + WebRTC. Sideloaded with a free Apple ID
(no $99 developer membership needed).

## Files in this directory

```
ios/
├── README.md                  ← you are here
└── Jarvis/
    ├── JarvisApp.swift        App entry point
    ├── ContentView.swift      Call / Hang up button
    ├── CallManager.swift      CallKit integration (CXProvider + CXCallController)
    ├── WebRTCClient.swift     RTCPeerConnection + SDP exchange with the Mac
    └── Info.plist.additions.txt  privacy strings + background modes to add in Xcode
```

## One-time setup

### 1. Install Xcode (free, App Store)
Apple ID is enough — you don't need to enrol in the paid Developer Program.

### 2. Create the project

1. Open Xcode → **File → New → Project…**
2. iOS → **App** → Next.
3. Settings:
   - Product Name: `Jarvis`
   - Interface: **SwiftUI**
   - Language: **Swift**
   - Use Core Data: **off**
   - Include Tests: **off**
4. Save the project inside `ios/` — choose this very `ios/` directory. Xcode
   will create `ios/Jarvis.xcodeproj` alongside the existing `Jarvis/`
   source folder.
5. **Important**: when the wizard creates default files (`JarvisApp.swift`,
   `ContentView.swift`), Xcode wants to put them in `ios/Jarvis/`. The
   files in this repo already live there — let Xcode overwrite them, or
   first move ours aside, let Xcode create its skeleton, then replace its
   files with ours via Finder.

After the project exists, drag `CallManager.swift` and `WebRTCClient.swift`
from Finder into the Xcode project navigator (target membership: Jarvis).

### 3. Add the WebRTC package

In Xcode:

1. **File → Add Package Dependencies…**
2. Paste URL: `https://github.com/stasel/WebRTC.git`
3. Dependency rule: **Up to Next Major** from `137.0.0` (anything 137.x works).
4. Add Package → ensure the `WebRTC` library is added to the **Jarvis** target.

This is the standard Apple-prebuilt WebRTC framework wrapped for Swift
Package Manager; it's the same binary Google ships.

### 4. Add capabilities + privacy keys

Open the project → select the **Jarvis** target → **Signing & Capabilities**
tab.

**Signing & Capabilities → Signing**
- Team: **Personal Team — <your name> (Personal Team)**.
  If "Personal Team" isn't there, click **Add an Account…**, sign in with
  your Apple ID, then come back.
- Bundle Identifier: change to something unique like
  `com.<yourhandle>.jarvis`. Xcode will auto-create a personal provisioning
  profile.

**Add capabilities**
Click **+ Capability** twice:
- **Background Modes** → check:
  - ✓ Audio, AirPlay, and Picture in Picture
  - ✓ Voice over IP

**Info.plist privacy strings**
Open `Info.plist` (or in Xcode 15+: project → target → Info tab):
- `NSMicrophoneUsageDescription` →
  *"Jarvis needs the microphone so you can talk to it."*
- `NSLocalNetworkUsageDescription` →
  *"Jarvis connects to the Mac running on your local network or Tailscale."*
- `NSAppTransportSecurity` → dict → `NSAllowsArbitraryLoads = YES`
  (lets the app reach plain HTTP `http://100.x.x.x:7860`. Switch off
  once you've enabled TLS via Tailscale certs.)

Details also in `Jarvis/Info.plist.additions.txt` if you want to copy values.

### 5. Build & run on your phone

1. Plug iPhone into Mac with a cable, unlock it, trust the Mac if prompted.
2. In Xcode toolbar, pick your iPhone as the run destination.
3. Press ⌘R.
4. First time: iOS will refuse to launch the app — go to
   **Settings → General → VPN & Device Management** on the iPhone,
   tap your Apple ID, **Trust**.
5. Re-run from Xcode (⌘R). App should now launch.

### 6. Free-tier 7-day re-sign

Personal Team provisioning profiles expire 7 days after install. The app
stops launching after that. To refresh: plug the phone in, ⌘R from Xcode,
30 seconds. No re-trust step needed once the cert is trusted.

## How to use

### Local Wi-Fi (same network as Mac)

1. On the Mac, run the server:
   ```bash
   uv run python main.py
   ```
2. Find the Mac's LAN IP: `ipconfig getifaddr en0` (or System Settings → Wi-Fi → Details).
3. In the Jarvis app, set the server URL to `http://<mac-lan-ip>:7860`.
4. Tap **Call Jarvis**. iOS will show a native call UI.
5. Plug in AirPods, talk.

### Remote (cellular, or different Wi-Fi)

You need Tailscale (or any other reachable IP).

1. Install Tailscale on both Mac and iPhone (free for personal use).
2. Sign in to the same tailnet on both.
3. On the Mac: `tailscale ip -4` → gives a 100.x.x.x IP.
4. In the Jarvis app, set the server URL to `http://100.x.x.x:7860`.
5. The phone reaches the Mac over Tailscale even on cellular.

## What you'll see when it works

- Native iOS call screen appears (lock-screen call controls, the green
  status bar at the top).
- Any audio playing on the phone (Apple Music, podcasts, YouTube) ducks to
  ~50% volume — this is iOS auto-ducking, not us.
- You can lock the phone; the call stays alive because of the
  "Voice over IP" background mode + AirPods.
- Status text in the app says `Connected. Talk to Jarvis.`
- The Mac terminal logs `>>> speaking`, transcription, Claude replies.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App crashes at launch with "key NSMicrophoneUsageDescription must be present" | Forgot the privacy string. Re-add. |
| "Signaling HTTP 0" | Mac unreachable. Wrong URL, wrong network, firewall, or Tailscale not running. Try opening `http://<url>/health` in Safari first — should see `{"ok":true}`. |
| Call connects but no audio | Check `Background Modes → Audio` is on. Also check AirPods are paired and selected. |
| Call drops after a few seconds | Mac process died or pipeline crashed. Check terminal output. |
| No music ducking | Confirm CallKit UI actually appeared. If it didn't, the CXStartCallAction probably failed silently — check Console.app for `Jarvis` crash logs. |
