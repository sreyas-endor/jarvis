"""Jarvis voice agent — WebRTC server. iPhone client connects, Mac runs pipeline.

Pipeline per call: SmallWebRTCTransport.input → VAD → STT → ClaudeCodeLLMService →
TTS → EventLogger → SmallWebRTCTransport.output. STT and TTS providers selected
via STT_PROVIDER and TTS_PROVIDER env vars; see README.

Mac stays running headless (lid closed, on AC). iOS app POSTs SDP offer to
/api/offer; we spin up a fresh pipeline bound to that connection, tear down
when the client disconnects.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Hand the claude subprocess (and its PreToolUse voice-permission hook) a
# stable handle to the repo root via env. settings.local.json references
# $JARVIS_HOME for the hook command so the config travels across machines.
os.environ["JARVIS_HOME"] = str(Path(__file__).parent.resolve())

# Outbound IPv6 to api.cartesia.ai (CloudFront) silently times out on this
# network — curl works because it does Happy Eyeballs, but the `websockets`
# library picks the first getaddrinfo result and stalls 20s on the v6 socket.
# Filter v6 out of DNS resolution for this process so every outbound socket
# uses v4. Process-scoped only; nothing system-wide changes. Must run
# BEFORE any pipecat service that opens a websocket gets imported.
import socket as _socket

_real_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [r for r in _real_getaddrinfo(*args, **kwargs) if r[0] == _socket.AF_INET]


_socket.getaddrinfo = _ipv4_only_getaddrinfo

# Load .env before any pipecat service tries to read env vars.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from jarvis.llm.service import ClaudeCodeLLMService
from jarvis.pipeline import build_pipeline_task
from jarvis.sessions import (
    NarrationEvent,
    SessionRegistry,
    discover_sessions,
)
from jarvis.workers import WorkerManager

# Bind 0.0.0.0 so the iPhone (over Tailscale or LAN) can reach us — Tailscale
# will expose the port on the tailnet IP.
JARVIS_HOST = os.environ.get("JARVIS_HOST", "0.0.0.0")
JARVIS_PORT = int(os.environ.get("JARVIS_PORT", "7860"))


async def _run_pipeline_for_connection(connection: SmallWebRTCConnection) -> None:
    """One pipeline per call. Tears down when the client disconnects."""
    log = logging.getLogger("jarvis.pipeline")
    log.info("connection accepted pc_id=%s", connection.pc_id)

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )
    task = build_pipeline_task(transport)

    # The LLM service was just constructed inside build_pipeline_task and
    # registered itself as the active instance. Wire the worker input
    # sink so focused voice input routes to the WorkerManager.
    active = ClaudeCodeLLMService.active_instance()
    if active is not None:
        active.set_worker_input_sink(_worker_sink)

    # Reconcile worker registry on each call — picks up workers that
    # survived a server restart.
    await _worker_manager.reconcile()

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        log.info(
            "client connected; audio_input_track=%s",
            _client.audio_input_track() is not None,
        )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        log.info("client disconnected; cancelling pipeline")
        await task.cancel()

    try:
        # handle_sigint=False — uvicorn owns the signal handlers; the runner
        # installing its own would steal Ctrl-C from the server process.
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        # Drop any attached session tailers so we don't keep narrating
        # into a dead pipeline after hang-up.
        await _session_registry.detach_all()
        log.info("pipeline finished pc_id=%s", connection.pc_id)


app = FastAPI(title="Jarvis voice agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
_request_handler = SmallWebRTCRequestHandler()


# Session registry. Lives across calls (each call rebinds the on_event
# callback to the new active service via _narrate). detach_all() runs on
# call teardown so we don't keep tailing into a dead pipeline.
async def _narrate(event: NarrationEvent) -> None:
    svc = ClaudeCodeLLMService.active_instance()
    if svc is None:
        return
    # Use the session id's leading 4 hex chars so we have *some* handle to
    # disambiguate in voice without reading out a full UUID.
    label = event.session_id[:4]
    await svc.speak_narration(f"session {label}: {event.summary}")


_session_registry = SessionRegistry(on_event=_narrate)
_worker_manager = WorkerManager()


async def _worker_sink(name: str, text: str) -> bool:
    """Called by the LLM service to deliver focused voice input to a worker."""
    return await _worker_manager.send_input(name, text)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    async def _on_new_connection(connection: SmallWebRTCConnection) -> None:
        background_tasks.add_task(_run_pipeline_for_connection, connection)

    return await _request_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=_on_new_connection,
    )


@app.patch("/api/offer")
async def offer_patch(request: SmallWebRTCPatchRequest):
    await _request_handler.handle_patch_request(request)
    return {"status": "success"}


@app.get("/internal/sessions")
async def list_sessions(exclude: str | None = None) -> dict:
    """List recent sessions on disk. Used by tools/jarvis_cli.py list.

    ``exclude`` lets the master session skip itself in the listing — it
    knows its own session id and passes it here.
    """
    sessions = discover_sessions(exclude_session_id=exclude)
    return {
        "sessions": [
            {
                "id": s.session_id,
                "project": s.project_dir,
                "last_modified": s.last_modified,
                "summary": s.first_user_message,
            }
            for s in sessions
        ],
        "attached": _session_registry.attached_ids,
    }


@app.post("/internal/sessions/attach")
async def attach_session(body: dict) -> dict:
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    info = await _session_registry.attach(session_id)
    if info is None:
        return {"ok": False, "error": f"session {session_id} not found"}
    return {
        "ok": True,
        "session_id": info.session_id,
        "project": info.project_dir,
        "summary": info.first_user_message,
    }


@app.post("/internal/sessions/detach")
async def detach_session(body: dict) -> dict:
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    ok = await _session_registry.detach(session_id)
    return {"ok": ok}


@app.post("/internal/worker/spawn")
async def worker_spawn(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing name"}
    try:
        worker = await _worker_manager.spawn(
            name=name,
            cwd=body.get("cwd") or None,
            initial_prompt=body.get("prompt") or None,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "name": worker.name,
        "tmux_session": worker.tmux_session,
        "cwd": worker.cwd,
        "session_id": worker.session_id,
        "attach_cmd": f"tmux attach -t {worker.tmux_session}",
    }


@app.get("/internal/worker/list")
async def worker_list() -> dict:
    await _worker_manager.reconcile()
    return {
        "workers": [
            {
                "name": w.name,
                "tmux_session": w.tmux_session,
                "cwd": w.cwd,
                "session_id": w.session_id,
                "alive": w.is_alive(),
                "attach_cmd": f"tmux attach -t {w.tmux_session}",
            }
            for w in _worker_manager.list()
        ],
        "focused": (
            ClaudeCodeLLMService.active_instance().focused_worker
            if ClaudeCodeLLMService.active_instance() is not None
            else None
        ),
    }


@app.post("/internal/worker/send")
async def worker_send(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    text = body.get("text") or ""
    if not name or not text:
        return {"ok": False, "error": "missing name or text"}
    ok = await _worker_manager.send_input(name, text)
    return {"ok": ok}


@app.post("/internal/worker/kill")
async def worker_kill(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing name"}
    # If killing the focused worker, drop focus too — otherwise voice
    # routes into a dead pane.
    svc = ClaudeCodeLLMService.active_instance()
    if svc is not None and svc.focused_worker == name:
        svc.unfocus_worker()
    ok = await _worker_manager.kill(name)
    return {"ok": ok}


@app.post("/internal/worker/focus")
async def worker_focus(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    svc = ClaudeCodeLLMService.active_instance()
    if svc is None:
        return {"ok": False, "error": "no active voice session"}
    if not name:
        svc.unfocus_worker()
        return {"ok": True, "focused": None}
    if _worker_manager.get(name) is None:
        return {"ok": False, "error": f"worker {name!r} not found"}
    svc.focus_worker(name)
    return {"ok": True, "focused": name}


@app.post("/internal/permission")
async def internal_permission(body: dict) -> dict:
    """PreToolUse hook entrypoint. Called by tools/voice_permission_hook.py.

    Forwards the tool name/args to whichever ClaudeCodeLLMService is
    currently driving the active voice call; that service speaks the
    prompt, waits for a yes/no in the next transcription, and returns
    the verdict.

    Fails closed if no call is in progress — better than auto-allowing
    a destructive tool when no human is on the line to object.
    """
    svc = ClaudeCodeLLMService.active_instance()
    if svc is None:
        return {"allow": False, "reason": "no active voice session"}
    return await svc.request_permission_voice(
        tool=body.get("tool", "") or "",
        args=body.get("args") or {},
    )


# Browser test page for smoke-testing the WebRTC plumbing without Xcode.
# Mounted last so /api/* and /health routes win when there's a name collision.
_web_dir = Path(__file__).parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    uvicorn.run(app, host=JARVIS_HOST, port=JARVIS_PORT, log_level="info")


if __name__ == "__main__":
    main()
