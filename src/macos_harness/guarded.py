"""Model-neutral, single-owner observe/act/check controller (no inference)."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, Self


class GuardedError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ObservationTimeout(RuntimeError):
    pass


def fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise GuardedError("invalid_args", "Missing or unknown fields")
    return value


def valid_text(value: Any, maximum: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def string(value: Any, maximum: int = 256) -> str:
    if not value or not valid_text(value, maximum):
        raise GuardedError("invalid_args", "Expected a bounded nonempty string")
    return value


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise GuardedError(
            "invalid_args", f"Expected integer in [{minimum}, {maximum}]"
        )
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def bounded(value: Any, maximum: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    # Keep JSON expansion bounded too; do not expose control characters.
    value = "".join(c if c.isprintable() else " " for c in value)
    return value.encode("utf-8", errors="replace")[:maximum].decode(
        "utf-8", errors="ignore"
    )


@dataclass(frozen=True)
class Target:
    native: Any  # Strong reference pins the AXUIElement, never a raw integer index.
    window: Any
    state: str
    role: str
    label: str
    operations: tuple[str, ...]
    value: str | None = None
    source: str = "ax"
    bounds: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    chrome: bool = False  # Title-bar controls do not make an AX-poor app usable.


@dataclass(frozen=True)
class Snapshot:
    process: tuple
    generation: int
    targets: tuple[Target, ...]
    truncated: bool = False
    event_sequence: int = 0
    change_source: str = "polling"
    capture_safe: bool = False
    ocr_status: str = "disabled"

    def equivalent(self, other: Snapshot) -> bool:
        return self == other


class GuardedBackend(Protocol):
    """Trusted injectable backend. execute must revalidate before the native call.

    After entering a native effect, any failure MUST be outcome_unknown, not an
    exception or a retry. read_snapshot must be bounded and side-effect free.
    """

    def read_snapshot(self, app: str, limit: int, deadline: float) -> Snapshot: ...
    def execute(
        self,
        app: str,
        snapshot: Snapshot,
        target: Target,
        operation: str,
        text: str | None,
    ) -> dict: ...


class GuardedController:
    """One latest observation globally; every observe replaces all older handles.

    Pass NativeAXBackend(mac) in production or a GuardedBackend in offline tests.
    Public methods accept the same argument objects as the JSON-lines RPC.
    """

    def __init__(self, backend: GuardedBackend, allowed_apps: list[str]):
        if not allowed_apps:
            raise GuardedError("invalid_args", "At least one allowed app is required")
        self.backend = backend
        self.allowed_apps = frozenset(string(app) for app in allowed_apps)
        self._lock = threading.Lock()
        self._salt = secrets.token_hex(16)
        self._current: tuple | None = None
        self._limits: dict[str, int] = {}
        self._closed = False

    def app(self, app: str) -> GuardedApp:
        return GuardedApp(self, self._scope({"app": app}))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._current = None
            close = getattr(self.backend, "close", None)
            if close is not None:
                close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _scope(self, scope: Any) -> str:
        if self._closed:
            raise GuardedError("closed", "Controller is closed")
        app = string(fields(scope, {"app"})["app"])
        if app not in self.allowed_apps:
            raise GuardedError("scope_denied", "App is not in the configured allowlist")
        return app

    def _publish(self, app: str, snapshot: Snapshot, limit: int) -> dict:
        observation_id = secrets.token_hex(16)
        handles = {secrets.token_hex(16): target for target in snapshot.targets}
        revision = digest(
            [
                self._salt,
                app,
                snapshot.process,
                snapshot.generation,
                snapshot.event_sequence,
                snapshot.ocr_status,
                [(hash(t.native), hash(t.window), t.state) for t in snapshot.targets],
                snapshot.truncated,
                limit,
            ]
        )
        self._current = (app, observation_id, snapshot, handles, limit)
        self._limits[app] = limit
        candidates = []
        for handle, target in handles.items():
            item = {
                "id": handle,
                "role": bounded(target.role, 80),
                "label": bounded(target.label),
                "operations": list(target.operations),
                "source": target.source,
            }
            if target.value is not None:
                item["value"] = bounded(target.value)
            if target.bounds is not None:
                item["bounds"] = dict(zip(("x", "y", "width", "height"), target.bounds))
            if target.confidence is not None:
                item["confidence"] = target.confidence
            candidates.append(item)
        return {
            "scope": {"app": app},
            "observationId": observation_id,
            "revision": revision,
            "candidates": candidates,
            "truncated": snapshot.truncated,
            "changeSource": snapshot.change_source,
            "ocrStatus": snapshot.ocr_status,
        }

    def observe(self, scope: dict, maxElements: int = 64) -> dict:
        app = self._scope(scope)
        limit = integer(maxElements, 1, 128)
        with self._lock:
            self._current = None
            snap = self.backend.read_snapshot(app, limit, time.monotonic() + 2)
            return self._publish(app, snap, limit)

    def act(self, scope: dict, observationId: str, action: dict) -> dict:
        app = self._scope(scope)
        string(observationId)
        fields(action, {"targetId", "operation"}, {"text"})
        handle = string(action["targetId"])
        operation = string(action["operation"], 32)
        text = action.get("text")
        if operation == "setValue":
            if not valid_text(text, 4096):
                raise GuardedError(
                    "invalid_args", "setValue requires text (at most 4096 bytes)"
                )
        elif "text" in action:
            raise GuardedError("invalid_args", "text is only valid for setValue")
        with self._lock:
            current = self._current
            if current is None or current[:2] != (app, observationId):
                return {
                    "status": "stale",
                    "reason": "Observation expired or belongs to another scope",
                }
            self._current = None  # Consume before validation/effect; never replay.
            _, _, snapshot, handles, limit = current
            target = handles.get(handle)
            if target is None:
                return {
                    "status": "blocked",
                    "reason": "Unknown observation-scoped target",
                }
            if operation not in target.operations:
                return {
                    "status": "blocked",
                    "reason": "Operation not supported by this target",
                }
            try:
                fresh = self.backend.read_snapshot(app, limit, time.monotonic() + 2)
                if not snapshot.equivalent(fresh):
                    return {
                        "status": "stale",
                        "reason": "App, window, generation, or UI state changed",
                    }
            except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
                return {"status": "stale", "reason": "Unable to revalidate observation"}
            # A trusted backend owns the precise effect boundary. Unexpected errors
            # are conservatively uncertain, even if they occurred before dispatch.
            try:
                return self.backend.execute(app, snapshot, target, operation, text)
            except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
                return {
                    "status": "outcome_unknown",
                    "reason": "Effect receipt unavailable; observe before deciding",
                }

    def waitForChange(self, scope: dict, revision: str, timeoutMs: int = 1000) -> dict:
        return self._wait(scope, revision, timeoutMs)

    def settle(
        self, scope: dict, revision: str, timeoutMs: int = 2000,
        reactionMs: int = 600, quietMs: int = 150,
    ) -> dict:
        """Wait for reaction then quiet; neither quiet nor a receipt proves success."""
        reaction = integer(reactionMs, 0, 60000) / 1000
        quiet = integer(quietMs, 0, 60000) / 1000
        return self._wait(scope, revision, timeoutMs, (reaction, quiet))

    def _wait(
        self, scope: dict, revision: str, timeoutMs: int,
        settling: tuple[float, float] | None = None,
    ) -> dict:
        app = self._scope(scope)
        string(revision)
        timeout = integer(timeoutMs, 0, 60000) / 1000
        with self._lock:
            limit = self._limits.get(app, 64)
            self._current = None
            started = time.monotonic()
            deadline = started + timeout
            last_revision, last_change = revision, started
            reacted = settled = timed_out = False
            observation = None
            while True:
                try:
                    snap = self.backend.read_snapshot(
                        app, limit, min(deadline, time.monotonic() + 2)
                        if timeout else time.monotonic() + 2,
                    )
                    observation = self._publish(app, snap, limit)
                except ObservationTimeout:
                    if observation is None:
                        raise
                    timed_out = True
                    break
                now = time.monotonic()
                if observation["revision"] != last_revision:
                    reacted, last_change = True, now
                    last_revision = observation["revision"]
                if settling is None:
                    if reacted or now >= deadline:
                        break
                    wake = deadline
                else:
                    reaction, quiet = settling
                    settled = reacted and now >= last_change + quiet
                    if settled or (not reacted and now >= started + reaction):
                        break
                    if now >= deadline:
                        timed_out = True
                        break
                    wake = min(deadline, last_change + quiet if reacted else started + reaction)
                delay = min(0.1, max(0, wake - time.monotonic()))
                wait = getattr(self.backend, "wait_for_change", None)
                if wait is None:
                    time.sleep(delay)
                else:
                    wait(app, snap.event_sequence, delay)
            if settling is not None:
                return {"reacted": reacted, "settled": settled, "timedOut": timed_out, "observation": observation}
            return {"changed": observation["revision"] != revision, "observation": observation}


class GuardedApp:
    """An allowlisted app scope, not a frontmost-app pointer or new authority."""

    def __init__(self, controller: GuardedController, app: str):
        self._controller = controller
        self._app = controller._scope({"app": app})

    def observe(self, maxElements: int = 64) -> dict:
        return self._controller.observe({"app": self._app}, maxElements)

    def act(self, observationId: str, action: dict) -> dict:
        return self._controller.act({"app": self._app}, observationId, action)

    def waitForChange(self, revision: str, timeoutMs: int = 1000) -> dict:
        return self._controller.waitForChange({"app": self._app}, revision, timeoutMs)

    def settle(self, revision: str, timeoutMs: int = 2000, reactionMs: int = 600, quietMs: int = 150) -> dict:
        return self._controller.settle({"app": self._app}, revision, timeoutMs, reactionMs, quietMs)
