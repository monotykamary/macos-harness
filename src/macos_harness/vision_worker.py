"""Standalone Vision worker: deliberately imports neither harness nor browser code."""

import json
import math
import os
import signal
import sys
import time


def recognize(png: bytes, level: str, limit: int, timeout: float) -> dict:
    import Quartz
    import Vision
    from Foundation import NSData

    deadline = time.monotonic() + timeout
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelAccurate
        if level == "accurate"
        else Vision.VNRequestTextRecognitionLevelFast
    )
    request.setUsesLanguageCorrection_(False)
    request.setUsesCPUOnly_(True)
    data = NSData.dataWithBytes_length_(png, len(png))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    image = (
        Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if source is not None
        else None
    )
    if (
        image is None
        or Quartz.CGImageGetWidth(image) * Quartz.CGImageGetHeight(image) > 16_000_000
    ):
        raise ValueError("Invalid or oversized image")
    # nil options avoid native lookups into a Python-backed dictionary.
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok or error is not None or time.monotonic() >= deadline:
        raise ValueError("Vision request did not complete")
    observations = request.results() or []
    regions = []
    for observation in observations[:limit]:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        box = observation.boundingBox()
        bounds = (
            float(box.origin.x),
            1 - float(box.origin.y + box.size.height),
            float(box.size.width),
            float(box.size.height),
        )
        confidence = float(candidate.confidence())
        if not all(math.isfinite(v) for v in (*bounds, confidence)):
            continue
        x, y, w, h = bounds
        if (
            x < 0
            or y < -1e-6
            or w <= 0
            or h <= 0
            or x + w > 1.000001
            or y + h > 1.000001
        ):
            continue
        text = "".join(c if c.isprintable() else " " for c in str(candidate.string()))
        text = text.encode("utf-8", errors="replace")[:160].decode(
            "utf-8", errors="ignore"
        )
        if text.strip():
            regions.append(
                {
                    "text": text,
                    "bounds": (x, max(0, y), w, h),
                    "confidence": max(0, min(1, confidence)),
                }
            )
    return {
        "regions": sorted(
            regions, key=lambda r: (r["bounds"][1], r["bounds"][0], r["text"])
        ),
        "truncated": len(observations) > limit,
    }


def main() -> int:
    # Native Vision can hold the GIL. A process-level timer also bounds a request
    # if its parent disappears mid-recognition; no Python callback is required.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, 8)
    wire = os.dup(sys.stdout.fileno())
    try:
        with open(os.devnull, "wb") as quiet:
            os.dup2(quiet.fileno(), sys.stdout.fileno())
            os.dup2(quiet.fileno(), sys.stderr.fileno())
        import objc
        import Quartz  # noqa: F401 - warm native frameworks before the handshake
        import Vision  # noqa: F401

        level = sys.argv[1]
        if level not in {"fast", "accurate"}:
            return 2
        with os.fdopen(wire, "wb", closefd=False) as output:
            signal.setitimer(signal.ITIMER_REAL, 0)
            output.write(b'{"ready":true}\n')
            output.flush()
            while True:
                line = sys.stdin.buffer.readline(256)
                if not line:
                    return 0
                if not line.endswith(b"\n"):
                    return 2
                header = json.loads(line)
                size, maximum, seconds = (
                    header["bytes"],
                    header["limit"],
                    header["timeout"],
                )
                if (
                    not 0 <= size <= 32 * 1024 * 1024
                    or not 1 <= maximum <= 128
                    or not 0 < seconds <= 60
                ):
                    return 2
                signal.setitimer(signal.ITIMER_REAL, seconds)
                png = sys.stdin.buffer.read(size)
                if len(png) != size:
                    return 2
                with objc.autorelease_pool():
                    payload = (
                        json.dumps(recognize(png, level, maximum, seconds)).encode()
                        + b"\n"
                    )
                signal.setitimer(signal.ITIMER_REAL, 0)
                if len(payload) > 128 * 1024:
                    return 2
                output.write(payload)
                output.flush()
    except Exception:  # noqa: BLE001 - private native errors never cross the worker boundary
        return 2
    finally:
        os.close(wire)


if __name__ == "__main__":
    raise SystemExit(main())
