"""AX observer lifecycle with fake native APIs; never observes a real app."""

import os
import sys
import threading
from types import SimpleNamespace

import pytest

from macos_harness import macos as native
from macos_harness.ax_events import AXChangeJournal


def test_journal_wakes_and_releases_native_resources(monkeypatch):
    calls = []
    callbacks = []
    root = object()
    sleeper = threading.Event()
    cf = SimpleNamespace(
        kCFRunLoopDefaultMode="default",
        CFRunLoopGetCurrent=lambda: "loop",
        CFRunLoopAddSource=lambda *args: calls.append("add_source"),
        CFRunLoopRemoveSource=lambda *args: calls.append("remove_source"),
        CFRunLoopRunInMode=lambda *args: sleeper.wait(0.005),
    )

    def create(pid, callback, out):
        callbacks.append(callback)
        return 0, "observer"

    shim = SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: root,
        AXObserverCreate=create,
        AXObserverGetRunLoopSource=lambda observer: "source",
        AXObserverAddNotification=lambda *args: calls.append("add_notification") or 0,
        AXObserverRemoveNotification=lambda *args: (
            calls.append("remove_notification") or 0
        ),
    )
    monkeypatch.setattr(native, "AS", shim)
    monkeypatch.setitem(sys.modules, "CoreFoundation", cf)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(callbackFor=lambda function: lambda callback: callback))
    journal = AXChangeJournal(42)
    try:
        assert journal.available and journal.sequence == 0
        callbacks[0](None, root, "AXSheetCreated", None)
        journal.wait(0, 0.1)
        assert journal.sequence == 1
        assert calls.count("add_notification") == len(journal.NOTIFICATIONS)
    finally:
        journal.close()
    assert not journal._thread.is_alive() and not journal.available
    assert calls.count("remove_notification") == calls.count("add_notification")
    assert calls[-1] == "remove_source"


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("MACOS_HARNESS_TEST_AX") != "1", reason="explicit observer creation probe for this process only")
def test_native_callback_registration_for_own_process():
    import objc

    @objc.callbackFor(native.AS.AXObserverCreate)
    def callback(observer, element, notification, refcon):
        return None

    error, observer = native.AS.AXObserverCreate(os.getpid(), callback, None)
    assert error == 0 and observer is not None


def test_unavailable_observer_is_polling_not_activity(monkeypatch):
    monkeypatch.setattr(
        native,
        "AS",
        SimpleNamespace(
            AXUIElementCreateApplication=lambda pid: object(),
            AXObserverCreate=lambda *args: (1, None),
        ),
    )
    monkeypatch.setitem(sys.modules, "CoreFoundation", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(callbackFor=lambda function: lambda callback: callback))
    journal = AXChangeJournal(42)
    try:
        assert not journal.available and journal.sequence == 0
        journal.wait(0, 0.001)
    finally:
        journal.close()
    assert not journal._thread.is_alive()
