import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";
import type { FabricInvocationContext } from "pi-fabric/protocol";
import type { HarnessObservation } from "../contract.js";
import { MacOSHarnessProvider } from "../macos.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const python = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const fixture = fileURLToPath(new URL("../../tests/fixtures/guarded_rpc_server.py", import.meta.url));

// Optional checkout-only integration; uses the real CLI with a patched offline backend.
it.skipIf(!existsSync(python) || !existsSync(fixture))("adapts the actual offline Python CLI without native app access", async () => {
  const provider = new MacOSHarnessProvider({ command: [python, "-u", fixture], allowedApps: ["Test"], callTimeoutMs: 5000 });
  const context = { cwd: root, signal: undefined, parentToolCallId: "test", nestedToolCallId: "nested", extensionContext: {} as FabricInvocationContext["extensionContext"], update() {} } satisfies FabricInvocationContext;
  const scope = { app: "Test" };
  try {
    await provider.invoke("connect", {}, context);
    const observation = await provider.invoke("observe", { scope }, context) as HarnessObservation;
    expect(observation.candidates).toHaveLength(1);
    expect(observation.candidates[0]).toMatchObject({ label: "Save", operations: ["press"] });
    const action = { scope, observationId: observation.observationId, action: { targetId: observation.candidates[0]!.id, operation: "press" } };
    expect(await provider.invoke("act", action, context)).toEqual({ status: "executed" });
    expect(await provider.invoke("act", action, context)).toMatchObject({ status: "stale" });
    const check = await provider.invoke("waitForChange", { scope, revision: observation.revision, timeoutMs: 0 }, context) as { changed: boolean; observation: HarnessObservation };
    expect(check.changed).toBe(true);
    expect(check.observation.revision).not.toBe(observation.revision);
    expect(check.observation.observationId).not.toBe(observation.observationId);
  } finally { await provider.close(); }
}, 15000);
