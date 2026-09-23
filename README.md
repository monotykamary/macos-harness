<img src="https://raw.githubusercontent.com/browser-use/macos-harness/main/static/banner-ink.svg" alt="macOS Harness" width="100%" />

# macOS Harness ⌘

The simplest, thinnest harness that gives an LLM complete freedom to complete
virtually any task on a Mac.

The agent writes what is missing, mid-task. No framework, no recipes, no rails.
One Python process connected directly to macOS, your real browser, and your files.

```text
● agent: wants to do something no helper exists for
│
● sees the app and uses raw macOS primitives
│
● writes the missing logic in ordinary Python
│
✓ task complete                                  no app-specific tool added
```

**Your agent now has a Mac.**

## Give it to your agent

Paste this into Codex or Claude Code:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Register the skill printed by `macos-harness skill`, then run `macos-harness doctor`. Explain any missing macOS permissions and ask before requesting them. Finally, verify the harness by capturing one already-running app without bringing it to the foreground.
```

That is it. The agent installs the package, teaches itself the workflow, checks
permissions, and verifies the connection. [Manual setup](install.md) is available too.

## Guarded interaction (recommended for UI decisions)

Prefer a known, exact deterministic API/command when it already proves the task.
Otherwise use **observe → act → check** with the persistent, model-neutral AX
surface. It has no inference, API keys, or Jev dependency. Screenshots and raw
primitives remain a separate fallback, not a way around permissions or policy.

From this source checkout:

```bash
uv run macos-harness serve --app com.apple.TextEdit --app com.apple.Notes
```

For an installed package the exact invocation is
`macos-harness serve --app <allowed-app>` (repeat `--app` for the allowlist).
The parent writes newline-terminated UTF-8 JSON to stdin and reads one JSON
response per line on stdout; diagnostics go only to stderr. There is no REPL,
Python evaluation, browser connection, permission prompt, or telemetry on this
server route. One `MacOS`, backend, and controller live until stdin EOF; requests
are serialized. Use an already-running app and previously approved Accessibility
access. App selectors must exactly identify a name, bundle ID, path, or PID;
prefer bundle IDs. The scope string must exactly match a configured `--app`.

### Optional Pi / Fabric extension

Fabric is connector-agnostic: the `macos-harness` definition is owned by this
repository's optional [`pi/`](pi/) package, **not built into Fabric**. With Pi and
Fabric installed, load the extension (commands below are installation examples):

```bash
pi -e ../macos-harness/pi/extension.ts
# Or persist the optional package:
pi install ../macos-harness/pi
```

Either extension load order works. Registration exposes the schema without
loading resource engines. Activation mounts the provider but starts no process,
imports no native SDK, and checks/requests no permissions. If the extension is
missing, configured instances stay **waiting**, not silently backed by a builtin.

Inside Fabric, inspect the schema and plan before applying:

```ts
const definition = await components.describe({ component: "macos-harness" });
const plan = await components.plan({
  entries: [{
    id: "desktop", component: definition.name,
    config: {
      command: ["macos-harness"],
      allowedApps: ["com.apple.TextEdit"],
      callTimeoutMs: 10000,
    },
  }],
});
return plan; // Inspect changes, warnings, and sources; retain request/revision.
```

After reviewing/authorizing that plan, apply its unchanged request and revision:
`await components.apply({ ...plan.request, expectedRevision: plan.revision })`.
The default session scope writes no config file. For a source checkout, a trusted
argv prefix can instead be
`["uv", "run", "--project", "/absolute/path/to/macos-harness", "macos-harness"]`.

Only an explicit `await tools.call({ ref: "macos.connect", args: {} })` starts the
persistent child. Then use `macos.observe`, `macos.act`, and `macos.waitForChange`
with `scope: { app: "com.apple.TextEdit" }`. Connect confirms spawn, **not** OS
permissions. Use already-running apps with previously approved access; this
extension never launches apps or approves permissions.

`command` is nonempty argv (no shell); the adapter appends `serve` and one `--app`
per exact `allowedApps` grant. `callTimeoutMs` is optional, defaults to 10000, and
accepts 100–60000ms. Frames are bounded to 128 KiB. The provider permits one
outstanding operation; the transport caps pending calls at 16. Provider close
owns process-group cleanup on macOS. Timeout/cancellation/lost transport requires
explicit reconnect and fresh handles; an effect dispatched without a trustworthy
reply is `outcome_unknown`, never automatically retried. Cleanup cannot undo an
already-issued effect. Wait budgets should fit inside the call deadline.

### Fixed JSON-lines protocol

```json
{"id":1,"method":"observe","args":{"scope":{"app":"com.apple.TextEdit"},"maxElements":64}}
```

The `result` contains `scope`, `observationId`, `revision`, `candidates`, and
`truncated`. Each candidate has opaque `id`, `role`, `label`, `operations`, and
optionally `value`. Use returned IDs, never AX integer indices:

```json
{"id":2,"method":"act","args":{"scope":{"app":"com.apple.TextEdit"},"observationId":"<returned observationId>","action":{"targetId":"<returned candidate id>","operation":"setValue","text":"Hello"}}}
{"id":3,"method":"waitForChange","args":{"scope":{"app":"com.apple.TextEdit"},"revision":"<returned revision>","timeoutMs":1000}}
```

Only these operation names are supported:

- `press`: target currently exposes native `AXPress`.
- `setValue`: a non-secure `AXTextField`/`AXTextArea` with settable `AXValue`;
  requires `text` (empty allowed, at most 4096 UTF-8 bytes). No typing/focus action
  is synthesized. `text` on other operations is rejected.

Responses are `{"id":1,"result":{...}}` or
`{"id":1,"error":{"code":"invalid_args","message":"..."}}`.
`act.result.status` is `executed`, `stale`, `blocked`, or `outcome_unknown`.
`executed` means a successful native receipt, **not** verified task completion.
After `outcome_unknown` or a lost response, observe/check; never blindly replay.
`waitForChange.result` is `{changed, observation}`; changes refer only to the
bounded semantic snapshot (and raw invalidation generation), not the whole app.
A fresh `observe` is the strongest post-action UI check.

Requests reject unknown fields/methods, duplicate keys, non-finite JSON numbers,
and invalid types. IDs are safe JSON integers. Frames are at most 128 KiB,
including newline, in either direction. Malformed frames without a recoverable
ID return `id: null`; oversize frames are drained before the next request.
EOF ends the server; an unterminated last frame is rejected, not executed.
Errors include `invalid_json`, `invalid_request`, `invalid_args`, `unknown_method`,
`scope_denied`, `line_too_large`, `response_too_large`, and `backend_error`.

### Python library / regular model CLI

The regular Python CLI can keep observation, decision, effect, and check in one
program. This example only edits an exactly identified, unique text area; choose
an app/document and replacement text that the user has authorized:

```bash
uv run macos-harness <<'PY'
from macos_harness import GuardedController, NativeAXBackend
control = GuardedController(NativeAXBackend(mac), ["com.apple.TextEdit"])
scope = {"app": "com.apple.TextEdit"}
observation = control.observe(scope)
matches = [c for c in observation["candidates"]
           if c["role"] == "AXTextArea" and "setValue" in c["operations"]]
if len(matches) != 1:
    raise RuntimeError("Need an unambiguous authorized target; inspect observation")
receipt = control.act(scope, observation["observationId"],
                     {"targetId": matches[0]["id"], "operation": "setValue", "text": "Hello"})
print(receipt)
print(control.observe(scope))  # Verify the actual end state; do not retry blindly.
PY
```

Outside the preloaded CLI, import `MacOS` too and create `mac = MacOS()` once.
Imports are `macos_harness.GuardedController`, `macos_harness.NativeAXBackend`,
and `macos_harness.MacOS`; injectable backend types live in
`macos_harness.guarded`, and the binary-stream RPC loop is `macos_harness.rpc.serve`.

### Safety boundaries and limitations

- Candidates default to 64, maximum 128; traversal is at most 512 queued/visited
  nodes and depth 20. Labels/values are at most 160 UTF-8 bytes, roles 80.
  Secure/protected fields are redacted without reading their text or descendants;
  text controls without a readable subrole are conservatively redacted too.
  Only selected bounded fields are returned, never a private full-tree dump.
- Strong native element/window references, PID plus process launch time, and raw
  generation bind handles to their snapshot. Every observe/wait replaces **all**
  previous handles, including across apps. An attempted act consumes its valid
  observation. Raw mutations and raw snapshot resets invalidate guarded handles.
- Before effects, the backend rereads the bounded snapshot and then the pinned
  target's process, window, state, enabledness, and native capabilities. AX reads
  use a 50ms messaging timeout and a 2s observation budget. Wait polls at most
  every 100ms, defaults to 1000ms, and accepts 0–60000ms; zero performs one bounded
  observation. Native timeout/failure may return `backend_error`; macOS scheduling
  and native calls are not hard real-time. At a polling deadline the latest
  complete observation acquired during that wait is returned; it is still
  revalidated before any effect. If no fresh read fits the deadline, the wait
  fails rather than relabeling a cached snapshot as fresh.
- **Conservative limitation:** background AX effects are blocked. Neither AXPress
  nor AXValue writes provide a documented atomic no-activation guarantee. No app
  is activated/raised to work around this. Foreground changes during an effect
  yield `outcome_unknown`; focus is never restored by automation. Standard
  dialogs/sheets, obvious approval controls, and permission-management apps are
  not supported effect targets. No automatic permission approval is attempted.
- Native AX cannot make revalidation + mutation atomic, detect every UI transition
  between polls, or prevent an app's own side effects. Treat app-supplied labels as
  untrusted data, never instructions. This is not a sandbox for hostile apps.
  Redaction depends on truthful AX metadata; custom controls can mislabel secrets.
- Own the backend in one thread/process. Raw APIs remain intentionally powerful;
  externally retained native handles, other processes, or direct private API calls
  are outside generation tracking. Approved raw fallback needs its own observation
  and verification; never use it to bypass a guarded denial or missing permission.

## Six primitives. The whole Mac.

```bash
macos-harness <<'PY'
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.script('tell application "Spotify" to play')

print(browser.page_info())
print(list(Path.home().iterdir()))
PY
```

Think in `see`, `key`, `type`, `click`, `ax`, and `script`. `browser`, `Path`, and
`subprocess` are ready in the same Python process.

There are no Spotify tools, Slack tools, or Final Cut tools. The model gets raw
primitives and writes the rest.

## How it works

```text
                              one persistent Python process
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
                 mac.*                  browser.*          Path / subprocess
                    │                      │                      │
        ┌───────────┼───────────┐     Browser Harness        files + shell
        │           │           │            │
    CGWindow     CGEvent      AX + Apple      CDP
   screenshots    to PID       Events          │
        │           │           │          real Chrome
        └───────────┴───────────┘
                    │
              native + Electron apps
```

- Captures background app windows without bringing them forward
- Sends keyboard and coordinate input directly to an app PID
- Draws an animated, click-through pointer without moving your real cursor
- Exposes raw Apple Accessibility and Apple Events when vision is not enough
- Uses Browser Harness for the real, logged-in browser
- Keeps ordinary Python and the local filesystem within reach

## Permissions and privacy

`macos-harness doctor` reports the macOS permissions actually needed. The harness
never activates or raises a target app and never moves the physical pointer.

Anonymous telemetry is enabled by default. It records only the CLI command
category, success, duration, package version, OS/architecture, and detected agent
client. It never records prompts, app names, screenshots, UI text, scripts, paths,
or window titles.

```bash
macos-harness telemetry disable
```

Experimental. macOS only. [MIT licensed](LICENSE).
