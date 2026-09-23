import { expect, it, vi } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { FabricComponentContext, FabricComponentDefinition, FabricProvider } from "pi-fabric/protocol";

const loads = vi.hoisted(() => ({ provider: 0, transport: 0 }));
vi.mock("../macos.js", async original => { loads.provider++; return original(); });
vi.mock("../macos-rpc.js", async original => { loads.transport++; return original(); });

it("registration exposes schema without engines; activation imports no transport and rejects leases cleanly", async () => {
  const { default: extension } = await import("../extension.js");
  let definition!: FabricComponentDefinition;
  extension({ events: { on() {}, emit(_name: string, value: { component: FabricComponentDefinition }) { definition = value.component; } } } as unknown as ExtensionAPI);
  expect(definition.configSchema?.properties).toHaveProperty("callTimeoutMs");
  expect(loads).toEqual({ provider: 0, transport: 0 });
  let provider!: FabricProvider;
  const context = { invocation: { cwd: process.cwd() }, provide(value: FabricProvider) { provider = value; } } as unknown as FabricComponentContext;
  const config = { command: ["never-start-this-command"], allowedApps: ["Test"] };
  await definition.activate(context, config);
  expect(loads).toEqual({ provider: 1, transport: 0 });
  await provider.list({}, context.invocation); await provider.describe("observe", context.invocation); await provider.close?.();
  const { MacOSHarnessProvider } = await import("../macos.js");
  const close = vi.spyOn(MacOSHarnessProvider.prototype, "close");
  try {
    await expect(definition.activate({ ...context, provide() { throw new Error("lease rejected"); } }, config)).rejects.toThrow("lease rejected");
    expect(close).toHaveBeenCalledOnce();
    expect(loads.transport).toBe(0);
  } finally { close.mockRestore(); }
});
