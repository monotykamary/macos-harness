# Optional macOS Harness Pi extension

A normal Pi package owned by macOS Harness, not a Fabric builtin. Fabric remains
connector-agnostic. Requires host peers `@earendil-works/pi-coding-agent` and
`typebox`; `pi-fabric` is an optional, type-only protocol peer. No sibling repository
imports, global installs, model calls, credentials, or native SDK imports.

```sh
pi -e ../macos-harness/pi/extension.ts
# Or, when authorized:
pi install ../macos-harness/pi
```

The extension uses the existing `pi-fabric:component:register:v1` and
`pi-fabric:component:discover:v1` events in either load order. Without Fabric it is
idle; without this extension a Fabric `macos-harness` config remains waiting.

Inspect `components.describe({ component: "macos-harness" })`, then plan an entry:

```ts
const plan = await components.plan({ entries: [{
  id: "desktop", component: "macos-harness",
  config: { command: ["macos-harness"], allowedApps: ["com.apple.TextEdit"], callTimeoutMs: 10000 },
}] });
// Review plan.changes, warnings, and sources before applying with normal approvals.
await components.apply({ ...plan.request, expectedRevision: plan.revision });
```

Registration exposes `configSchema` via `component.ts`/`config.ts` without engines.
Activation lazily imports `macos.ts` and provides `macos`; only `macos.connect`
loads `macos-rpc.ts` and spawns trusted argv, appending `serve --app APP` per grant.
Connect verifies spawn only. It never launches target apps or grants permissions.
Provider close owns child/process-group cleanup, not rollback of prior effects.
Use the native harness skill for authorized observe → act → check decisions.

Limits: 1–32 argv entries and exact app grants; 100–60000ms call timeout (default
10000); 128 KiB JSON-lines frames; one provider operation, at most 16 transport
requests. No shell, retries, stderr forwarding, implicit reconnect, or cached
handle reuse. Interrupted dispatched effects are honestly `outcome_unknown`.
Use fresh observations after explicit reconnect. Wait budgets must fit call deadlines.

## Offline development checks

From this directory only:

```sh
bun install --ignore-scripts
bun run typecheck
bun run test
```

Tests use a fake Pi event bus, injected clients, and synthetic Node children.
The standalone probe runs outside the repo without a Fabric runtime. The optional
Python integration uses `../.venv/bin/python` and the repository's
`../tests/fixtures/guarded_rpc_server.py` (real CLI, patched offline backend); it
skips when either is absent. No live apps, OS permission probes, or model/key access.
No native suites need repeating for adapter-only changes.
