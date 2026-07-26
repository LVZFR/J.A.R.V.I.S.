import tempfile
import unittest
import io
import math
import wave
from pathlib import Path

from auth import AuthPolicy, AuthStore, FaceIdentity, PasskeyManager, VoiceprintManager, cosine_similarity


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class AuthPolicyTests(unittest.TestCase):
    def test_requires_passkey_recent_face_and_voice(self):
        clock = Clock()
        policy = AuthPolicy(clock=clock, face_ttl=15, voice_ttl=30, require_face=True)
        session = policy.create_session("session-a")

        self.assertEqual(policy.authorization(session)["reason"], "passkey_required")
        policy.mark_passkey(session)
        self.assertEqual(policy.authorization(session)["reason"], "face_required")
        policy.mark_face(session, matched=True)
        self.assertEqual(policy.authorization(session)["reason"], "voice_required")
        policy.mark_voice(session, matched=True)
        self.assertTrue(policy.authorization(session)["authorized"])

        clock.value += 16
        self.assertEqual(policy.authorization(session)["reason"], "face_required")

    def test_failed_biometric_clears_previous_match(self):
        policy = AuthPolicy(require_face=True)
        session = policy.create_session("session-b")
        policy.mark_passkey(session)
        policy.mark_face(session, True)
        policy.mark_voice(session, True)
        policy.mark_face(session, False)
        self.assertEqual(policy.authorization(session)["reason"], "face_required")

    def test_face_can_be_disabled_for_passkey_and_voice_only(self):
        policy = AuthPolicy(require_face=False)
        session = policy.create_session("session-no-face")
        policy.mark_passkey(session)
        self.assertEqual(policy.authorization(session)["reason"], "voice_required")
        policy.mark_voice(session, True)
        self.assertTrue(policy.authorization(session)["authorized"])
        policy.mark_face(session, False)
        self.assertTrue(policy.authorization(session)["authorized"])


class AuthStoreTests(unittest.TestCase):
    def test_store_round_trip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuthStore(Path(tmp))
            store.save_json("identity.json", {"owner": "Don"})
            self.assertEqual(store.load_json("identity.json"), {"owner": "Don"})
            self.assertEqual((Path(tmp) / "identity.json").stat().st_mode & 0o777, 0o600)

    def test_cosine_similarity_distinguishes_embeddings(self):
        self.assertAlmostEqual(cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertLess(cosine_similarity([1, 0], [0, 1]), 0.1)


class PasskeyManagerTests(unittest.TestCase):
    def test_registration_options_require_platform_user_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = PasskeyManager(AuthStore(Path(tmp)), rp_id="localhost", origin="http://localhost:8787")
            transaction, options = manager.registration_options()
            self.assertTrue(transaction)
            self.assertEqual(options["rp"]["id"], "localhost")
            self.assertEqual(options["authenticatorSelection"]["authenticatorAttachment"], "platform")
            self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")

    def test_verified_registration_is_persisted(self):
        class Verified:
            credential_id = b"credential-id"
            credential_public_key = b"public-key"
            sign_count = 7

        with tempfile.TemporaryDirectory() as tmp:
            store = AuthStore(Path(tmp))
            manager = PasskeyManager(
                store,
                rp_id="localhost",
                origin="http://localhost:8787",
                verify_registration=lambda **_: Verified(),
            )
            transaction, _ = manager.registration_options()
            manager.verify_registration_response(transaction, {"id": "ignored"})
            saved = store.load_json("passkeys.json")
            self.assertEqual(saved[0]["sign_count"], 7)
            self.assertTrue(manager.enrolled)


def wav_tone(frequency: float, phase: float = 0.0) -> bytes:
    import struct

    samples = [int(12000 * math.sin(2 * math.pi * frequency * i / 16000 + phase)) for i in range(16000)]
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


class VoiceprintTests(unittest.TestCase):
    def test_enrolls_templates_and_matches_similar_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = VoiceprintManager(AuthStore(Path(tmp)), threshold=0.90)
            manager.enroll([wav_tone(180, 0.0), wav_tone(180, 0.1), wav_tone(180, 0.2)])
            self.assertTrue(manager.enrolled)
            self.assertTrue(manager.verify(wav_tone(180, 0.15))["matched"])
            self.assertFalse(manager.verify(wav_tone(600, 0.15))["matched"])
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), [".template-key", "voiceprint.enc"])
            self.assertNotIn(b"centroid", (Path(tmp) / "voiceprint.enc").read_bytes())


class FakeRecognizer:
    def __init__(self):
        self.trained = False

    def train(self, faces, labels):
        self.trained = bool(faces) and len(faces) == len(labels)

    def write(self, path):
        Path(path).write_text("model", encoding="utf-8")

    def read(self, path):
        self.trained = Path(path).exists()

    def predict(self, face):
        return (0, 30.0 if float(face.mean()) < 100 else 90.0)


class FaceIdentityTests(unittest.TestCase):
    def test_enrollment_retains_model_not_raw_images(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            faces = {b"owner-a": np.full((32, 32), 30, dtype=np.uint8), b"owner-b": np.full((32, 32), 40, dtype=np.uint8), b"stranger": np.full((32, 32), 180, dtype=np.uint8)}
            identity = FaceIdentity(AuthStore(Path(tmp)), extractor=lambda data: faces[data], recognizer_factory=FakeRecognizer, threshold=65)
            identity.enroll([b"owner-a", b"owner-b"])
            self.assertTrue(identity.enrolled)
            self.assertTrue(identity.verify(b"owner-a")["matched"])
            self.assertFalse(identity.verify(b"stranger")["matched"])
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), [".template-key", "face-model.enc"])
            self.assertNotIn(b"model", (Path(tmp) / "face-model.enc").read_bytes())


if __name__ == "__main__":
    unittest.main()
