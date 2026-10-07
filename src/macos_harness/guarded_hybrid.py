"""Opt-in AX + Vision observations. Pixel actions never bypass an AX denial."""

from __future__ import annotations

import math
import time
from dataclasses import replace

from .guarded import ObservationTimeout, Snapshot, Target, bounded, digest
from .guarded_ax import NativeAXBackend
from .ocr import VisionOCR


class NativeHybridBackend(NativeAXBackend):
    CONTAINERS = frozenset(
        {
            "AXApplication",
            "AXWindow",
            "AXGroup",
            "AXScrollArea",
            "AXSplitGroup",
            "AXTabGroup",
            "AXToolbar",
        }
    )

    def __init__(
        self, mac, *, ocr: str = "auto", recognition_level: str = "fast", provider=None
    ):
        if ocr not in {"never", "auto", "always"}:
            raise ValueError("ocr must be never, auto or always")
        super().__init__(mac)
        self.ocr_mode = ocr
        self.ocr = provider if provider is not None else VisionOCR(recognition_level)
        if provider is None and ocr != "never":
            # Explicit hybrid-backend construction warms one owned worker, without
            # looking at apps or checking/requesting permissions.
            self.ocr.start(time.monotonic() + 8)

    def close(self) -> None:
        super().close()
        close = getattr(self.ocr, "close", None)
        if close is not None:
            close()

    def read_snapshot(self, app: str, limit: int, deadline: float) -> Snapshot:
        snapshot = super().read_snapshot(app, limit, deadline)
        if self.ocr_mode == "never":
            return snapshot
        controls = [t for t in snapshot.targets if t.role not in self.CONTAINERS]
        if not snapshot.capture_safe or snapshot.process[2] in {
            "com.apple.systempreferences",
            "com.apple.SecurityAgent",
        }:
            return replace(snapshot, ocr_status="blocked")
        if self.ocr_mode == "auto" and any(t.operations and t.label and not t.chrome for t in controls):
            return replace(snapshot, ocr_status="not_needed")
        # No geometry means we cannot exclude a known control from pixel targeting.
        if any(
            t.bounds is None and (t.label or t.operations or t.value) for t in controls
        ):
            return replace(snapshot, ocr_status="blocked")
        windows = [t for t in snapshot.targets if t.role == "AXWindow"]
        if len(windows) != 1 or windows[0].bounds is None:
            return replace(snapshot, ocr_status="blocked")
        window = windows[0]
        matches = [
            w
            for w in self.mac.windows(str(snapshot.process[0]))
            if w["on_screen"]
            and all(
                math.isclose(w["bounds"][key], value, abs_tol=0.5, rel_tol=0)
                for key, value in zip(("x", "y", "width", "height"), window.bounds)
            )
        ]
        if len(matches) != 1:
            return replace(snapshot, ocr_status="blocked")
        selected = matches[0]
        bounds = tuple(selected["bounds"][key] for key in ("x", "y", "width", "height"))
        capture = self.ocr.capture(
            self.mac, snapshot.process[0], selected["window_id"], bounds, deadline
        )
        regions, clipped = self.ocr.read(capture, deadline)
        # A permission dialog/protected field could have appeared while pixels were read.
        fresh = super().read_snapshot(app, limit, deadline)
        if not snapshot.equivalent(fresh):
            raise ObservationTimeout("AX changed during OCR observation")
        targets = list(snapshot.targets)
        labels = {t.label.casefold().strip() for t in controls if t.label}
        for index, region in enumerate(regions):
            label = bounded(region.text)
            if (
                not label
                or label.casefold().strip() in labels
                or self._SENSITIVE.search(label)
            ):
                continue
            x, y, width, height = region.bounds
            rect = (
                bounds[0] + x * bounds[2],
                bounds[1] + y * bounds[3],
                width * bounds[2],
                height * bounds[3],
            )
            # Never offer pixel clicks over a native control, including disabled ones.
            if any(t.bounds is not None and overlaps(rect, t.bounds) for t in controls):
                continue
            if len(targets) >= limit:
                clipped = True
                break
            approval = label.casefold().startswith(
                ("allow", "approve", "grant", "authorize")
            )
            operations = ("click",) if region.confidence >= 0.8 and not approval else ()
            targets.append(
                Target(
                    (capture.window_id, index),
                    window.native,
                    digest(
                        [
                            capture.fingerprint,
                            capture.window_id,
                            bounds,
                            region.text,
                            rect,
                            region.confidence,
                        ]
                    ),
                    "OCRText",
                    label,
                    operations,
                    source="ocr",
                    bounds=rect,
                    confidence=region.confidence,
                )
            )
        return replace(
            snapshot,
            targets=tuple(targets),
            truncated=snapshot.truncated or clipped,
            ocr_status="used",
        )

    def execute(
        self,
        app: str,
        snapshot: Snapshot,
        target: Target,
        operation: str,
        text: str | None,
    ) -> dict:
        if target.source != "ocr":
            return super().execute(app, snapshot, target, operation, text)
        if (
            operation != "click"
            or operation not in target.operations
            or target.bounds is None
        ):
            return {"status": "blocked", "reason": "Unsupported OCR operation"}
        try:
            # Re-OCR and compare the full raster fingerprint, window and process. A
            # same-looking label alone is never enough to authorize a coordinate.
            fresh = self.read_snapshot(app, 128, time.monotonic() + 2)
            if (
                snapshot.process != fresh.process
                or snapshot.generation != fresh.generation
                or snapshot.event_sequence != fresh.event_sequence
                or target not in fresh.targets
            ):
                return {"status": "stale", "reason": "OCR target or window changed"}
            self.mac._ensure_post_events()
            before = self.mac._frontmost_app()
            if before is None or int(before["pid"]) != snapshot.process[0]:
                return {
                    "status": "blocked",
                    "reason": "Background OCR effects are not focus-safe",
                }
            if self._process(app) != snapshot.process or (
                self._journal is not None
                and self._journal.sequence != snapshot.event_sequence
            ):
                return {"status": "stale", "reason": "App changed before OCR input"}
        except Exception:  # noqa: BLE001 - fail closed before native dispatch
            return {"status": "stale", "reason": "Unable to revalidate OCR target"}
        x, y, width, height = target.bounds
        try:
            self.mac.click(
                x + width / 2,
                y + height / 2,
                app=str(snapshot.process[0]),
                coordinate_space="screen",
            )
            if self.mac._frontmost_app() != before:
                return {
                    "status": "outcome_unknown",
                    "reason": "Focus changed during OCR input",
                }
        except Exception:  # noqa: BLE001 - a dispatched effect cannot be replayed
            return {
                "status": "outcome_unknown",
                "reason": "OCR input may have happened; inspect before deciding",
            }
        return {"status": "executed"}


def overlaps(a: tuple, b: tuple) -> bool:
    return (
        a[0] < b[0] + b[2]
        and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3]
        and b[1] < a[1] + a[3]
    )
