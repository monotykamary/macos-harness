"""Model-neutral, single-owner observe/act/check controller (no inference)."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


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


@dataclass(frozen=True)
class Snapshot:
    process: tuple
    generation: int
    targets: tuple[Target, ...]
    truncated: bool = False

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

    def _scope(self, scope: Any) -> str:
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
                [(hash(t.native), hash(t.window), t.state) for t in snapshot.targets],
                snapshot.truncated,
                limit,
            ]
        )
        self._current = (app, observation_id, snapshot, handles, limit)
        candidates = []
        for handle, target in handles.items():
            item = {
                "id": handle,
                "role": bounded(target.role, 80),
                "label": bounded(target.label),
                "operations": list(target.operations),
            }
            if target.value is not None:
                item["value"] = bounded(target.value)
            candidates.append(item)
        return {
            "scope": {"app": app},
            "observationId": observation_id,
            "revision": revision,
            "candidates": candidates,
            "truncated": snapshot.truncated,
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
        app = self._scope(scope)
        string(revision)
        timeout = integer(timeoutMs, 0, 60000) / 1000
        with self._lock:
            previous = (
                self._current if self._current and self._current[0] == app else None
            )
            limit = previous[4] if previous else 64
            self._current = None
            deadline = time.monotonic() + timeout
            # Never re-label a pre-wait snapshot as fresh. If the deadline is
            # too short for a new read, fail without minting replacement handles.
            observation = None
            while True:
                try:
                    # timeout=0 is one non-waiting observation (bounded to 2s).
                    snap = self.backend.read_snapshot(
                        app,
                        limit,
                        min(deadline, time.monotonic() + 2)
                        if timeout
                        else time.monotonic() + 2,
                    )
                    observation = self._publish(app, snap, limit)
                except ObservationTimeout:
                    if observation is None:
                        raise
                    break
                if observation["revision"] != revision or time.monotonic() >= deadline:
                    break
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            return {
                "changed": observation["revision"] != revision,
                "observation": observation,
            }
