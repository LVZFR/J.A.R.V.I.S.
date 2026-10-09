#!/usr/bin/env python3
"""Local JARVIS dashboard bridge for the Hermes Agent Runs API."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import hmac
import secrets

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(ROOT / ".env")

HERMES_API_BASE = os.getenv("HERMES_API_BASE", "http://127.0.0.1:8642").rstrip("/")
API_SERVER_KEY = os.getenv("API_SERVER_KEY", "")
HERMES_PROFILE = os.getenv("HERMES_PROFILE", "default")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_STT_MODEL_ID = os.getenv("ELEVENLABS_STT_MODEL_ID", "scribe_v1")
WHISPER_MODEL = os.getenv("JARVIS_WHISPER_MODEL", "base")
WAKEWORD_THRESHOLD = float(os.getenv("JARVIS_WAKEWORD_THRESHOLD", "0.55"))
AUTH_COOKIE = "jarvis_session"
SHARED_SECRET = os.getenv("JARVIS_SHARED_SECRET", "")

_wakeword_engine: Any = None
_wakeword_lock = asyncio.Lock()
_sessions: set[str] = set()

app = FastAPI(title="JARVIS Hermes Bridge", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class RunRequest(BaseModel):
    input: str = Field(min_length=1)
    input_mode: Literal["typed", "voice"] = "voice"
    session_id: str | None = None
    instructions: str | None = None
    previous_response_id: str | None = None


class ApprovalRequest(BaseModel):
    run_id: str
    choice: str
    resolve_all: bool = False


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2500)


def auth_headers() -> dict[str, str]:
    if not API_SERVER_KEY:
        raise HTTPException(503, "API_SERVER_KEY is not configured in JARVIS/.env")
    return {"Authorization": f"Bearer {API_SERVER_KEY}"}


def public_status_config() -> dict[str, Any]:
    whisper = shutil.which("whisper")
    wakeword_installed = importlib.util.find_spec("openwakeword") is not None
    return {
        "profile": HERMES_PROFILE,
        "hermes_api_base": HERMES_API_BASE,
        "stt": {
            "available": bool(ELEVENLABS_API_KEY or whisper),
            "primary": "ElevenLabs Scribe" if ELEVENLABS_API_KEY else None,
            "fallback": f"Whisper CLI ({WHISPER_MODEL})" if whisper else None,
        },
        "tts": {
            "available": True,
            "provider": "ElevenLabs" if ELEVENLABS_API_KEY else "Browser speechSynthesis fallback",
        },
        "wakeword": {
            "available": wakeword_installed,
            "phrase": "Hey Jarvis",
            "provider": "openWakeWord (local)" if wakeword_installed else None,
            "threshold": WAKEWORD_THRESHOLD,
        },
    }


async def get_wakeword_engine() -> Any:
    global _wakeword_engine
    if _wakeword_engine is not None:
        return _wakeword_engine
    async with _wakeword_lock:
        if _wakeword_engine is None:
            from wakeword import load_engine

            _wakeword_engine = await asyncio.to_thread(load_engine, WAKEWORD_THRESHOLD)
    return _wakeword_engine


async def hermes_json(method: str, path: str, **kwargs: Any) -> Any:
    headers = {**auth_headers(), **kwargs.pop("headers", {})}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=120)) as client:
            response = await client.request(method, f"{HERMES_API_BASE}{path}", headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Hermes gateway unavailable: {exc}") from exc
    if response.is_error:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:500]
        raise HTTPException(response.status_code, detail)
    return response.json()


def request_session(request: Request) -> str | None:
    token = request.cookies.get(AUTH_COOKIE)
    return token if token and token in _sessions else None


def require_authorized(request: Request) -> str:
    session = request_session(request)
    if not session:
        raise HTTPException(401, {"authorized": False, "reason": "login_required"})
    return session


def attach_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )


@app.get("/")
async def index() -> Response:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    authorized = request_session(request) is not None
    return {"configured": bool(SHARED_SECRET), "session": {"authorized": authorized}}


class LoginRequest(BaseModel):
    secret: str = Field(min_length=1)


@app.post("/api/auth/login")
async def auth_login(payload: LoginRequest) -> JSONResponse:
    if not SHARED_SECRET:
        raise HTTPException(503, "JARVIS_SHARED_SECRET is not configured in .env")
    if not hmac.compare_digest(payload.secret, SHARED_SECRET):
        raise HTTPException(401, "Incorrect access key")
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    response = JSONResponse({"authorized": True})
    attach_auth_cookie(response, token)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        _sessions.discard(token)
    response = JSONResponse({"logged_out": True})
    response.delete_cookie(AUTH_COOKIE, path="/")
    return response


@app.get("/api/status")
async def status() -> dict[str, Any]:
    result = public_status_config()
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            health = await client.get(f"{HERMES_API_BASE}/health")
            gateway_ok = health.status_code == 200
        result["gateway"] = {
            "online": gateway_ok,
            "status": health.json().get("status", "unknown") if gateway_ok else f"HTTP {health.status_code}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
        if gateway_ok and API_SERVER_KEY:
            try:
                result["capabilities"] = await hermes_json("GET", "/v1/capabilities")
            except HTTPException as exc:
                result["gateway"]["auth_error"] = str(exc.detail)
    except (httpx.RequestError, ValueError) as exc:
        result["gateway"] = {"online": False, "status": "offline", "error": str(exc)}
    return result


@app.post("/api/run")
async def create_run(payload: RunRequest, request: Request) -> Any:
    require_authorized(request)
    body = payload.model_dump(exclude_none=True, exclude={"input_mode"})
    headers = {}
    session_key = request.headers.get("X-Jarvis-Session-Key")
    if session_key:
        headers["X-Hermes-Session-Key"] = session_key[:256]
    return await hermes_json("POST", "/v1/runs", json=body, headers=headers)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> Any:
    return await hermes_json("GET", f"/v1/runs/{run_id}")


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    async def relay() -> AsyncIterator[bytes]:
        headers = auth_headers()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None)) as client:
                async with client.stream("GET", f"{HERMES_API_BASE}/v1/runs/{run_id}/events", headers=headers) as upstream:
                    if upstream.is_error:
                        body = await upstream.aread()
                        yield f"data: {json.dumps({'event': 'bridge.error', 'error': body.decode(errors='replace')[:500]})}\n\n".encode()
                        return
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
        except (httpx.RequestError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            yield f"data: {json.dumps({'event': 'bridge.error', 'error': str(exc)})}\n\n".encode()

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/approval")
async def approval(payload: ApprovalRequest) -> Any:
    return await hermes_json(
        "POST",
        f"/v1/runs/{payload.run_id}/approval",
        json={"choice": payload.choice, "resolve_all": payload.resolve_all},
    )


async def elevenlabs_stt(data: bytes, filename: str, content_type: str) -> str:
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    files = {"file": (filename, data, content_type or "application/octet-stream")}
    form = {"model_id": ELEVENLABS_STT_MODEL_ID}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post("https://api.elevenlabs.io/v1/speech-to-text", headers=headers, files=files, data=form)
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("text", "")).strip()


async def whisper_stt(data: bytes, suffix: str) -> str:
    whisper = shutil.which("whisper")
    if not whisper:
        raise RuntimeError("Whisper CLI is not installed")
    with tempfile.TemporaryDirectory(prefix="jarvis-stt-") as tmp:
        audio = Path(tmp) / f"recording{suffix}"
        audio.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            whisper, str(audio), "--model", WHISPER_MODEL, "--output_format", "txt", "--output_dir", tmp,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[-800:])
        output = Path(tmp) / "recording.txt"
        if not output.exists():
            raise RuntimeError("Whisper did not produce a transcript")
        return output.read_text(encoding="utf-8").strip()


@app.post("/api/stt")
async def stt(request: Request, audio: UploadFile = File(...)) -> JSONResponse:
    require_authorized(request)
    started = time.perf_counter()
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty audio upload")
    errors: list[str] = []
    if ELEVENLABS_API_KEY:
        try:
            text = await elevenlabs_stt(data, audio.filename or "recording.webm", audio.content_type or "audio/webm")
            if text:
                return JSONResponse({"text": text, "provider": "elevenlabs", "latency_ms": round((time.perf_counter() - started) * 1000)})
            errors.append("ElevenLabs returned an empty transcript")
        except Exception as exc:
            errors.append(f"ElevenLabs: {exc}")
    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"
    try:
        text = await whisper_stt(data, suffix)
        if not text:
            raise RuntimeError("Whisper returned an empty transcript")
        return JSONResponse({"text": text, "provider": "whisper", "latency_ms": round((time.perf_counter() - started) * 1000), "fallback_reason": errors[0] if errors else None})
    except Exception as exc:
        errors.append(f"Whisper: {exc}")
        raise HTTPException(503, {"message": "No STT provider succeeded", "errors": errors}) from exc


@app.post("/api/tts")
async def tts(payload: TTSRequest) -> Response:
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ElevenLabs TTS is not configured; use browser speechSynthesis")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Accept": "audio/mpeg"}
    body = {
        "text": payload.text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=body, params={"output_format": "mp3_44100_128"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ElevenLabs TTS failed: {exc}") from exc
    return Response(response.content, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/api/wakeword")
async def wakeword_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    session = websocket.cookies.get(AUTH_COOKIE)
    if not session or session not in _sessions:
        await websocket.send_json({"event": "wakeword.error", "error": "Login is required before wake mode can start"})
        await websocket.close(code=1008)
        return
    if importlib.util.find_spec("openwakeword") is None:
        await websocket.send_json({"event": "wakeword.error", "error": "openWakeWord is not installed"})
        await websocket.close(code=1011)
        return
    try:
        await websocket.send_json({"event": "wakeword.loading", "phrase": "Hey Jarvis"})
        engine = await get_wakeword_engine()
        await websocket.send_json({"event": "wakeword.ready", "phrase": "Hey Jarvis"})
        while True:
            pcm = await websocket.receive_bytes()
            result = await asyncio.to_thread(engine.process_pcm, pcm)
            if result["detected"]:
                await websocket.send_json({"event": "wakeword.detected", **result})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"event": "wakeword.error", "error": str(exc)[:300]})
            await websocket.close(code=1011)
        except RuntimeError:
            pass
