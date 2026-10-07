"""Capability contract tests: fake native backends only, no desktop access."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_guarded import action

pytest_plugins = ("test_guarded",)  # Reuse the strictly offline AX/controller fixtures.

from macos_harness import GuardedController, GuardedError, NativeHybridBackend
from macos_harness.guarded import ObservationTimeout
from macos_harness.ocr import OCRCapture, OCRText
from macos_harness.rpc import dispatch


@pytest.fixture
def clock(monkeypatch):
    from macos_harness import guarded

    now = [0.0]
    monkeypatch.setattr(guarded.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        guarded.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    return now


def test_settle_no_reaction_is_not_quiet_success(controller, clock):
    obs = controller.observe({"app": "Test"})
    result = controller.settle(obs["scope"], obs["revision"])
    assert not result["reacted"] and not result["settled"] and not result["timedOut"]
    assert clock[0] == pytest.approx(0.6)
    assert result["observation"]["observationId"] != obs["observationId"]
    assert controller.act(**action(obs))["status"] == "stale"


def test_settle_reaction_and_quiet_with_event_wakeup(controller, clock):
    obs = controller.observe({"app": "Test"}, maxElements=5)
    waits = []

    def wait(app, sequence, timeout):
        waits.append((app, sequence))
        clock[0] += timeout
        if sequence == 0:
            controller.backend.snapshot = replace(
                controller.backend.snapshot, event_sequence=1
            )

    controller.backend.wait_for_change = wait
    result = controller.settle(obs["scope"], obs["revision"])
    assert result["reacted"] and result["settled"] and not result["timedOut"]
    assert waits[0] == ("Test", 0) and waits[-1] == ("Test", 1)
    assert clock[0] == pytest.approx(0.25)
    assert ("Test", 5) in controller.backend.calls


def test_settle_deadline_never_means_ready(controller, clock):
    obs = controller.observe({"app": "Test"})
    original = controller.backend.read_snapshot

    def changing(*args):
        controller.backend.snapshot = replace(
            controller.backend.snapshot, generation=int(clock[0] * 1000) + 1
        )
        return original(*args)

    controller.backend.read_snapshot = changing
    result = controller.settle(obs["scope"], obs["revision"], timeoutMs=500)
    assert result["reacted"] and result["timedOut"] and not result["settled"]
    assert clock[0] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "kwargs", [{"quietMs": True}, {"reactionMs": -1}, {"timeoutMs": 60001}]
)
def test_settle_strict_arguments(controller, kwargs):
    with pytest.raises(GuardedError):
        controller.settle({"app": "Test"}, "revision", **kwargs)


def test_settle_short_deadline_does_not_publish_cached_evidence(ax):
    obs = ax.controller.observe({"app": "Test"})
    with pytest.raises(ObservationTimeout):
        ax.controller.settle(obs["scope"], obs["revision"], timeoutMs=1)
    assert ax.controller.act(**action(obs))["status"] == "stale"


def test_scoped_app_and_rpc_settle_contract(controller):
    app = controller.app("Test")
    obs = app.observe()
    assert app.act(obs["observationId"], action(obs)["action"])["status"] == "executed"
    result = dispatch(
        controller,
        {
            "id": 1,
            "method": "settle",
            "args": {
                "scope": obs["scope"],
                "revision": obs["revision"],
                "timeoutMs": 0,
                "reactionMs": 0,
            },
        },
    )
    assert set(result) == {"reacted", "settled", "timedOut", "observation"}
    assert app.waitForChange(result["observation"]["revision"], 0)["changed"] is False
    assert app.settle(result["observation"]["revision"], 0, 0)["reacted"] is False
    with pytest.raises(GuardedError):
        controller.app("Forbidden")
    controller.close()
    with pytest.raises(GuardedError, match="closed"):
        app.observe()


@pytest.mark.parametrize(
    "operation,native_action",
    [
        ("click", "AXPress"),
        ("press", "AXPress"),
        ("increment", "AXIncrement"),
        ("decrement", "AXDecrement"),
        ("scroll_down", "AXScrollDownByPage"),
        ("showMenu", "AXShowMenu"),
    ],
)
def test_native_capabilities_dispatch_once(ax, operation, native_action):
    ax.mac._actions = lambda element: [native_action] if element == ax.button else []
    obs = ax.controller.observe({"app": "Test"})
    assert operation in obs["candidates"][-1]["operations"]
    assert ax.controller.act(**action(obs, operation))["status"] == "executed"
    assert ax.calls == [(ax.button, native_action)]
    assert ax.controller.act(**action(obs, operation))["status"] == "stale"


def test_events_invalidate_handles_even_when_snapshot_values_revert(ax):
    obs = ax.controller.observe({"app": "Test"})
    journal = SimpleNamespace(
        sequence=1, available=True, watch=lambda elements: None, close=lambda: None
    )
    ax.controller.backend._journal = journal
    assert ax.controller.act(**action(obs))["status"] == "stale"
    assert not ax.calls
    fresh = ax.controller.observe({"app": "Test"})
    assert fresh["changeSource"] == "ax_notifications"
    assert fresh["revision"] != obs["revision"]


@pytest.fixture
def hybrid(ax):
    ax.children[ax.window] = []
    ax.data[ax.window].update(
        AXPosition={"x": 10, "y": 20}, AXSize={"width": 400, "height": 300}
    )
    ax.mac.windows = lambda app: [
        {
            "window_id": 77,
            "on_screen": True,
            "bounds": {"x": 10, "y": 20, "width": 400, "height": 300},
        }
    ]
    ax.mac._ensure_post_events = lambda: None
    clicks = []
    ax.mac.click = lambda *args, **kwargs: clicks.append((args, kwargs))

    class OCR:
        fingerprint = "raster-1"
        regions = (OCRText("Play", (0.2, 0.3, 0.1, 0.1), 0.99),)
        captures = 0

        def capture(self, mac, pid, window_id, bounds, deadline):
            self.captures += 1
            return OCRCapture(b"png", window_id, bounds, self.fingerprint)

        def read(self, capture, deadline):
            return self.regions, False

    ocr = OCR()
    backend = NativeHybridBackend(ax.mac, provider=ocr)
    control = GuardedController(backend, ["Test"])
    return SimpleNamespace(
        ax=ax, ocr=ocr, backend=backend, control=control, clicks=clicks
    )


def test_disabled_hybrid_never_starts_a_worker(ax, monkeypatch):
    from macos_harness import ocr

    def forbidden(*args, **kwargs):
        pytest.fail("AX-only must not start OCR")

    monkeypatch.setattr(ocr.subprocess, "Popen", forbidden)
    with GuardedController(
        NativeHybridBackend(ax.mac, ocr="never"), ["Test"]
    ) as control:
        assert control.observe({"app": "Test"})["ocrStatus"] == "disabled"


def test_ocr_provenance_geometry_and_fresh_click(hybrid):
    obs = hybrid.control.observe({"app": "Test"})
    target = obs["candidates"][-1]
    assert obs["ocrStatus"] == "used"
    assert target["source"] == "ocr" and target["operations"] == ["click"]
    assert target["bounds"] == {"x": 90, "y": 110, "width": 40, "height": 30}
    assert hybrid.control.act(**action(obs, "click"))["status"] == "executed"
    assert hybrid.clicks == [((110, 125), {"app": "42", "coordinate_space": "screen"})]
    assert hybrid.control.act(**action(obs, "click"))["status"] == "stale"


def test_ocr_same_text_on_changed_pixels_is_stale(hybrid):
    obs = hybrid.control.observe({"app": "Test"})
    hybrid.ocr.fingerprint = "overlay-arrived"
    assert hybrid.control.act(**action(obs, "click"))["status"] == "stale"
    assert not hybrid.clicks


def test_ocr_does_not_approve_background_input(hybrid):
    obs = hybrid.control.observe({"app": "Test"})
    hybrid.ax.runtime.focus = {"pid": 99}
    assert hybrid.control.act(**action(obs, "click"))["status"] == "blocked"
    assert not hybrid.clicks


@pytest.mark.parametrize(
    "role,subrole",
    [("AXSheet", None), ("AXTextField", "AXSecureTextField"), ("AXTextField", None)],
)
def test_ocr_never_reads_through_protected_or_dialog_state(hybrid, role, subrole):
    ax = hybrid.ax
    ax.children[ax.window] = [ax.button]
    ax.data[ax.button].update(AXRole=role, AXSubrole=subrole)
    hybrid.backend.ocr_mode = "always"
    obs = hybrid.control.observe({"app": "Test"})
    assert obs["ocrStatus"] == "blocked" and hybrid.ocr.captures == 0


@pytest.mark.parametrize("attribute,value", [("AXEnabled", False), ("AXHidden", True)])
def test_ocr_cannot_bypass_a_disabled_or_hidden_container(hybrid, attribute, value):
    hybrid.ax.data[hybrid.ax.window][attribute] = value
    assert hybrid.control.observe({"app": "Test"})["ocrStatus"] == "blocked"
    assert hybrid.ocr.captures == 0


def test_ocr_truncation_cannot_hide_a_denial(hybrid):
    obs = hybrid.control.observe({"app": "Test"}, maxElements=1)
    assert obs["ocrStatus"] == "blocked" and hybrid.ocr.captures == 0


def test_title_bar_buttons_do_not_suppress_auto_ocr(hybrid):
    ax = hybrid.ax
    ax.children[ax.window] = [ax.button]
    ax.data[ax.button].update(AXSubrole="AXCloseButton", AXTitle="Close", AXPosition={"x": 10, "y": 20}, AXSize={"width": 14, "height": 14})
    assert hybrid.control.observe({"app": "Test"})["ocrStatus"] == "used"
    assert hybrid.ocr.captures == 1


def test_ocr_auto_avoids_capture_when_ax_is_usable(hybrid):
    ax = hybrid.ax
    ax.children[ax.window] = [ax.button]
    assert hybrid.control.observe({"app": "Test"})["ocrStatus"] == "not_needed"
    assert hybrid.ocr.captures == 0


def test_ocr_never_clicks_over_disabled_native_controls(hybrid):
    ax = hybrid.ax
    ax.children[ax.window] = [ax.button]
    ax.data[ax.button].update(
        AXEnabled=False,
        AXPosition={"x": 90, "y": 110},
        AXSize={"width": 40, "height": 30},
    )
    hybrid.backend.ocr_mode = "always"
    obs = hybrid.control.observe({"app": "Test"})
    assert all(c["source"] == "ax" for c in obs["candidates"])


def test_ocr_low_confidence_and_approval_text_are_not_actionable(hybrid):
    hybrid.ocr.regions = (
        OCRText("Play", (0.1, 0.1, 0.1, 0.1), 0.5),
        OCRText("Allow access", (0.5, 0.5, 0.1, 0.1), 1),
    )
    obs = hybrid.control.observe({"app": "Test"})
    assert all(c["operations"] == [] for c in obs["candidates"] if c["source"] == "ocr")


def test_ocr_mutation_during_capture_is_not_published(hybrid):
    original = hybrid.ocr.read

    def race(*args):
        hybrid.ax.runtime.launch = 2
        return original(*args)

    hybrid.ocr.read = race
    with pytest.raises(ObservationTimeout):
        hybrid.control.observe({"app": "Test"})


def test_ocr_uncertain_click_is_never_replayed(hybrid):
    def fail(*args, **kwargs):
        hybrid.clicks.append("sent")
        raise RuntimeError("private native error")

    hybrid.ax.mac.click = fail
    obs = hybrid.control.observe({"app": "Test"})
    receipt = hybrid.control.act(**action(obs, "click"))
    assert receipt["status"] == "outcome_unknown" and "private" not in str(receipt)
    assert hybrid.control.act(**action(obs, "click"))["status"] == "stale"
    assert hybrid.clicks == ["sent"]
