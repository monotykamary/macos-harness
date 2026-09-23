"""Entirely offline: never construct MacOS or call a real native API."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from macos_harness import GuardedController, GuardedError, MacOS, NativeAXBackend
from macos_harness import macos as native
from macos_harness.guarded import Snapshot, Target
from macos_harness.rpc import MAX_LINE_BYTES, dispatch, serve


class Backend:
    def __init__(self):
        self.target = Target(
            object(), object(), "state", "AXButton", "Save", ("press", "setValue")
        )
        self.snapshot = Snapshot((42, 1.0, "test", "/test"), 0, (self.target,))
        self.calls = []
        self.receipt = {"status": "executed"}

    def read_snapshot(self, app, limit, deadline):
        self.calls.append((app, limit))
        return self.snapshot

    def execute(self, *args):
        self.calls.append("execute")
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt


@pytest.fixture
def controller():
    return GuardedController(Backend(), ["Test", "Other"])


def action(observation, operation="press", **extra):
    return {
        "scope": observation["scope"],
        "observationId": observation["observationId"],
        "action": dict(
            targetId=observation["candidates"][-1]["id"], operation=operation, **extra
        ),
    }


def test_handles_replay_and_persistent_state(controller):
    first = controller.observe({"app": "Test"})
    assert controller.backend.calls == [("Test", 64)]
    assert controller.act(**action(first))["status"] == "executed"
    assert controller.act(**action(first))["status"] == "stale"
    assert controller.backend.calls.count("execute") == 1
    assert (
        controller.observe({"app": "Test"})["observationId"] != first["observationId"]
    )


def test_scope_isolation_and_global_invalidation(controller):
    first = controller.observe({"app": "Test"})
    wrong = action(first)
    wrong["scope"] = {"app": "Other"}
    assert controller.act(**wrong)["status"] == "stale"
    controller.observe({"app": "Other"})
    assert controller.act(**action(first))["status"] == "stale"
    with pytest.raises(GuardedError, match="allowlist"):
        controller.observe({"app": "Tes"})
    with pytest.raises(GuardedError):
        controller.observe({"app": "Test", "pid": 42})


@pytest.mark.parametrize(
    "change", ["process", "generation", "native", "window", "state", "operations"]
)
def test_stale_identity_and_state(controller, change):
    obs = controller.observe({"app": "Test"})
    backend = controller.backend
    if change == "process":
        backend.snapshot = replace(backend.snapshot, process=(42, 2.0, "test", "/test"))
    elif change == "generation":
        backend.snapshot = replace(backend.snapshot, generation=1)
    else:
        value = {
            "native": object(),
            "window": object(),
            "state": "new",
            "operations": (),
        }[change]
        backend.snapshot = replace(
            backend.snapshot, targets=(replace(backend.target, **{change: value}),)
        )
    assert controller.act(**action(obs))["status"] == "stale"
    assert "execute" not in backend.calls


def test_unknown_receipt_never_retries(controller):
    obs = controller.observe({"app": "Test"})
    controller.backend.receipt = RuntimeError("private native error")
    assert controller.act(**action(obs))["status"] == "outcome_unknown"
    assert controller.act(**action(obs))["status"] == "stale"
    assert controller.backend.calls.count("execute") == 1


def test_operation_and_text_validation(controller):
    obs = controller.observe({"app": "Test"})
    assert controller.act(**action(obs, "raise"))["status"] == "blocked"
    obs = controller.observe({"app": "Test"})
    for text in (None, "x" * 4097):
        with pytest.raises(GuardedError):
            controller.act(**action(obs, "setValue", text=text))
    assert controller.act(**action(obs, "setValue", text=""))["status"] == "executed"
    for limit in (0, 129, True, 1.5):
        with pytest.raises(GuardedError):
            controller.observe({"app": "Test"}, maxElements=limit)


def test_wait_revision_not_handle_changes(controller):
    obs = controller.observe({"app": "Test"}, maxElements=5)
    same = controller.waitForChange(obs["scope"], obs["revision"], 0)
    assert not same["changed"]
    assert same["observation"]["observationId"] != obs["observationId"]
    assert controller.act(**action(obs))["status"] == "stale"
    controller.backend.snapshot = replace(controller.backend.snapshot, generation=2)
    assert controller.waitForChange(obs["scope"], obs["revision"], 0)["changed"]
    with pytest.raises(GuardedError):
        controller.waitForChange(obs["scope"], obs["revision"], 60001)


def frame(method="observe", args=None, id=1, **extra):
    return {
        "id": id,
        "method": method,
        "args": args or {"scope": {"app": "Test"}},
        **extra,
    }


def rpc(controller, data):
    output, diagnostics = io.BytesIO(), io.StringIO()
    serve(controller, io.BytesIO(data), output, diagnostics)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_rpc_malformed_oversized_and_recovery(controller):
    data = (
        b"{broken}\n"
        + b"x" * MAX_LINE_BYTES
        + b"\n"
        + json.dumps(frame()).encode()
        + b"\n"
    )
    results = rpc(controller, data)
    assert [r.get("error", {}).get("code") for r in results] == [
        "invalid_json",
        "line_too_large",
        None,
    ]
    assert results[2]["result"]["candidates"]
    assert len(controller.backend.calls) == 1


@pytest.mark.parametrize(
    "message,code",
    [
        (frame(method="eval"), "unknown_method"),
        (frame(method=[]), "unknown_method"),
        (frame(extra="no"), "invalid_args"),
        (frame(args={"scope": {"app": "Test"}, "extra": 1}), "invalid_args"),
        (frame(id=True), "invalid_args"),
        (frame(id=2**53), "invalid_args"),
        ([], "invalid_args"),
        (frame(args={"scope": {"app": "Secret"}}), "scope_denied"),
    ],
)
def test_rpc_rejects_unknown_and_invalid(controller, message, code):
    result = rpc(controller, json.dumps(message).encode() + b"\n")[0]
    assert result["error"]["code"] == code
    assert not controller.backend.calls


@pytest.mark.parametrize(
    "data", [b'{"id":1,"id":2}\n', b'{"id":NaN}\n', b"\xff\n", b"[]"]
)
def test_rpc_strict_json_and_partial_eof(controller, data):
    assert "error" in rpc(controller, data)[0]
    assert not controller.backend.calls


def test_rpc_persistent_handles_and_stdout_diagnostics(controller):
    obs = dispatch(controller, frame())["observationId"]
    assert controller._current[1] == obs
    original = controller.backend.read_snapshot

    def noisy(*args):
        print("private backend diagnostic")
        return original(*args)

    controller.backend.read_snapshot = noisy
    result = rpc(controller, json.dumps(frame()).encode() + b"\n")[0]
    assert result["id"] == 1 and "result" in result


@pytest.fixture
def ax(monkeypatch):
    """Only these fake native calls exist; no live native path is available."""
    mac = MacOS.__new__(MacOS)
    mac._elements = {}
    mac._guarded_generation = 0
    root, window, button = object(), object(), object()
    data = {
        root: {"AXRole": "AXApplication"},
        window: {"AXRole": "AXWindow", "AXTitle": "Window"},
        button: {
            "AXRole": "AXButton",
            "AXTitle": "Save",
            "AXEnabled": True,
            "AXWindow": window,
        },
    }
    children = {root: [window], window: [button]}
    calls = []
    runtime = SimpleNamespace(launch=1, focus={"pid": 42, "name": "Test"}, error=0)
    running = SimpleNamespace(
        launchDate=lambda: SimpleNamespace(
            timeIntervalSince1970=lambda: runtime.launch
        ),
        isTerminated=lambda: False,
    )
    mac._resolve_app = lambda app: (
        running,
        {"name": "Test", "pid": 42, "bundle_id": "test", "path": "/test"},
    )
    mac._ensure_accessibility = lambda: None
    mac._frontmost_app = lambda: runtime.focus.copy()
    mac._copy_attribute = lambda e, name: data[e].get(name)
    mac._actions = lambda e: ["AXPress"] if e == button else []
    mac._settable = lambda e, name: True
    mac._jsonable = lambda value: value

    def effect(*args):
        calls.append(args)
        return runtime.error

    shim = SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: root,
        AXUIElementCopyAttributeValue=lambda element, name, _: (
            0,
            mac._copy_attribute(element, name),
        ),
        AXUIElementSetMessagingTimeout=lambda *args: 0,
        AXUIElementGetAttributeValueCount=lambda e, name, _: (
            0,
            len(children.get(e, [])) if name == "AXChildren" else 0,
        ),
        AXUIElementCopyAttributeValues=lambda e, name, start, count, _: (
            0,
            children.get(e, [])[start : start + count],
        ),
        AXUIElementGetPid=lambda *args: (0, 42),
        AXUIElementPerformAction=effect,
        AXUIElementSetAttributeValue=effect,
    )
    monkeypatch.setattr(native, "AS", shim)
    return SimpleNamespace(
        mac=mac,
        data=data,
        children=children,
        button=button,
        window=window,
        root=root,
        calls=calls,
        runtime=runtime,
        shim=shim,
        controller=GuardedController(NativeAXBackend(mac), ["Test", "Tes"]),
    )


def test_native_pin_and_raw_index_reset(ax):
    obs = ax.controller.observe({"app": "Test"})
    ax.mac._elements[0] = object()  # New path never trusts this reusable raw index.
    assert ax.controller.act(**action(obs))["status"] == "executed"
    assert ax.calls == [(ax.button, "AXPress")]
    obs = ax.controller.observe({"app": "Test"})
    ax.mac._snapshot_tree(ax.root, max_depth=0, max_nodes=0, include_menu_bar=False)
    assert ax.mac._elements == {}
    assert ax.controller.act(**action(obs))["status"] == "stale"


def test_native_restart_exact_allowlist_and_no_focus_steal(ax):
    with pytest.raises(Exception, match="exactly"):
        ax.controller.observe({"app": "Tes"})
    obs = ax.controller.observe({"app": "Test"})
    ax.runtime.launch = 2
    assert ax.controller.act(**action(obs))["status"] == "stale"
    obs = ax.controller.observe({"app": "Test"})
    ax.runtime.focus = {"pid": 99, "name": "Other"}
    assert ax.controller.act(**action(obs))["status"] == "blocked"
    assert not ax.calls


@pytest.mark.parametrize(
    "attribute,value",
    [("AXSubrole", "AXSecureTextField"), ("AXProtectedContent", True)],
)
def test_secure_redaction_never_reads_text(ax, attribute, value):
    ax.data[ax.button].update(
        {
            "AXRole": "AXTextField",
            attribute: value,
            "AXValue": "SECRET",
            "AXTitle": "SECRET",
        }
    )
    getter = ax.mac._copy_attribute

    def get(element, name):
        assert not (
            element == ax.button and name in {"AXValue", "AXTitle", "AXDescription"}
        )
        return getter(element, name)

    ax.mac._copy_attribute = get
    obs = ax.controller.observe({"app": "Test"})
    assert "SECRET" not in json.dumps(obs)
    assert obs["candidates"][-1]["label"] == "[redacted]"
    assert obs["candidates"][-1]["operations"] == []


def test_dialog_and_permission_controls_not_actionable(ax):
    ax.data[ax.window]["AXSubrole"] = "AXDialog"
    obs = ax.controller.observe({"app": "Test"})
    assert obs["candidates"][-1]["operations"] == []
    del ax.data[ax.window]["AXSubrole"]
    ax.data[ax.button]["AXTitle"] = "Allow access"
    assert ax.controller.observe({"app": "Test"})["candidates"][-1]["operations"] == []


def test_native_checkbox_value_is_observable_and_part_of_freshness(ax):
    ax.data[ax.button].update(AXRole="AXCheckBox", AXValue=0)
    obs = ax.controller.observe({"app": "Test"})
    assert obs["candidates"][-1]["value"] == "0"
    ax.data[ax.button]["AXValue"] = 1
    assert ax.controller.act(**action(obs))["status"] == "stale"
    assert ax.controller.observe({"app": "Test"})["candidates"][-1]["value"] == "1"
    assert not ax.calls


def test_native_sensitive_label_is_redacted_before_value_read(ax):
    ax.data[ax.button].update(AXRole="AXTextField", AXSubrole="AXStandardTextField", AXTitle="API key", AXValue="PRIVATE")
    getter = ax.mac._copy_attribute

    def get(element, name):
        assert not (element == ax.button and name == "AXValue")
        return getter(element, name)

    ax.mac._copy_attribute = get
    obs = ax.controller.observe({"app": "Test"})
    assert "PRIVATE" not in json.dumps(obs)
    assert obs["candidates"][-1]["operations"] == []
    assert obs["candidates"][-1]["label"] == "[redacted]"


def test_native_set_value_and_uncertain_receipt(ax):
    ax.data[ax.button].update(
        AXRole="AXTextField", AXSubrole="AXStandardTextField", AXValue="old"
    )
    obs = ax.controller.observe({"app": "Test"})
    assert "setValue" in obs["candidates"][-1]["operations"]
    ax.runtime.error = -25204
    assert (
        ax.controller.act(**action(obs, "setValue", text="new"))["status"]
        == "outcome_unknown"
    )
    assert ax.calls == [(ax.button, "AXValue", "new")]
    assert ax.controller.act(**action(obs, "setValue", text="new"))["status"] == "stale"


def test_native_last_moment_revalidation(ax):
    obs = ax.controller.observe({"app": "Test"})
    backend = ax.controller.backend
    read = backend.read_snapshot

    def race(*args):
        snapshot = read(*args)
        ax.data[ax.button]["AXEnabled"] = False
        return snapshot

    backend.read_snapshot = race
    assert ax.controller.act(**action(obs))["status"] == "stale"
    assert not ax.calls


def test_native_focus_change_is_unknown_not_retry(ax):
    obs = ax.controller.observe({"app": "Test"})

    def effect(*args):
        ax.calls.append(args)
        ax.runtime.focus = {"pid": 99, "name": "Other"}
        return 0

    ax.shim.AXUIElementPerformAction = effect
    assert ax.controller.act(**action(obs))["status"] == "outcome_unknown"
    assert len(ax.calls) == 1


def test_observation_bounds_and_raw_mutation_invalidation(ax):
    ax.data[ax.button]["AXTitle"] = "long " * 10000
    obs = ax.controller.observe({"app": "Test"}, maxElements=2)
    assert len(obs["candidates"]) == 2 and obs["truncated"]
    obs = ax.controller.observe({"app": "Test"})
    assert len(obs["candidates"][-1]["label"].encode()) <= 160
    ax.mac._elements[0] = ax.button
    ax.mac.set(0, "raw")
    assert ax.controller.act(**action(obs))["status"] == "stale"


def test_cli_registration_without_starting_native(monkeypatch):
    from macos_harness import cli
    from macos_harness import rpc as rpc_module

    calls = []
    monkeypatch.setattr(rpc_module, "serve_cli", lambda apps: calls.append(apps) or 0)
    assert cli.main(["serve", "--app", "Test", "--app", "Other"]) == 0
    assert calls == [["Test", "Other"]]
    with pytest.raises(SystemExit):
        cli.main(["serve"])


def test_real_cli_process_is_persistent_framed_and_exits_on_eof():
    import select
    import subprocess
    import sys
    from pathlib import Path

    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(Path(__file__).parent / "fixtures/guarded_rpc_server.py"),
            "serve",
            "--app",
            "Test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def send(message):
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], "missing RPC response"
        return json.loads(process.stdout.readline())

    try:
        obs = send(frame())["result"]
        receipt = send(frame("act", action(obs), id=2))
        assert receipt == {"id": 2, "result": {"status": "executed"}}
        assert send(frame("act", action(obs), id=3))["result"]["status"] == "stale"
        check = send(
            frame(
                "waitForChange",
                {"scope": obs["scope"], "revision": obs["revision"], "timeoutMs": 0},
                id=4,
            )
        )["result"]
        assert check["changed"]
        assert check["observation"]["revision"] != obs["revision"]
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stdout.read() == b""
        diagnostics = process.stderr.read()
        assert diagnostics.count(b"offline MacOS constructed once") == 1
        assert diagnostics.count(b"offline effect") == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


def test_wait_deadline_and_event_polling(controller, monkeypatch):
    from macos_harness import guarded

    now = [0.0]
    monkeypatch.setattr(guarded.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        guarded.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    obs = controller.observe({"app": "Test"})
    assert not controller.waitForChange(obs["scope"], obs["revision"], 60000)["changed"]
    assert 60 <= now[0] < 60.01
    original = controller.backend.read_snapshot

    def changing(*args):
        if now[0] > 60.2:
            controller.backend.snapshot = replace(
                controller.backend.snapshot, generation=1
            )
        return original(*args)

    controller.backend.read_snapshot = changing
    assert controller.waitForChange(obs["scope"], obs["revision"], 1000)["changed"]
    assert now[0] < 61


def test_wait_short_native_deadline_does_not_relabel_cached_state(ax):
    from macos_harness.guarded import ObservationTimeout

    obs = ax.controller.observe({"app": "Test"})
    with pytest.raises(ObservationTimeout):
        ax.controller.waitForChange(obs["scope"], obs["revision"], 1)
    assert ax.controller.act(**action(obs))["status"] == "stale"


def test_worst_case_output_fits_frame(controller):
    target = replace(
        controller.backend.target, role='"' * 80, label='"' * 160, value="\\" * 160
    )
    controller.backend.snapshot = replace(
        controller.backend.snapshot, targets=(target,) * 128
    )
    request = frame(args={"scope": {"app": "Test"}, "maxElements": 128})
    result = rpc(controller, json.dumps(request).encode() + b"\n")[0]
    assert "result" in result
    assert len(result["result"]["candidates"]) == 128


def test_exact_input_frame_boundary(controller):
    message = json.dumps(frame()).encode()
    accepted = message + b" " * (MAX_LINE_BYTES - len(message) - 1) + b"\n"
    assert "result" in rpc(controller, accepted)[0]
    assert (
        rpc(controller, accepted[:-1] + b" \n")[0]["error"]["code"] == "line_too_large"
    )


def test_invalid_unicode_is_argument_error(controller):
    message = frame(args={"scope": {"app": "\ud800"}})
    assert (
        rpc(controller, json.dumps(message).encode() + b"\n")[0]["error"]["code"]
        == "invalid_args"
    )


def test_permission_check_failure_never_prompts_or_leaks(ax):
    def denied():
        raise RuntimeError("PRIVATE permission details")

    ax.mac._ensure_accessibility = denied
    result = rpc(ax.controller, json.dumps(frame()).encode() + b"\n")[0]
    assert result["error"]["code"] == "backend_error"
    assert "PRIVATE" not in json.dumps(result)
    assert not ax.calls


@pytest.mark.parametrize("attribute", ["AXSubrole", "AXProtectedContent"])
def test_security_metadata_read_failure_never_exposes_text(ax, attribute):
    ax.data[ax.button].update(
        AXRole="AXTextField",
        AXSubrole="AXStandardTextField",
        AXTitle="SECRET",
        AXValue="SECRET",
    )
    original = ax.shim.AXUIElementCopyAttributeValue

    def unreadable(element, name, out):
        if element == ax.button and name == attribute:
            return -25204, None  # CannotComplete is not an absent attribute.
        if element == ax.button and name in {"AXTitle", "AXValue"}:
            pytest.fail("security metadata must succeed before text is read")
        return original(element, name, out)

    ax.shim.AXUIElementCopyAttributeValue = unreadable
    result = rpc(ax.controller, json.dumps(frame()).encode() + b"\n")[0]
    assert result["error"]["code"] == "backend_error"
    assert "SECRET" not in json.dumps(result)
    assert not ax.calls


def test_child_count_failure_is_not_an_empty_snapshot(ax):
    ax.shim.AXUIElementGetAttributeValueCount = lambda *args: (-25204, None)
    result = rpc(ax.controller, json.dumps(frame()).encode() + b"\n")[0]
    assert result["error"]["code"] == "backend_error"
    assert not ax.calls


@pytest.mark.parametrize("error", [-25205, -25212])
def test_optional_native_attributes_can_be_absent(ax, error):
    original = ax.shim.AXUIElementCopyAttributeValue

    def optional(element, name, out):
        if name in {"AXSubrole", "AXProtectedContent"}:
            return error, None
        return original(element, name, out)

    ax.shim.AXUIElementCopyAttributeValue = optional
    obs = ax.controller.observe({"app": "Test"})
    assert obs["candidates"][-1]["label"] == "Save"
    assert ax.controller.act(**action(obs))["status"] == "executed"


def test_missing_text_subrole_is_redacted(ax):
    ax.data[ax.button].update(AXRole="AXTextField", AXValue="SECRET")
    obs = ax.controller.observe({"app": "Test"})
    assert obs["candidates"][-1]["label"] == "[redacted]"
    assert "SECRET" not in json.dumps(obs)


def test_native_exception_after_dispatch_is_unknown(ax):
    def effect(*args):
        ax.calls.append(args)
        raise RuntimeError("native disconnected after applying")

    ax.shim.AXUIElementPerformAction = effect
    obs = ax.controller.observe({"app": "Test"})
    assert ax.controller.act(**action(obs))["status"] == "outcome_unknown"
    assert len(ax.calls) == 1


@pytest.mark.parametrize(
    "name,args,kwargs",
    [
        ("set_value", (0, "text"), {}),
        ("perform_action", (0,), {}),
        ("key", ("return",), {}),
        ("type", ("text",), {}),
        ("click", (1, 2), {}),
        ("drag", (1, 2, 3, 4), {}),
        ("scroll", (1,), {}),
        ("script", ("script",), {}),
        ("ax_search", (), {}),
    ],
)
def test_all_raw_mutations_invalidate_even_on_failure(
    ax, name, args, kwargs, monkeypatch
):
    # Abort at the first underlying operation so this test is strictly offline.
    def fail(*args, **kwargs):
        raise RuntimeError("offline stop")

    ax.mac._ensure_accessibility = fail
    ax.mac._element = fail
    monkeypatch.setattr(native.subprocess, "run", fail)
    previous = ax.mac._guarded_generation
    with pytest.raises(RuntimeError):
        getattr(ax.mac, name)(*args, **kwargs)
    assert ax.mac._guarded_generation == previous + 1
