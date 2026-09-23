import { EventEmitter } from "node:events";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { FabricComponentContext, FabricComponentDefinition, FabricComponentRegistration, FabricInvocationContext, FabricProvider } from "pi-fabric/protocol";
import extension from "../extension.js";

const register = "pi-fabric:component:register:v1";
const discover = "pi-fabric:component:discover:v1";
const invocation = { cwd: process.cwd(), signal: undefined, parentToolCallId: "test", nestedToolCallId: "nested", extensionContext: {} as FabricInvocationContext["extensionContext"], update() {} } satisfies FabricInvocationContext;
const config = { command: ["never-start-this-command"], allowedApps: ["Test"] };
function fakePi() {
  const bus = new EventEmitter();
  const events = {
    on(name: string, listener: (value: unknown) => void) { bus.on(name, listener); return () => { bus.off(name, listener); }; },
    emit(name: string, value: unknown) { bus.emit(name, value); },
  };
  return { events } as ExtensionAPI;
}
function fakeFabric(pi: ExtensionAPI) {
  const definitions = new Map<string, FabricComponentDefinition>();
  const accept = (definition: FabricComponentDefinition, options: { overwrite?: boolean } = {}) => {
    // Mirror Fabric's catalog: identical objects still need explicit overwrite.
    if (definitions.has(definition.name) && !options.overwrite) throw new Error(`Fabric component already registered: ${definition.name}`);
    definitions.set(definition.name, definition);
  };
  pi.events.on(register, (value: unknown) => {
    const registration = value as FabricComponentRegistration;
    expect(registration.version).toBe(1);
    expect(registration.overwrite).toBe(true);
    accept(registration.component, { overwrite: registration.overwrite });
  });
  return { definitions, discover: () => pi.events.emit(discover, { version: 1, register: accept }) };
}
function owner() {
  let provider: FabricProvider | undefined;
  const context = { invocation, provide(value: FabricProvider) { provider = value; } } as unknown as FabricComponentContext;
  return { context, get provider() { if (!provider) throw new Error("No provider"); return provider; } };
}

describe("optional macOS v1 registration", () => {
  it.each(["Fabric first", "connector first"])("supports %s and repeated discovery with no processes", async order => {
    const pi = fakePi();
    if (order === "connector first") extension(pi);
    const fabric = fakeFabric(pi);
    if (order === "Fabric first") extension(pi);
    else { expect(fabric.definitions.size).toBe(0); fabric.discover(); }
    const definition = fabric.definitions.get("macos-harness")!;
    expect(definition).toMatchObject({ provides: ["macos"], guarantee: "managed", configSchema: { required: ["command", "allowedApps"], additionalProperties: false } });
    fabric.discover(); fabric.discover();
    expect(fabric.definitions.size).toBe(1);
    expect(fabric.definitions.get("macos-harness")).toBe(definition);
    const owned = owner();
    expect(await definition.activate(owned.context, config)).toBeUndefined();
    try {
      expect((await owned.provider.list({}, invocation)).map(action => action.name)).toEqual(["connect", "observe", "act", "waitForChange"]);
      await expect(owned.provider.invoke("observe", { scope: { app: "Test" } }, invocation)).rejects.toThrow("connect");
    } finally { await owned.provider.close?.(); }
  });

  it("ignores incompatible discovery and can register without Fabric", () => {
    const pi = fakePi();
    expect(() => extension(pi)).not.toThrow();
    for (const value of [undefined, null, {}, { version: 2, register() { throw new Error("wrong version"); } }, { version: 1, register: false }]) {
      expect(() => pi.events.emit(discover, value)).not.toThrow();
    }
  });

  it("leases own cleanup of the explicitly connected child; close is idempotent", async () => {
    const pi = fakePi();
    const fabric = fakeFabric(pi);
    extension(pi);
    const owned = owner();
    const fixture = fileURLToPath(new URL("./fixtures/harness-macos-child.mjs", import.meta.url));
    const definition = fabric.definitions.get("macos-harness")!;
    // No component disposer competes with Fabric's managed provider lease.
    expect(await definition.activate(owned.context, { ...config, command: [process.execPath, fixture, "normal"] })).toBeUndefined();
    let pid = 0;
    try {
      expect(await owned.provider.invoke("connect", {}, invocation)).toEqual({ connected: true, permissionsVerified: false });
      const observation = await owned.provider.invoke("observe", { scope: { app: "Test" }, maxElements: 1 }, invocation) as { fixture: { pid: number } };
      pid = observation.fixture.pid;
    } finally { await Promise.all([owned.provider.close?.(), owned.provider.close?.()]); }
    expect(pid).toBeGreaterThan(0);
    expect(() => process.kill(pid, 0)).toThrow();
    await expect(owned.provider.invoke("connect", {}, invocation)).rejects.toThrow("closed");
  });
});
