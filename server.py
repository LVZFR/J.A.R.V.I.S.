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

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import AuthPolicy, AuthStore, FaceIdentity, PasskeyManager, VoiceprintManager

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
AUTH_DIR = Path(os.getenv("JARVIS_AUTH_DIR", str(ROOT / ".jarvis-auth"))).expanduser()
AUTH_RP_ID = os.getenv("JARVIS_AUTH_RP_ID", "localhost")
AUTH_ORIGIN = os.getenv("JARVIS_AUTH_ORIGIN", "http://localhost:8787")
AUTH_COOKIE = "jarvis_auth"
VOICEPRINT_THRESHOLD = float(os.getenv("JARVIS_VOICEPRINT_THRESHOLD", "0.82"))
FACE_THRESHOLD = float(os.getenv("JARVIS_FACE_THRESHOLD", "65"))
FACE_AUTH_ENABLED = os.getenv("JARVIS_FACE_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

_wakeword_engine: Any = None
_wakeword_lock = asyncio.Lock()
auth_store = AuthStore(AUTH_DIR, use_keychain=True)
auth_policy = AuthPolicy(require_face=FACE_AUTH_ENABLED)
passkeys = PasskeyManager(auth_store, rp_id=AUTH_RP_ID, origin=AUTH_ORIGIN)
face_identity = FaceIdentity(auth_store, threshold=FACE_THRESHOLD) if FACE_AUTH_ENABLED else None
voiceprints = VoiceprintManager(auth_store, threshold=VOICEPRINT_THRESHOLD)

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


class PasskeyVerifyRequest(BaseModel):
    transaction: str
    credential: dict[str, Any]


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


def validate_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != AUTH_ORIGIN:
        raise HTTPException(403, "Cross-origin authentication request rejected")


def request_session(request: Request) -> Any:
    return auth_policy.get_session(request.cookies.get(AUTH_COOKIE))


def require_passkey(request: Request) -> Any:
    session = request_session(request)
    status = auth_policy.authorization(session)
    if status["reason"] == "passkey_required":
        raise HTTPException(401, status)
    return session


def require_passkey_face(request: Request) -> Any:
    session = require_passkey(request)
    status = auth_policy.authorization(session)
    if status["reason"] == "face_required":
        raise HTTPException(401, status)
    return session


def require_authorized(request: Request) -> Any:
    session = request_session(request)
    status = auth_policy.authorization(session)
    if not status["authorized"]:
        raise HTTPException(401, status)
    return session


def attach_auth_cookie(response: Response, session: Any) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        session.token,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )


@app.get("/")
async def index(request: Request) -> Response:
    if request.url.hostname == "127.0.0.1":
        port = request.url.port or 8787
        return RedirectResponse(f"http://localhost:{port}/", status_code=307)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    session = request_session(request)
    authorization = auth_policy.authorization(session)
    passkey_current = bool(session and session.passkey_at is not None and authorization["reason"] != "passkey_required")
    face_current = bool(FACE_AUTH_ENABLED and passkey_current and authorization["reason"] in {"voice_required", "authorized"})
    return {
        "policy": {"face_required": FACE_AUTH_ENABLED, "voice_required": True, "passkey_required": True},
        "enrollment": {
            "passkey": passkeys.enrolled,
            "face": bool(face_identity and face_identity.enrolled),
            "face_disabled": not FACE_AUTH_ENABLED,
            "voice": voiceprints.enrolled,
            "complete": passkeys.enrolled and voiceprints.enrolled and (not FACE_AUTH_ENABLED or bool(face_identity and face_identity.enrolled)),
        },
        "session": {
            "passkey": passkey_current,
            "face": face_current,
            "voice": bool(session and authorization["authorized"]),
            **authorization,
        },
    }


@app.post("/api/auth/passkey/register/options")
async def passkey_register_options(request: Request) -> dict[str, Any]:
    validate_same_origin(request)
    if passkeys.enrolled:
        require_passkey(request)
    transaction, options = passkeys.registration_options()
    return {"transaction": transaction, "options": options}


@app.post("/api/auth/passkey/register/verify")
async def passkey_register_verify(payload: PasskeyVerifyRequest, request: Request) -> JSONResponse:
    validate_same_origin(request)
    try:
        passkeys.verify_registration_response(payload.transaction, payload.credential)
    except Exception as exc:
        raise HTTPException(401, f"Passkey registration failed: {exc}") from exc
    session = auth_policy.create_session()
    auth_policy.mark_passkey(session)
    response = JSONResponse({"verified": True, "next": "face_enrollment" if FACE_AUTH_ENABLED else "voice_enrollment"})
    attach_auth_cookie(response, session)
    return response


@app.post("/api/auth/passkey/login/options")
async def passkey_login_options(request: Request) -> dict[str, Any]:
    validate_same_origin(request)
    try:
        transaction, options = passkeys.authentication_options()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"transaction": transaction, "options": options}


@app.post("/api/auth/passkey/login/verify")
async def passkey_login_verify(payload: PasskeyVerifyRequest, request: Request) -> JSONResponse:
    validate_same_origin(request)
    try:
        passkeys.verify_authentication_response(payload.transaction, payload.credential)
    except Exception as exc:
        raise HTTPException(401, f"Passkey authentication failed: {exc}") from exc
    session = auth_policy.create_session()
    auth_policy.mark_passkey(session)
    response = JSONResponse({"verified": True})
    attach_auth_cookie(response, session)
    return response


@app.post("/api/auth/face/enroll")
async def face_enroll(request: Request, images: list[UploadFile] = File(...)) -> Any:
    validate_same_origin(request)
    if not FACE_AUTH_ENABLED:
        raise HTTPException(410, "Face authentication is disabled")
    require_passkey(request)
    try:
        return await asyncio.to_thread(face_identity.enroll, [await image.read() for image in images])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/auth/face/verify")
async def face_verify(request: Request, image: UploadFile = File(...)) -> Any:
    validate_same_origin(request)
    if not FACE_AUTH_ENABLED:
        raise HTTPException(410, "Face authentication is disabled")
    session = require_passkey(request)
    try:
        result = await asyncio.to_thread(face_identity.verify, await image.read())
    except ValueError as exc:
        auth_policy.mark_face(session, False)
        raise HTTPException(400, str(exc)) from exc
    auth_policy.mark_face(session, bool(result["matched"]))
    if not result["matched"]:
        raise HTTPException(401, {"reason": "face_mismatch", **result})
    return result


@app.post("/api/auth/voice/enroll")
async def voice_enroll(request: Request, samples: list[UploadFile] = File(...)) -> Any:
    validate_same_origin(request)
    (require_passkey_face if FACE_AUTH_ENABLED else require_passkey)(request)
    try:
        return await asyncio.to_thread(voiceprints.enroll, [await sample.read() for sample in samples])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/auth/voice/verify")
async def voice_verify(request: Request, audio: UploadFile = File(...)) -> Any:
    validate_same_origin(request)
    session = (require_passkey_face if FACE_AUTH_ENABLED else require_passkey)(request)
    try:
        result = await asyncio.to_thread(voiceprints.verify, await audio.read())
    except ValueError as exc:
        auth_policy.mark_voice(session, False)
        raise HTTPException(400, str(exc)) from exc
    auth_policy.mark_voice(session, bool(result["matched"]))
    if not result["matched"]:
        raise HTTPException(401, {"reason": "voice_mismatch", **result})
    return result


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    validate_same_origin(request)
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        auth_policy.sessions.pop(token, None)
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
    if payload.input_mode == "typed":
        (require_passkey_face if FACE_AUTH_ENABLED else require_passkey)(request)
    else:
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
    origin = websocket.headers.get("origin")
    session = auth_policy.get_session(websocket.cookies.get(AUTH_COOKIE))
    authorization = auth_policy.authorization(session)
    if origin and origin != AUTH_ORIGIN:
        await websocket.send_json({"event": "wakeword.error", "error": "Cross-origin wake stream rejected"})
        await websocket.close(code=1008)
        return
    if authorization["reason"] not in {"voice_required", "authorized"}:
        requirement = "Touch ID and current face verification are required" if FACE_AUTH_ENABLED else "Touch ID verification is required"
        await websocket.send_json({"event": "wakeword.error", "error": requirement})
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
