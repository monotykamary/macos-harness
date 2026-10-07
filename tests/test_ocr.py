"""Vision adapter tests. Native smoke is opt-in and uses only a synthetic image."""

import io
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from macos_harness import VisionOCR
from macos_harness.guarded import ObservationTimeout
from macos_harness.macos import MacOSError
from macos_harness.ocr import OCRCapture


def image_bytes():
    image = Image.new("RGB", (800, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (30, 40), "Hello Vision 123", fill="black", font=ImageFont.load_default(size=48)
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_vision_defaults_and_validation():
    assert VisionOCR().recognition_level == "fast"
    with pytest.raises(ValueError):
        VisionOCR("unknown")
    with pytest.raises(ObservationTimeout):
        VisionOCR().read(OCRCapture(b"", 1, (0, 0, 1, 1), ""), time.monotonic() - 1)


def test_vision_adapter_normalizes_bounds_and_clips(monkeypatch):
    calls = []
    candidate = SimpleNamespace(string=lambda: "Read locally", confidence=lambda: 0.95)
    observation = SimpleNamespace(
        topCandidates_=lambda count: [candidate],
        boundingBox=lambda: SimpleNamespace(
            origin=SimpleNamespace(x=0.1, y=0.2),
            size=SimpleNamespace(width=0.4, height=0.3),
        ),
    )
    request = SimpleNamespace(
        setRecognitionLevel_=lambda level: calls.append(level),
        setUsesLanguageCorrection_=lambda value: calls.append(value),
        setUsesCPUOnly_=lambda value: None,
        cancel=lambda: calls.append("cancel"),
        results=lambda: [observation, observation],
    )
    handler = SimpleNamespace(
        performRequests_error_=lambda requests, error: (True, None)
    )
    vision = SimpleNamespace(
        VNRecognizeTextRequest=SimpleNamespace(
            alloc=lambda: SimpleNamespace(init=lambda: request)
        ),
        VNImageRequestHandler=SimpleNamespace(
            alloc=lambda: SimpleNamespace(
                initWithCGImage_options_=lambda image, options: handler
            )
        ),
        VNRequestTextRecognitionLevelAccurate=10,
        VNRequestTextRecognitionLevelFast=20,
    )
    monkeypatch.setitem(sys.modules, "Vision", vision)
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(
            CGImageSourceCreateWithData=lambda *args: object(),
            CGImageSourceCreateImageAtIndex=lambda *args: object(),
            CGImageGetWidth=lambda image: 100,
            CGImageGetHeight=lambda image: 100,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(
            NSData=SimpleNamespace(dataWithBytes_length_=lambda data, length: data)
        ),
    )
    regions, truncated = VisionOCR("fast")._read_native(
        OCRCapture(b"data", 1, (0, 0, 100, 100), "hash"), time.monotonic() + 2, limit=1
    )
    assert truncated and len(regions) == 1
    assert regions[0].bounds == pytest.approx((0.1, 0.5, 0.4, 0.3))
    assert regions[0].text == "Read locally" and calls == [20, False]


def test_capture_is_window_only_bounded_and_erases_temporary_file(monkeypatch):
    from macos_harness import ocr

    paths = []
    permissions = []
    bounds = {"x": 0, "y": 0, "width": 800, "height": 160}
    mac = SimpleNamespace(
        _ensure_screen_recording=lambda: permissions.append("checked"),
        windows=lambda app: [{"window_id": 77, "on_screen": True, "bounds": bounds}],
    )

    def capture(argv, **kwargs):
        assert argv[:5] == ["/usr/sbin/screencapture", "-x", "-o", "-l", "77"]
        assert 0 < kwargs["timeout"] <= 2
        path = Path(argv[-1])
        paths.append(path)
        path.write_bytes(image_bytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ocr.subprocess, "run", capture)
    result = VisionOCR().capture(mac, 42, 77, (0, 0, 800, 160), time.monotonic() + 2)
    assert result.png.startswith(b"\x89PNG") and len(result.fingerprint) == 64
    assert permissions == ["checked"] and not paths[0].exists()
    bounds["x"] = 5
    with pytest.raises(MacOSError, match="moved"):
        VisionOCR().capture(mac, 42, 77, (0, 0, 800, 160), time.monotonic() + 2)
    assert not paths[-1].exists()


def test_worker_deadline_reaps_hung_process(monkeypatch):
    import subprocess

    from macos_harness import ocr

    launch = subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = launch(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys,time; print('{\"ready\":true}', flush=True); time.sleep(60)",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(ocr.subprocess, "Popen", spawn)
    reader = VisionOCR()
    with pytest.raises(ObservationTimeout):
        reader.read(OCRCapture(b"png", 0, (0, 0, 1, 1), "test"), time.monotonic() + 0.2)
    assert reader._worker is None
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize(
    "result",
    [
        b'{"regions":[],"truncated":"false"}',
        b'{"regions":[{"text":"x","bounds":[0,0,1,1],"confidence":2}],"truncated":false}',
    ],
)
def test_worker_result_is_validated_without_leaking_payload(monkeypatch, result):
    import json

    reader = VisionOCR()
    monkeypatch.setattr(reader, "start", lambda deadline: None)
    monkeypatch.setattr(reader, "_exchange", lambda *args: json.loads(result))
    with pytest.raises(MacOSError, match="Invalid local Vision result"):
        reader.read(OCRCapture(b"png", 0, (0, 0, 1, 1), "test"), time.monotonic() + 1)


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("MACOS_HARNESS_TEST_VISION") != "1",
    reason="explicit synthetic native Vision smoke only",
)
def test_native_vision_synthetic_image():
    png = image_bytes()
    with VisionOCR() as reader:
        regions, truncated = reader.read(
            OCRCapture(png, 0, (0, 0, 800, 160), "synthetic"), time.monotonic() + 2
        )
        pid = reader._worker.pid
        assert reader.read(
            OCRCapture(png, 0, (0, 0, 800, 160), "synthetic"), time.monotonic() + 2
        )[0]
        assert reader._worker.pid == pid
    assert reader._worker is None
    assert "Hello Vision 123" in " ".join(region.text for region in regions)
    assert not truncated
    assert all(0 <= region.confidence <= 1 for region in regions)
