import importlib.util
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch
from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HERMES_API_BASE", "http://127.0.0.1:8642")
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("JARVIS_AUTH_DIR", tempfile.mkdtemp(prefix="jarvis-auth-test-"))
os.environ.setdefault("JARVIS_FACE_AUTH_ENABLED", "false")
sys.path.insert(0, str(ROOT))


class ServerContractTests(unittest.TestCase):
    def test_server_module_exposes_all_required_routes(self):
        import server

        paths = {
            (route.path, ",".join(sorted(getattr(route, "methods", None) or [])))
            for route in server.app.routes
            if getattr(route, "methods", None)
        }
        required = {
            ("/", "GET"),
            ("/api/status", "GET"),
            ("/api/run", "POST"),
            ("/api/runs/{run_id}", "GET"),
            ("/api/runs/{run_id}/events", "GET"),
            ("/api/approval", "POST"),
            ("/api/stt", "POST"),
            ("/api/tts", "POST"),
            ("/api/auth/status", "GET"),
            ("/api/auth/passkey/register/options", "POST"),
            ("/api/auth/passkey/register/verify", "POST"),
            ("/api/auth/passkey/login/options", "POST"),
            ("/api/auth/passkey/login/verify", "POST"),
            ("/api/auth/face/enroll", "POST"),
            ("/api/auth/face/verify", "POST"),
            ("/api/auth/voice/enroll", "POST"),
            ("/api/auth/voice/verify", "POST"),
            ("/api/auth/logout", "POST"),
        }
        self.assertTrue(required.issubset(paths), required - paths)
        websocket_paths = {route.path for route in server.app.routes if route.__class__.__name__ == "APIWebSocketRoute"}
        self.assertIn("/api/wakeword", websocket_paths)

    def test_config_does_not_expose_api_key(self):
        import server

        config = server.public_status_config()
        self.assertNotIn("API_SERVER_KEY", config)
        self.assertNotIn("test-key", repr(config))
        self.assertEqual(config["wakeword"]["phrase"], "Hey Jarvis")
        self.assertIn("available", config["wakeword"])

    def test_dashboard_prevents_stale_authentication_javascript(self):
        import server

        with TestClient(server.app, base_url="http://localhost:8787") as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertIn('/static/auth-client.js?v=', response.text)
        self.assertIn('/static/app.js?v=', response.text)

    def test_run_is_rejected_without_authenticated_biometrics(self):
        import server

        with TestClient(server.app) as client:
            response = client.post("/api/run", json={"input": "do not send upstream"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["reason"], "passkey_required")

    def test_typed_run_requires_passkey_but_not_a_fresh_voice_match(self):
        import server

        session = server.auth_policy.create_session("typed-chat-session")
        server.auth_policy.mark_passkey(session)
        upstream = AsyncMock(return_value={"run_id": "typed-run"})
        try:
            with patch.object(server, "hermes_json", upstream):
                with TestClient(server.app) as client:
                    client.cookies.set(server.AUTH_COOKIE, session.token)
                    response = client.post(
                        "/api/run",
                        json={"input": "typed hello", "input_mode": "typed"},
                    )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["run_id"], "typed-run")
            self.assertNotIn("input_mode", upstream.await_args.kwargs["json"])
        finally:
            server.auth_policy.sessions.pop(session.token, None)

    def test_voice_run_still_requires_a_fresh_voice_match(self):
        import server

        session = server.auth_policy.create_session("spoken-command-session")
        server.auth_policy.mark_passkey(session)
        try:
            with TestClient(server.app) as client:
                client.cookies.set(server.AUTH_COOKIE, session.token)
                response = client.post(
                    "/api/run",
                    json={"input": "spoken hello", "input_mode": "voice"},
                )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"]["reason"], "voice_required")
        finally:
            server.auth_policy.sessions.pop(session.token, None)

    def test_auth_status_never_serializes_biometric_templates(self):
        import server

        with TestClient(server.app) as client:
            status = client.get("/api/auth/status").json()
        serialized = repr(status).lower()
        self.assertNotIn("public_key", serialized)
        self.assertNotIn("centroid", serialized)
        self.assertIn("enrollment", status)
        self.assertFalse(status["policy"]["face_required"])
        self.assertTrue(status["enrollment"]["face_disabled"])

    def test_face_endpoints_are_disabled(self):
        import server

        with TestClient(server.app) as client:
            response = client.post("/api/auth/face/verify", files={"image": ("face.jpg", b"unused", "image/jpeg")})
        self.assertEqual(response.status_code, 410)
        self.assertIn("disabled", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
