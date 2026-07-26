import struct
import unittest

from wakeword import WakeWordEngine


class FakeModel:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.frames = []

    def predict(self, frame):
        self.frames.append(frame)
        return {"hey_jarvis": next(self.scores)}


class WakeWordEngineTests(unittest.TestCase):
    def test_detects_hey_jarvis_above_threshold(self):
        model = FakeModel([0.21, 0.81])
        engine = WakeWordEngine(model=model, threshold=0.7, cooldown_seconds=0)
        pcm = struct.pack("<1280h", *([120] * 1280))

        first = engine.process_pcm(pcm)
        second = engine.process_pcm(pcm)

        self.assertFalse(first["detected"])
        self.assertTrue(second["detected"])
        self.assertEqual(second["wake_word"], "hey_jarvis")
        self.assertAlmostEqual(second["score"], 0.81)
        self.assertEqual(len(model.frames[0]), 1280)

    def test_rejects_odd_length_pcm(self):
        engine = WakeWordEngine(model=FakeModel([1.0]))
        with self.assertRaisesRegex(ValueError, "16-bit PCM"):
            engine.process_pcm(b"abc")


if __name__ == "__main__":
    unittest.main()
