"""Best-effort AX activity journal. Notifications supplement, never replace reads."""

from __future__ import annotations

import threading


class AXChangeJournal:
    NOTIFICATIONS = (
        "AXWindowCreated",
        "AXUIElementDestroyed",
        "AXFocusedWindowChanged",
        "AXMainWindowChanged",
        "AXSheetCreated",
        "AXMenuOpened",
        "AXMenuClosed",
        "AXValueChanged",
        "AXSelectedChildrenChanged",
        "AXTitleChanged",
        "AXLayoutChanged",
        "AXFocusedUIElementChanged",
    )

    def __init__(self, pid: int):
        self._condition = threading.Condition()
        self._sequence = 0
        self._available = False
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._elements: tuple = ()
        self._thread = threading.Thread(
            target=self._run, args=(pid,), daemon=True, name="macos-harness-ax"
        )
        self._thread.start()
        self._ready.wait(0.15)

    @property
    def sequence(self) -> int:
        with self._condition:
            return self._sequence

    @property
    def available(self) -> bool:
        with self._condition:
            return self._available

    def watch(self, elements: tuple) -> None:
        with self._condition:
            self._elements = elements[:128]

    def wait(self, sequence: int, timeout: float) -> None:
        with self._condition:
            # Keep a timed fallback even if the observer is unavailable.
            self._condition.wait_for(lambda: self._sequence != sequence, timeout)

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=0.5)

    def _record(self, *args) -> None:
        with self._condition:
            if self._stop.is_set():
                return
            self._sequence += 1
            self._condition.notify_all()

    def _run(self, pid: int) -> None:
        observer = source = loop = None
        registrations: set = set()
        attempted: set = set()
        try:
            import CoreFoundation as CF
            import objc

            from . import macos as native

            AS = native.AS
            root = AS.AXUIElementCreateApplication(pid)
            @objc.callbackFor(AS.AXObserverCreate)
            def callback(observer, element, notification, refcon):
                self._record()

            error, observer = AS.AXObserverCreate(pid, callback, None)
            if error or observer is None:
                return
            source = AS.AXObserverGetRunLoopSource(observer)
            loop = CF.CFRunLoopGetCurrent()
            CF.CFRunLoopAddSource(loop, source, CF.kCFRunLoopDefaultMode)
            while not self._stop.is_set():
                with self._condition:
                    elements = (root, *self._elements)
                wanted = {
                    (element, name)
                    for element in elements
                    for name in self.NOTIFICATIONS
                }
                for element, name in registrations - wanted:
                    AS.AXObserverRemoveNotification(observer, element, name)
                registrations.intersection_update(wanted)
                attempted.intersection_update(wanted)
                for element, name in wanted - attempted:
                    attempted.add((element, name))
                    if AS.AXObserverAddNotification(observer, element, name, None) == 0:
                        registrations.add((element, name))
                with self._condition:
                    self._available = bool(registrations)
                self._ready.set()
                CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, 0.05, False)
        except Exception:  # noqa: BLE001 - native observer failures fall back to polling
            self._stop.set()
        finally:
            if observer is not None:
                for element, name in registrations:
                    try:
                        AS.AXObserverRemoveNotification(observer, element, name)
                    except Exception:  # noqa: BLE001 - teardown must still release the run-loop source
                        self._stop.set()
            if source is not None and loop is not None:
                CF.CFRunLoopRemoveSource(loop, source, CF.kCFRunLoopDefaultMode)
            with self._condition:
                self._available = False
                self._condition.notify_all()
            self._ready.set()
