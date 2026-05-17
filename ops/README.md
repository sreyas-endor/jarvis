# Ops — running Jarvis always-on

Goal: Mac sits headless (lid closed, on AC) and keeps the WebRTC server alive
so the iPhone can call into it any time, even after a reboot.

This directory has the launchd plist template + setup notes.

## What you need on the Mac

- **AC power.** Lid-closed-on-battery puts the Mac into deep sleep regardless
  of any software trick.
- **Either an external monitor/keyboard/mouse OR an HDMI dummy plug.**
  macOS clamshell mode requires "an external display" — a $5 HDMI dummy plug
  fools the Mac into clamshell mode with the lid closed. Without one, the
  lid switch suspends the system even on AC.
- **`caffeinate` in the launch command** (already wired in the plist) prevents
  App Nap from throttling the python process when the Mac thinks no one is
  watching the screen.
- **Tailscale on Mac + phone**, signed into the same tailnet. Phone reaches
  the Mac via `100.x.x.x:7860` regardless of which network the phone is on
  (home Wi-Fi, cellular, coffee shop). Free for personal use.

## Install the launchd agent

1. Copy the plist template into your `LaunchAgents` directory, substituting
   `REPLACE_ME` with your macOS username:

   ```bash
   sed "s/REPLACE_ME/$USER/g" ops/com.user.jarvis.plist \
     > ~/Library/LaunchAgents/com.user.jarvis.plist
   ```

2. Make sure the log directory exists:

   ```bash
   mkdir -p ~/Library/Logs
   ```

3. Verify the paths in the plist actually resolve on your machine — particularly
   `uv`. The template assumes `~/.local/bin/uv`; if yours is elsewhere:

   ```bash
   which uv
   ```

   and edit the second `<string>` under `ProgramArguments` accordingly.

4. Load it:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.jarvis.plist
   launchctl enable gui/$(id -u)/com.user.jarvis
   launchctl kickstart -k gui/$(id -u)/com.user.jarvis
   ```

5. Confirm it's running:

   ```bash
   launchctl print gui/$(id -u)/com.user.jarvis | grep -E 'state|pid'
   curl -s http://localhost:7860/health
   tail -f ~/Library/Logs/jarvis.log
   ```

## Stopping / restarting

```bash
# Stop until next login
launchctl kill SIGTERM gui/$(id -u)/com.user.jarvis

# Restart
launchctl kickstart -k gui/$(id -u)/com.user.jarvis

# Uninstall entirely
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.user.jarvis.plist
rm ~/Library/LaunchAgents/com.user.jarvis.plist
```

## Reachability — Tailscale setup

1. Install Tailscale on the Mac and sign in:

   ```bash
   brew install tailscale
   tailscale up
   ```

2. Install the **Tailscale** app on the iPhone (App Store), sign into the
   same tailnet.

3. Get the Mac's tailnet IP:

   ```bash
   tailscale ip -4
   # e.g. 100.67.84.132
   ```

4. In the Jarvis iOS app, set the server URL to `http://100.67.84.132:7860`
   (substituting your real IP). The phone can now reach the Mac from
   anywhere.

### Optional: TLS via Tailscale's auto cert

Once the basic flow works, enable HTTPS:

```bash
tailscale cert <your-mac-hostname>.<tailnet>.ts.net
# saves <hostname>.<tailnet>.ts.net.crt + .key in the current dir
```

Then point uvicorn at the cert (edit `main()` in `main.py`):

```python
uvicorn.run(
    app, host=JARVIS_HOST, port=JARVIS_PORT, log_level="info",
    ssl_keyfile="/path/to/key", ssl_certfile="/path/to/crt",
)
```

Once TLS is up, you can remove `NSAllowsArbitraryLoads` from the iOS app's
Info.plist for a cleaner trust posture.

## Lid-closed sanity check

After everything is installed:

1. Close the lid (with AC plugged in + dummy plug, if you needed one).
2. Wait 60s.
3. Open the Jarvis app on your phone → Call Jarvis.
4. Should connect and respond.

If it doesn't: open the lid, look at `~/Library/Logs/jarvis.log`. Common
failures:

| Symptom in log | Fix |
|---|---|
| `Address already in use` | Older instance still bound to 7860. `pkill -f main.py` then `launchctl kickstart -k …` |
| `RuntimeError: AZURE_SPEECH_KEY missing` | `.env` not loaded. launchd doesn't source shell rc files — confirm the path passed to `--project` actually contains `.env` |
| Server log silent after a few minutes | Caffeinate not engaged. Verify the `ProgramArguments` array starts with `/usr/bin/caffeinate -i` |
| Network unreachable on phone but Mac log healthy | Tailscale daemon stopped on Mac during sleep. `sudo tailscale up` to restart, then `launchctl kickstart` Jarvis |
