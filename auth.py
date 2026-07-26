"""Local authentication state and encrypted-boundary storage for JARVIS.

Passkey public keys and biometric feature templates are stored locally. Raw camera
frames and voice recordings are processed transiently and are not retained.
"""
from __future__ import annotations

import json
import base64
import io
import hashlib
import math
import os
import secrets
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class AuthSession:
    token: str
    passkey_at: float | None = None
    face_at: float | None = None
    voice_at: float | None = None


class AuthPolicy:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        face_ttl: float = 15.0,
        voice_ttl: float = 30.0,
        session_ttl: float = 8 * 60 * 60,
        require_face: bool = False,
    ) -> None:
        self.clock = clock
        self.face_ttl = face_ttl
        self.voice_ttl = voice_ttl
        self.session_ttl = session_ttl
        self.require_face = require_face
        self.sessions: dict[str, AuthSession] = {}

    def create_session(self, token: str | None = None) -> AuthSession:
        session = AuthSession(token=token or secrets.token_urlsafe(32))
        self.sessions[session.token] = session
        return session

    def get_session(self, token: str | None) -> AuthSession | None:
        if not token:
            return None
        return self.sessions.get(token)

    def mark_passkey(self, session: AuthSession) -> None:
        session.passkey_at = self.clock()
        session.face_at = None
        session.voice_at = None

    def mark_face(self, session: AuthSession, matched: bool) -> None:
        session.face_at = self.clock() if matched else None
        if self.require_face and not matched:
            session.voice_at = None

    def mark_voice(self, session: AuthSession, matched: bool) -> None:
        session.voice_at = self.clock() if matched else None

    def authorization(self, session: AuthSession | None) -> dict[str, Any]:
        now = self.clock()
        if session is None or session.passkey_at is None or now - session.passkey_at > self.session_ttl:
            return {"authorized": False, "reason": "passkey_required"}
        if self.require_face and (session.face_at is None or now - session.face_at > self.face_ttl):
            return {"authorized": False, "reason": "face_required"}
        if session.voice_at is None or now - session.voice_at > self.voice_ttl:
            return {"authorized": False, "reason": "voice_required"}
        return {"authorized": True, "reason": "authorized"}


class AuthStore:
    def __init__(self, root: Path, use_keychain: bool = False) -> None:
        self.root = root
        self.use_keychain = use_keychain
        self._cipher: Any = None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def path(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("AuthStore names may not contain path separators")
        return self.root / name

    def save_json(self, name: str, value: Any) -> None:
        path = self.path(name)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def load_json(self, name: str, default: Any = None) -> Any:
        path = self.path(name)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _key(self) -> bytes:
        from cryptography.fernet import Fernet

        key_path = self.path(".template-key")
        if key_path.exists():
            return key_path.read_bytes().strip()
        service = "com.nous.jarvis.biometric." + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:12]
        if self.use_keychain and os.uname().sysname == "Darwin":
            found = subprocess.run(
                ["security", "find-generic-password", "-a", os.getenv("USER", "jarvis"), "-s", service, "-w"],
                capture_output=True,
                check=False,
            )
            if found.returncode == 0 and found.stdout.strip():
                return found.stdout.strip()
            key = Fernet.generate_key()
            added = subprocess.run(
                ["security", "add-generic-password", "-a", os.getenv("USER", "jarvis"), "-s", service, "-w", key.decode(), "-U"],
                capture_output=True,
                check=False,
            )
            if added.returncode == 0:
                return key
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        os.chmod(key_path, 0o600)
        return key

    def _fernet(self) -> Any:
        if self._cipher is None:
            from cryptography.fernet import Fernet

            self._cipher = Fernet(self._key())
        return self._cipher

    def save_encrypted(self, name: str, data: bytes) -> None:
        path = self.path(name)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(self._fernet().encrypt(data))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def load_encrypted(self, name: str) -> bytes:
        path = self.path(name)
        if not path.exists():
            raise FileNotFoundError(path)
        return self._fernet().decrypt(path.read_bytes())


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PasskeyManager:
    def __init__(
        self,
        store: AuthStore,
        rp_id: str,
        origin: str,
        owner: str = "Don",
        verify_registration: Callable[..., Any] | None = None,
        verify_authentication: Callable[..., Any] | None = None,
    ) -> None:
        from webauthn import verify_authentication_response, verify_registration_response

        self.store = store
        self.rp_id = rp_id
        self.origin = origin
        self.owner = owner
        self.pending: dict[str, tuple[str, bytes, float]] = {}
        self._verify_registration = verify_registration or verify_registration_response
        self._verify_authentication = verify_authentication or verify_authentication_response

    @property
    def credentials(self) -> list[dict[str, Any]]:
        return self.store.load_json("passkeys.json", [])

    @property
    def enrolled(self) -> bool:
        return bool(self.credentials)

    def _transaction(self, mode: str, challenge: bytes) -> str:
        transaction = secrets.token_urlsafe(24)
        self.pending[transaction] = (mode, challenge, time.monotonic() + 120)
        return transaction

    def _take_transaction(self, transaction: str, mode: str) -> bytes:
        pending = self.pending.pop(transaction, None)
        if not pending or pending[0] != mode or pending[2] < time.monotonic():
            raise ValueError("Passkey transaction is missing or expired")
        return pending[1]

    def registration_options(self) -> tuple[str, dict[str, Any]]:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorAttachment,
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        challenge = secrets.token_bytes(32)
        exclude = [PublicKeyCredentialDescriptor(id=_unb64url(item["credential_id"])) for item in self.credentials]
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="JARVIS Local Command Interface",
            user_id=b"jarvis-owner-don",
            user_name=self.owner,
            user_display_name=self.owner,
            challenge=challenge,
            exclude_credentials=exclude,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return self._transaction("registration", challenge), json.loads(options_to_json(options))

    def verify_registration_response(self, transaction: str, credential: dict[str, Any]) -> dict[str, Any]:
        challenge = self._take_transaction(transaction, "registration")
        verified = self._verify_registration(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.origin,
            require_user_verification=True,
        )
        credentials = self.credentials
        record = {
            "credential_id": _b64url(verified.credential_id),
            "public_key": _b64url(verified.credential_public_key),
            "sign_count": int(verified.sign_count),
            "created_at": int(time.time()),
        }
        credentials.append(record)
        self.store.save_json("passkeys.json", credentials)
        return record

    def authentication_options(self) -> tuple[str, dict[str, Any]]:
        if not self.credentials:
            raise ValueError("No passkey has been enrolled")
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

        challenge = secrets.token_bytes(32)
        allowed = [PublicKeyCredentialDescriptor(id=_unb64url(item["credential_id"])) for item in self.credentials]
        options = generate_authentication_options(
            rp_id=self.rp_id,
            challenge=challenge,
            allow_credentials=allowed,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._transaction("authentication", challenge), json.loads(options_to_json(options))

    def verify_authentication_response(self, transaction: str, credential: dict[str, Any]) -> dict[str, Any]:
        challenge = self._take_transaction(transaction, "authentication")
        credential_id = credential.get("id") or credential.get("rawId")
        records = self.credentials
        record = next((item for item in records if item["credential_id"] == credential_id), None)
        if record is None:
            raise ValueError("Unknown passkey credential")
        verified = self._verify_authentication(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.origin,
            credential_public_key=_unb64url(record["public_key"]),
            credential_current_sign_count=int(record["sign_count"]),
            require_user_verification=True,
        )
        record["sign_count"] = int(verified.new_sign_count)
        self.store.save_json("passkeys.json", records)
        return record


def voice_embedding(wav_bytes: bytes) -> list[float]:
    import numpy as np

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                raise ValueError("Voiceprint audio must be 16 kHz mono 16-bit WAV")
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid WAV voice sample") from exc
    if len(samples) < 8000:
        raise ValueError("Voice sample must be at least 0.5 seconds")
    samples = samples - float(samples.mean())
    peak = float(np.max(np.abs(samples)))
    if peak < 0.015:
        raise ValueError("Voice sample is too quiet")
    samples /= peak
    frame_size, hop = 400, 160
    frame_count = 1 + (len(samples) - frame_size) // hop
    frames = np.stack([samples[index * hop:index * hop + frame_size] for index in range(frame_count)])
    energies = np.sqrt(np.mean(frames * frames, axis=1))
    active = frames[energies >= max(0.025, float(np.percentile(energies, 35)))]
    if len(active) < 4:
        raise ValueError("Not enough voiced audio for a voiceprint")
    spectrum = np.mean(np.abs(np.fft.rfft(active * np.hanning(frame_size), axis=1)) ** 2, axis=0)
    frequencies = np.fft.rfftfreq(frame_size, 1 / 16000)
    mel = lambda hz: 2595 * np.log10(1 + hz / 700)
    hz = lambda value: 700 * (10 ** (value / 2595) - 1)
    points = hz(np.linspace(mel(80), mel(7600), 42))
    bands = []
    for left, center, right in zip(points[:-2], points[1:-1], points[2:]):
        weights = np.maximum(0, np.minimum((frequencies - left) / max(center - left, 1), (right - frequencies) / max(right - center, 1)))
        bands.append(math.log(float(np.sum(spectrum * weights)) + 1e-8))
    vector = np.asarray(bands, dtype=np.float64)
    vector = (vector - vector.mean()) / max(float(vector.std()), 1e-8)
    return [round(float(value), 8) for value in vector]


class VoiceprintManager:
    def __init__(self, store: AuthStore, threshold: float = 0.82) -> None:
        self.store = store
        self.threshold = threshold

    @property
    def enrolled(self) -> bool:
        return self.store.path("voiceprint.enc").exists()

    def enroll(self, samples: list[bytes]) -> dict[str, Any]:
        if len(samples) < 3:
            raise ValueError("Three voice samples are required")
        embeddings = [voice_embedding(sample) for sample in samples]
        centroid = [sum(values) / len(values) for values in zip(*embeddings)]
        payload = json.dumps({"centroid": centroid, "samples": len(samples), "created_at": int(time.time())}, separators=(",", ":")).encode()
        self.store.save_encrypted("voiceprint.enc", payload)
        return {"enrolled": True, "samples": len(samples)}

    def verify(self, sample: bytes) -> dict[str, Any]:
        if not self.enrolled:
            raise ValueError("Voiceprint is not enrolled")
        stored = json.loads(self.store.load_encrypted("voiceprint.enc"))
        score = cosine_similarity(stored["centroid"], voice_embedding(sample))
        return {"matched": score >= self.threshold, "score": round(score, 4), "threshold": self.threshold}


def extract_face(image_bytes: bytes) -> Any:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Invalid face image")
    cascade_path = Path(__file__).resolve().parent / "assets" / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        raise RuntimeError(f"Face detector data is unavailable: {cascade_path}")
    faces = cascade.detectMultiScale(image, scaleFactor=1.12, minNeighbors=6, minSize=(90, 90))
    if len(faces) != 1:
        raise ValueError("Exactly one clearly visible face is required")
    x, y, width, height = faces[0]
    crop = image[y:y + height, x:x + width]
    crop = cv2.resize(crop, (160, 160), interpolation=cv2.INTER_AREA)
    return cv2.equalizeHist(crop)


class FaceIdentity:
    def __init__(
        self,
        store: AuthStore,
        extractor: Callable[[bytes], Any] = extract_face,
        recognizer_factory: Callable[[], Any] | None = None,
        threshold: float = 65.0,
    ) -> None:
        if recognizer_factory is None:
            import cv2

            recognizer_factory = cv2.face.LBPHFaceRecognizer_create
        self.store = store
        self.extractor = extractor
        self.recognizer_factory = recognizer_factory
        self.threshold = threshold

    @property
    def model_path(self) -> Path:
        return self.store.path("face-model.enc")

    @property
    def enrolled(self) -> bool:
        return self.model_path.exists()

    def enroll(self, images: list[bytes]) -> dict[str, Any]:
        import numpy as np

        if len(images) < 2:
            raise ValueError("At least two face images are required")
        faces = [self.extractor(image) for image in images]
        recognizer = self.recognizer_factory()
        recognizer.train(faces, np.zeros(len(faces), dtype=np.int32))
        temporary = tempfile.NamedTemporaryFile(suffix=".yml", dir=self.store.root, delete=False)
        temporary.close()
        try:
            recognizer.write(temporary.name)
            self.store.save_encrypted("face-model.enc", Path(temporary.name).read_bytes())
        finally:
            Path(temporary.name).unlink(missing_ok=True)
        return {"enrolled": True, "samples": len(faces)}

    def verify(self, image: bytes) -> dict[str, Any]:
        if not self.enrolled:
            raise ValueError("Face identity is not enrolled")
        recognizer = self.recognizer_factory()
        temporary = tempfile.NamedTemporaryFile(suffix=".yml", dir=self.store.root, delete=False)
        try:
            temporary.write(self.store.load_encrypted("face-model.enc"))
            temporary.close()
            recognizer.read(temporary.name)
            _, distance = recognizer.predict(self.extractor(image))
        finally:
            temporary.close()
            Path(temporary.name).unlink(missing_ok=True)
        score = max(0.0, min(1.0, 1.0 - float(distance) / 100.0))
        return {"matched": float(distance) <= self.threshold, "score": round(score, 4), "distance": round(float(distance), 2), "threshold": self.threshold}
