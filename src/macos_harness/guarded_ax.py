"""Bounded semantic AX backend; independent of screenshots and raw index maps."""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from . import macos as native
from .guarded import ObservationTimeout, Snapshot, Target, bounded, digest
from .macos import MacOS, MacOSError


class NativeAXBackend:
    MAX_VISITS = 512
    MAX_DEPTH = 20
    AX_TIMEOUT = 0.05
    _TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXStaticText", "AXComboBox"})
    _VALUE_ROLES = _TEXT_ROLES | {"AXCheckBox", "AXRadioButton", "AXSwitch"}
    _SENSITIVE = re.compile(r"password|passcode|secret|token|api[ _-]?key|credit[ _-]?card|one[ _-]?time", re.IGNORECASE)
    _DIALOGS = frozenset({"AXDialog", "AXSystemDialog", "AXSheet"})

    def __init__(self, mac: MacOS):
        self.mac = mac

    def _process(self, app: str) -> tuple:
        running, info = self.mac._resolve_app(app)
        # Raw APIs accept substrings. The guarded surface deliberately does not.
        exact = [info.get(k) for k in ("name", "bundle_id", "path", "pid")]
        if app.casefold() not in [str(v).casefold() for v in exact if v is not None]:
            raise MacOSError("Guarded app selectors must match exactly")
        launched = running.launchDate()
        if launched is None or running.isTerminated():
            raise MacOSError("Cannot establish application process identity")
        return (
            int(info["pid"]),
            float(launched.timeIntervalSince1970()),
            info.get("bundle_id"),
            info.get("path"),
        )

    @staticmethod
    def _budget(deadline: float) -> None:
        if time.monotonic() + NativeAXBackend.AX_TIMEOUT >= deadline:
            raise ObservationTimeout("AX observation deadline reached")

    def _get(self, element: Any, name: str, deadline: float) -> Any:
        self._budget(deadline)
        error, value = native.AS.AXUIElementCopyAttributeValue(element, name, None)
        # Unsupported/absent optional attributes are normal. A disconnected app
        # or failed security-metadata read is not evidence that a field is safe.
        if error in {-25205, -25212}:  # AttributeUnsupported, NoValue
            return None
        if error:
            raise MacOSError("Unable to read guarded AX attribute")
        return value

    def _children(
        self, element: Any, name: str, count: int, deadline: float
    ) -> tuple[list, bool]:
        self._budget(deadline)
        error, total = native.AS.AXUIElementGetAttributeValueCount(element, name, None)
        if error in {-25205, -25212}:  # AttributeUnsupported, NoValue
            return [], False
        if error:
            raise MacOSError("Unable to count bounded AX children")
        if not total:
            return [], False
        self._budget(deadline)
        error, values = native.AS.AXUIElementCopyAttributeValues(
            element, name, 0, min(total, count), None
        )
        if error:
            raise MacOSError("Unable to read bounded AX children")
        return list(values or []), total > count

    def _target(
        self, element: Any, window: Any, denied: bool, deadline: float
    ) -> tuple[Target, bool, bool]:
        role = self._get(element, "AXRole", deadline)
        subrole = self._get(element, "AXSubrole", deadline)
        if not isinstance(role, str):
            raise MacOSError("AX element is no longer readable")
        secure = (
            "secure" in str(role).casefold()
            or "secure" in str(subrole).casefold()
            or (role in {"AXTextField", "AXTextArea", "AXComboBox"} and subrole is None)
            or bool(self._get(element, "AXProtectedContent", deadline))
        )
        denied = denied or role in self._DIALOGS or subrole in self._DIALOGS
        if secure:
            return (
                Target(
                    element,
                    window,
                    digest([role, subrole, "redacted"]),
                    bounded(role, 80),
                    "[redacted]",
                    (),
                ),
                denied,
                True,
            )
        names = (
            "AXTitle",
            "AXDescription",
            "AXEnabled",
            "AXHidden",
            "AXIdentifier",
            "AXFocused",
            "AXSelected",
            "AXPosition",
            "AXSize",
        )
        raw = {name: self._get(element, name, deadline) for name in names}
        if self._SENSITIVE.search(" ".join(str(raw[name] or "") for name in ("AXTitle", "AXDescription", "AXIdentifier"))):
            return (
                Target(element, window, digest([role, subrole, "redacted"]), bounded(role, 80), "[redacted]", ()),
                denied,
                True,
            )
        value = (
            self._get(element, "AXValue", deadline)
            if role in self._VALUE_ROLES
            else None
        )
        self._budget(deadline)
        actions = self.mac._actions(element)
        self._budget(deadline)
        settable = role in {"AXTextField", "AXTextArea"} and self.mac._settable(
            element, "AXValue"
        )
        label = bounded(raw["AXTitle"] or raw["AXDescription"])
        # Permission approval is not a supported workflow, including app-rendered
        # approval controls outside standard sheets. Fail conservatively.
        approval = label.casefold().startswith(
            ("allow", "approve", "grant", "authorize")
        )
        enabled = raw["AXEnabled"] is not None and bool(raw["AXEnabled"])
        operations = []
        if (
            enabled
            and not raw["AXHidden"]
            and not denied
            and not approval
            and window is not None
        ):
            if "AXPress" in actions and role not in {
                "AXWindow",
                "AXApplication",
                "AXMenuBar",
            }:
                operations.append("press")
            if settable:
                operations.append("setValue")
        # Full scalar state is hashed privately; only bounded, selected strings
        # are published. Never include raw attributes or a private tree in RPC.
        state = digest(
            [
                role,
                subrole,
                {k: self.mac._jsonable(v) for k, v in raw.items()},
                value if isinstance(value, (str, bool, int, float)) else None,
                sorted(actions),
                bool(settable),
                denied,
                operations,
            ]
        )
        return (
            Target(
                element,
                window,
                state,
                bounded(role, 80),
                label,
                tuple(operations),
                bounded(str(value)) if isinstance(value, (str, bool, int)) else None,
            ),
            denied,
            False,
        )

    def read_snapshot(self, app: str, limit: int, deadline: float) -> Snapshot:
        self.mac._ensure_accessibility()  # Non-prompting only.
        process = self._process(app)
        generation = getattr(self.mac, "_guarded_generation", 0)
        # Do NOT use _application_element: it writes AXEnhancedUserInterface.
        root = native.AS.AXUIElementCreateApplication(process[0])
        error = native.AS.AXUIElementSetMessagingTimeout(root, self.AX_TIMEOUT)
        if error:
            raise MacOSError("Cannot bound AX messaging")
        queue = deque([(root, None, False, 0)])
        targets: list[Target] = []
        seen: set[Any] = set()
        truncated = False
        while queue and len(seen) < self.MAX_VISITS and len(targets) < limit:
            element, window, denied, depth = queue.popleft()
            if element in seen:
                continue
            seen.add(element)
            role = self._get(element, "AXRole", deadline)
            if role == "AXWindow":
                window = element
            if role == "AXMenuBar":
                continue
            target, denied, secure = self._target(element, window, denied, deadline)
            targets.append(target)
            if secure:
                continue  # Never visit descendants of secure containers.
            if depth >= self.MAX_DEPTH:
                truncated = True
                continue
            room = self.MAX_VISITS - len(seen) - len(queue)
            if room <= 0:
                truncated = True
                continue
            children, clipped = self._children(element, "AXChildren", room, deadline)
            if not children and depth == 0:
                children, clipped = self._children(element, "AXWindows", room, deadline)
            truncated |= clipped
            queue.extend((child, window, denied, depth + 1) for child in children)
        if process != self._process(app) or generation != getattr(
            self.mac, "_guarded_generation", 0
        ):
            raise MacOSError("Process or raw state changed during observation")
        return Snapshot(process, generation, tuple(targets), truncated or bool(queue))

    def execute(
        self,
        app: str,
        snapshot: Snapshot,
        target: Target,
        operation: str,
        text: str | None,
    ) -> dict:
        # Everything through the last focus check is pre-dispatch: safe to block.
        try:
            self.mac._ensure_accessibility()
            if snapshot.process != self._process(app) or snapshot.generation != getattr(
                self.mac, "_guarded_generation", 0
            ):
                return {
                    "status": "stale",
                    "reason": "Process or raw generation changed",
                }
            pid = snapshot.process[0]
            error, target_pid = native.AS.AXUIElementGetPid(target.native, None)
            if error or target_pid != pid:
                return {"status": "stale", "reason": "Target process changed"}
            deadline = time.monotonic() + 2
            window = self._get(target.native, "AXWindow", deadline)
            if window is None or window != target.window:
                return {"status": "stale", "reason": "Target window changed"}
            current, _, _ = self._target(target.native, window, False, deadline)
            if current != target or operation not in current.operations:
                return {
                    "status": "stale",
                    "reason": "Target state or capabilities changed",
                }
            if snapshot.process[2] in {
                "com.apple.systempreferences",
                "com.apple.SecurityAgent",
            }:
                return {
                    "status": "blocked",
                    "reason": "Permission management is not supported",
                }
            if snapshot.process != self._process(app) or snapshot.generation != getattr(
                self.mac, "_guarded_generation", 0
            ):
                return {
                    "status": "stale",
                    "reason": "Process or raw generation changed",
                }
            # AXPress has no atomic no-activation flag. Check focus last, then
            # dispatch once; a native race can only be reported, not rolled back.
            before = self.mac._frontmost_app()
            if before is None or int(before["pid"]) != pid:
                return {
                    "status": "blocked",
                    "reason": "Background AX effects are not focus-safe",
                }
        except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
            return {"status": "stale", "reason": "Unable to revalidate target"}
        self.mac._invalidate_guarded_observations()
        try:
            if operation == "press":
                error = native.AS.AXUIElementPerformAction(target.native, "AXPress")
            elif operation == "setValue":
                error = native.AS.AXUIElementSetAttributeValue(
                    target.native, "AXValue", text
                )
            else:
                return {"status": "blocked", "reason": "Unsupported operation"}
            self.mac._guard_focus(before, pid, operation)
            after = self.mac._frontmost_app()
            if error or after != before:
                return {
                    "status": "outcome_unknown",
                    "reason": "Native receipt or focus changed; check before deciding",
                }
        except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
            return {
                "status": "outcome_unknown",
                "reason": "Native effect may have happened; do not retry blindly",
            }
        return {"status": "executed"}
