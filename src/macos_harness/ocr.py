"""Local Apple Vision OCR. No inference service, implicit capture, or persisted image."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import select
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .guarded import ObservationTimeout, bounded, integer
from .macos import MacOSError


@dataclass(frozen=True)
class OCRText:
    text: str
    # Normalized, top-left-origin rectangle. Independent of Retina pixel density.
    bounds: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class OCRCapture:
    png: bytes
    window_id: int
    bounds: tuple[float, float, float, float]
    fingerprint: str


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ObservationTimeout("OCR observation deadline reached")
    return value


class VisionOCR:
    """Lazy native recognizer; construction never imports Vision or requests access."""

    def __init__(self, recognition_level: str = "fast"):
        if recognition_level not in {"accurate", "fast"}:
            raise ValueError("recognition_level must be accurate or fast")
        self.recognition_level = recognition_level
        self._worker = None

    def __enter__(self) -> Self:
        self.start(time.monotonic() + 8)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        try:
            worker.terminate()
            worker.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=1)
        finally:
            worker.stdin.close()
            worker.stdout.close()

    def start(self, deadline: float) -> None:
        if self._worker is not None:
            return
        remaining(deadline)
        self._worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("vision_worker.py")),
                self.recognition_level,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        os.set_blocking(self._worker.stdin.fileno(), False)
        os.set_blocking(self._worker.stdout.fileno(), False)
        try:
            if self._exchange(b"", deadline) != {"ready": True}:
                raise MacOSError("Invalid Vision worker handshake")
        except Exception:
            self.close()
            raise

    def _exchange(self, data: bytes, deadline: float) -> dict:
        worker = self._worker
        pending = memoryview(data)
        response = bytearray()
        while True:
            readers, writers, _ = select.select(
                [worker.stdout],
                [worker.stdin] if pending else [],
                [],
                remaining(deadline),
            )
            if writers:
                try:
                    pending = pending[os.write(worker.stdin.fileno(), pending) :]
                except BlockingIOError:
                    pass
            if readers:
                chunk = os.read(
                    worker.stdout.fileno(), min(4096, 128 * 1024 + 1 - len(response))
                )
                if not chunk:
                    raise MacOSError("Vision worker disconnected")
                response.extend(chunk)
                if len(response) > 128 * 1024:
                    raise MacOSError("Vision response exceeds frame budget")
                if b"\n" in response:
                    if (
                        pending
                        or not response.endswith(b"\n")
                        or response.count(b"\n") != 1
                    ):
                        raise MacOSError("Invalid Vision worker frame")
                    return json.loads(response)

    def capture(
        self, mac, pid: int, window_id: int, bounds: tuple, deadline: float
    ) -> OCRCapture:
        mac._ensure_screen_recording()  # Non-prompting. Never fall back to desktop capture.
        remaining(deadline)
        with tempfile.TemporaryDirectory(prefix="macos-harness-ocr-") as directory:
            path = Path(directory) / "window.png"
            try:
                result = subprocess.run(
                    [
                        "/usr/sbin/screencapture",
                        "-x",
                        "-o",
                        "-l",
                        str(window_id),
                        str(path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=remaining(deadline),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ObservationTimeout("Window capture deadline reached") from None
            if (
                result.returncode
                or not path.exists()
                or path.stat().st_size > 32 * 1024 * 1024
            ):
                raise MacOSError("Unable to capture bounded OCR window")
            png = path.read_bytes()
        remaining(deadline)
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            if image.width * image.height > 16_000_000:
                raise MacOSError("OCR window exceeds pixel budget")
            fingerprint = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
        windows = [w for w in mac.windows(str(pid)) if w["window_id"] == window_id]
        if len(windows) != 1 or not windows[0]["on_screen"]:
            raise MacOSError("OCR window no longer available")
        current = tuple(
            windows[0]["bounds"][key] for key in ("x", "y", "width", "height")
        )
        if current != bounds:
            raise MacOSError("OCR window moved during capture")
        remaining(deadline)
        return OCRCapture(png, window_id, bounds, fingerprint)

    def read(
        self, capture: OCRCapture, deadline: float, limit: int = 128
    ) -> tuple[tuple[OCRText, ...], bool]:
        """Reuse a killable worker; the owner must close it (or use a context manager)."""
        integer(limit, 1, 128)
        timeout = min(60, remaining(deadline))
        if len(capture.png) > 32 * 1024 * 1024:
            raise MacOSError("OCR image exceeds input budget")
        try:
            self.start(deadline)
            header = (
                json.dumps(
                    {"bytes": len(capture.png), "limit": limit, "timeout": timeout}
                ).encode()
                + b"\n"
            )
            payload = self._exchange(header + capture.png, deadline)
            remaining(deadline)
        except ObservationTimeout:
            self.close()
            raise
        except Exception:  # noqa: BLE001 - never leak native transport diagnostics
            self.close()
            raise MacOSError("Local Vision OCR failed") from None
        try:
            if (
                set(payload) != {"regions", "truncated"}
                or type(payload["truncated"]) is not bool
            ):
                raise ValueError
            regions = []
            if (
                not isinstance(payload["regions"], list)
                or len(payload["regions"]) > limit
            ):
                raise ValueError
            for region in payload["regions"]:
                if set(region) != {"text", "bounds", "confidence"} or not isinstance(
                    region["text"], str
                ):
                    raise ValueError
                rect, confidence = region["bounds"], region["confidence"]
                if (
                    not isinstance(rect, list)
                    or len(rect) != 4
                    or not all(
                        type(v) in (int, float) and math.isfinite(v)
                        for v in (*rect, confidence)
                    )
                ):
                    raise ValueError
                x, y, w, h = rect
                if not (
                    0 <= confidence <= 1
                    and x >= 0
                    and y >= 0
                    and w > 0
                    and h > 0
                    and x + w <= 1.000001
                    and y + h <= 1.000001
                ):
                    raise ValueError
                regions.append(
                    OCRText(bounded(region["text"]), tuple(rect), confidence)
                )
            return tuple(regions), payload["truncated"]
        except (ValueError, TypeError, KeyError):
            self.close()
            raise MacOSError("Invalid local Vision result") from None

    def _read_native(
        self, capture: OCRCapture, deadline: float, limit: int
    ) -> tuple[tuple[OCRText, ...], bool]:
        # Injectable native seam for offline tests; production always uses read's worker.
        from .vision_worker import recognize

        payload = recognize(
            capture.png, self.recognition_level, limit, remaining(deadline)
        )
        return tuple(
            OCRText(r["text"], r["bounds"], r["confidence"]) for r in payload["regions"]
        ), payload["truncated"]
