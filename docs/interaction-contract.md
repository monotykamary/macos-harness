# Guarded interaction contract

The browser and macOS harnesses share interaction ergonomics, not implementations
or identical authority. No model, API key, Jev dependency, inference, action replay,
or automatic raw fallback is part of this layer.

| Method | Input | Result |
| --- | --- | --- |
| `observe` | `scope`, optional `maxElements` | `scope`, `observationId`, `revision`, `candidates`, `truncated` |
| `act` | `scope`, `observationId`, `action` | `status`, optional `reason` |
| `waitForChange` | `scope`, `revision`, optional `timeoutMs` | `changed`, `observation` |
| `settle` | `scope`, `revision`, optional `timeoutMs`, `reactionMs`, `quietMs` | `reacted`, `settled`, `timedOut`, `observation` |

Browser scope identifies an authorized attached page; macOS scope is an exactly
allowlisted app. Each candidate advertises its supported `operations`. IDs are
opaque, observation-bound authority, not indices/selectors for later reuse.
On macOS every observation replaces **all** old handles, including other apps.
A valid act attempt consumes its observation even when revalidation refuses it.

`executed` means native dispatch succeeded, not that the goal was achieved.
`stale` requires observing again; `blocked` means stop, not raw fallback;
`outcome_unknown` means inspect the actual state, never blindly replay.
Revalidation and native effects are not atomic. No app activation, raising,
physical cursor movement, focus restoration, or permission approval is added.

## Python app scope

```python
from macos_harness import GuardedController, NativeAXBackend

with GuardedController(NativeAXBackend(mac), ["com.apple.TextEdit"]) as control:
    app = control.app("com.apple.TextEdit")
    observed = app.observe()
    # Choose an unambiguous, authorized candidate from observed["candidates"].
    # receipt = app.act(observed["observationId"], {
    #     "targetId": candidate["id"], "operation": "click",
    # })
    checked = app.waitForChange(observed["revision"], timeoutMs=0)
```

`GuardedApp` binds an allowlist scope, not the currently frontmost application.
Native process launch identity and pinned AX/window references still govern acts.
It does not grant access to additional apps. Close the controller to release its
AX observer and any OCR worker. Closed controllers cannot mint new handles.

## Native operation vocabulary

- `click`: native `AXPress`. Legacy `press` remains an alias for compatibility.
  **It does not mean a keyboard key**, unlike browser `press`; no keyboard action
  is silently synthesized. Prefer `click` in new cross-harness controllers.
- `setValue`: non-secure, settable text fields/areas only; requires `text`.
  This is an AX value write, not event-driven typing or browser `fill` semantics.
- `increment`, `decrement`: `AXIncrement` / `AXDecrement` when advertised.
- `scroll_up`, `scroll_down`, `scroll_left`, `scroll_right`: corresponding native
  `AXScroll*ByPage` actions, not unguarded wheel events.
- `showMenu`: `AXShowMenu` when advertised. This is not a menu-bar command API or
  permission to select arbitrary menu items; each next target needs observation.

Unknown/unadvertised actions fail closed. Only `setValue` accepts `text`.
Dialogs, secure controls, approval controls, and background effects stay blocked.

## Changes are not readiness; readiness is not success

AX notifications supplement fresh bounded reads. The observer journals window,
focus, sheet/menu, value and layout activity; activity changes the revision even
if observed values later revert. Notifications are best-effort and some apps
omit them. `changeSource` is `ax_notifications` or `polling`; reads still poll at
most every 100ms while waiting. No stale value cache authorizes effects.

`waitForChange` returns on a changed revision, not on quiet. `settle` waits for a
reaction (default 600ms), then a quiet interval (150ms), capped at 2000ms total.
All explicit timer values are integer milliseconds, 0–60000. A zero total timeout
performs one bounded observation. `settled` requires a reaction **and** quiet;
no reaction and deadline exhaustion are distinguishable from readiness.

Both methods return evidence read during that call, or fail if no fresh bounded
read fits. Native reads are budgeted, not hard real-time. An app can react again
after quiet; observe and verify the actual task outcome. Keep waits within the
transport's call deadline. OCR waits need enough time for fresh pixel recognition.

## Opt-in local Vision OCR

AX-only is the default. Enable pixels explicitly at the connection boundary:

```sh
macos-harness serve --app com.example.App --ocr auto
# --ocr always: supplement usable AX too; --ocr never: AX-only.
# --ocr-recognition-level fast (default) or accurate (explicit).
```

Python: use `NativeHybridBackend(mac, ocr="auto")` in the same controller.
Fabric config: `ocr: "auto"`, optional `ocrRecognitionLevel: "fast"`.

This is Apple Vision, **not Apple Intelligence**. Recognition is local, CPU-only,
with no cloud, model credentials or language-model-generated text. A warm isolated
worker avoids repeated framework loading; hybrid-backend construction explicitly
warms it (up to 8s), without reading apps or requesting permissions. Read deadlines
are enforced by the parent and a native process timer. Closing releases the worker.
Fast mode is the low-latency default; accurate mode may exceed the observation
budget on some systems. Timeout is an error, never a silent mode downgrade.

`auto` captures only when AX offers no enabled labelled non-container control.
Only one unambiguous on-screen AX window matched to the app's WindowServer bounds
is supported. No full-desktop capture, window parking or activation is used.
Screen Recording must already be approved. Temporary window images are removed
on both success and failure; no image is returned over JSONL.

Candidates add `source: "ax" | "ocr"`, optional screen-point `bounds` and OCR
`confidence`. Retina pixels are mapped using normalized text rectangles. Known
native labels/regions are deduplicated. `ocrStatus` is `disabled`, `not_needed`,
`blocked`, or `used`. Only high-confidence OCR text (at least 0.8) may advertise
`click`. It is a pixel hit, not proof the text is a semantic button. No arbitrary
coordinates, typing, inferred shortcuts or text generation are accepted by `act`.

OCR is suppressed for truncated AX observations, redacted/protected controls,
dialogs/approval controls, permission-management apps, ambiguous windows, and
native controls whose missing geometry prevents safe exclusion. OCR clicks never
overlap known non-container AX controls, even disabled ones. Before dispatch,
re-OCR and a full window raster fingerprint revalidate the target; input remains
foreground-only and uncertain receipts never replay. Animating windows may remain
stale and require another approach rather than weakening this guard.

**Privacy limitation:** screenshot text has no reliable secure-field metadata.
An app can omit or mislabel secrets in AX; this is not general DLP or a hostile-app
sandbox. Enabling OCR grants pixel disclosure for the app. Do not enable it for
sensitive surfaces on the assumption that AX redaction can prove the pixels safe.

## Checks

```sh
uv run pytest
uv run ruff check .
# Opt-in native OCR, generated pixels only: no screen capture, apps, or OS grants.
MACOS_HARNESS_TEST_VISION=1 uv run pytest tests/test_ocr.py -q
cd pi && bun run typecheck && bun run test
```

Native window capture/input remains separately permission-gated. The default tests
use synthetic native backends. No live-app action is part of these test commands.
