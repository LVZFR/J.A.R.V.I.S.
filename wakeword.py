"""Local wake-word detection for JARVIS using openWakeWord."""
from __future__ import annotations

import sys
import time
from array import array
from typing import Any, Callable


class WakeWordEngine:
    """Convert browser PCM frames into debounced openWakeWord detections."""

    def __init__(
        self,
        model: Any,
        threshold: float = 0.55,
        cooldown_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.last_detection = float("-inf")

    def process_pcm(self, pcm: bytes) -> dict[str, Any]:
        if not pcm or len(pcm) % 2:
            raise ValueError("Audio frame must contain little-endian 16-bit PCM")
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        try:
            import numpy as np

            frame: Any = np.asarray(samples, dtype=np.int16)
        except ImportError:
            frame = samples
        scores = self.model.predict(frame)
        wake_word, raw_score = max(scores.items(), key=lambda item: float(item[1]))
        score = float(raw_score)
        now = self.clock()
        detected = score >= self.threshold and now - self.last_detection >= self.cooldown_seconds
        if detected:
            self.last_detection = now
        return {"detected": detected, "wake_word": str(wake_word), "score": score}


def load_engine(threshold: float = 0.55) -> WakeWordEngine:
    """Download the bundled Hey Jarvis model if needed, then load it locally."""
    from openwakeword.model import Model
    from openwakeword.utils import download_models

    download_models(["hey_jarvis"])
    model = Model(
        wakeword_models=["hey_jarvis"],
        inference_framework="onnx",
        vad_threshold=0.5,
    )
    return WakeWordEngine(model=model, threshold=threshold)
