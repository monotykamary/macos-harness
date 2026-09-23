import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { validationMessage } from "../validation.js";
import { macosHarnessComponent } from "../component.js";
import type { FabricComponentContext } from "pi-fabric/protocol";
import type { FabricInvocationContext } from "pi-fabric/protocol";
import { MacOSHarnessProvider, type MacOSHarnessClient, type MacOSHarnessConfig } from "../macos.js";
import { createMacOSHarnessClient } from "../macos-rpc.js";

const fixture = fileURLToPath(new URL("./fixtures/harness-macos-child.mjs", import.meta.url));
const config: MacOSHarnessConfig = { command: ["macos-harness"], allowedApps: ["Notes", "com.example.Test App"] };
const scope = { app: "Notes" };
const observation = { scope, observationId: "o1", revision: "r1", candidates: [{ id: "b1", role: "button", label: "Save", operations: ["press"] }], truncated: false };
const action = { scope, observationId: "o1", action: { targetId: "b1", operation: "press" } };
const context = (signal?: AbortSignal): FabricInvocationContext => ({ cwd: process.cwd(), signal, parentToolCallId: "test", nestedToolCallId: "nested", extensionContext: {} as FabricInvocationContext["extensionContext"], update() {} });
function setup(overrides: Partial<MacOSHarnessClient> = {}, settings: MacOSHarnessConfig = config) {
  let connected = true;
  const client: MacOSHarnessClient = {
    isConnected: () => connected,
    observe: vi.fn(async () => observation), act: vi.fn(async () => ({ status: "executed" })),
    waitForChange: vi.fn(async () => ({ changed: false, observation })),
    close: vi.fn(() => { connected = false; }), ...overrides,
  };
  const factory = vi.fn(async () => client);
  return { client, factory, provider: new MacOSHarnessProvider(settings, factory) };
}
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
};
async function pipe(mode = "normal", timeout = 2000) {
  const client = await createMacOSHarnessClient({ command: [process.execPath, fixture, mode], allowedApps: config.allowedApps, cwd: process.cwd(), callTimeoutMs: timeout, signal: new AbortController().signal });
  const probe = await client.observe({ scope, maxElements: 1 }) as typeof observation & { fixture: { pid: number; cwd: string; argv: string[]; sequence: number } };
  return { client, probe };
}
const dead = (pid: number) => expect(() => process.kill(pid, 0)).toThrow();

describe("macOS Harness provider", () => {
  it("has an authoritative bounded config schema and matching constructor checks", async () => {
    const validate = (value: unknown) => { if (validationMessage(macosHarnessComponent.configSchema!, value)) throw new Error("Invalid config"); };
    expect(() => validate(config)).not.toThrow();
    const invalid: unknown[] = [null, {}, { ...config, command: [] }, { ...config, allowedApps: [] }, { ...config, command: Array(33).fill("x") }, { ...config, allowedApps: Array.from({ length: 33 }, (_, i) => `App${i}`) }, { ...config, command: [""] }, { ...config, command: ["x\u0000"] }, { ...config, command: ["x".repeat(4097)] }, { ...config, allowedApps: [" "] }, { ...config, allowedApps: [" App"] }, { ...config, allowedApps: ["x".repeat(257)] }, { ...config, allowedApps: ["Notes", "Notes"] }, { ...config, callTimeoutMs: 99 }, { ...config, callTimeoutMs: 60001 }, { ...config, callTimeoutMs: 100.5 }, { ...config, callTimeoutMs: NaN }, { ...config, extra: "PRIVATE_DIAGNOSTIC" }];
    for (const value of invalid) {
      expect(() => validate(value)).toThrow("Invalid config");
      expect(() => new MacOSHarnessProvider(value as MacOSHarnessConfig)).toThrow("Invalid macOS Harness configuration");
    }
    for (const callTimeoutMs of [100, 60000]) { const provider = new MacOSHarnessProvider({ ...config, callTimeoutMs }); await provider.close(); }
  });
  it("binds grants into descriptors, preserves risk/effects, and does not leak argv", async () => {
    const { provider, factory } = setup();
    const other = new MacOSHarnessProvider({ ...config, allowedApps: ["Other"] });
    try {
      const descriptors = await provider.list();
      expect(descriptors.map(d => [d.name, d.risk])).toEqual([["connect", "execute"], ["observe", "read"], ["act", "execute"], ["waitForChange", "read"]]);
      for (const descriptor of descriptors) {
        expect(descriptor.effect).toEqual({ kind: "emission", resources: ["harness:macos"], ordering: "ordered" });
        expect(descriptor.description).toContain("Notes");
        expect(descriptor).not.toEqual(await other.describe(descriptor.name));
      }
      expect(JSON.stringify(descriptors)).not.toContain("command");
      expect((descriptors[1]!.inputSchema as any).properties.scope.properties.app.enum).toEqual(config.allowedApps);
      (descriptors[1]!.inputSchema as any).properties.scope.properties.app.enum.push("Forbidden");
      expect(JSON.stringify(await provider.describe("observe"))).not.toContain("Forbidden");
      expect(factory).not.toHaveBeenCalled();
    } finally { await provider.close(); await other.close(); }
  });
  it("activation, discovery and reads never launch; explicit connect reuses one client", async () => {
    const { provider, client, factory } = setup();
    const provide = vi.fn();
    await macosHarnessComponent.activate({ invocation: context(), provide } as unknown as FabricComponentContext, config);
    expect(macosHarnessComponent).toMatchObject({ name: "macos-harness", provides: ["macos"], guarantee: "managed" });
    expect(provide).toHaveBeenCalledOnce();
    await provide.mock.calls[0]![0].close();
    await provider.list(); await provider.describe("observe");
    await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("connect");
    expect(factory).not.toHaveBeenCalled();
    try {
      expect(await provider.invoke("connect", {}, context())).toEqual({ connected: true, permissionsVerified: false });
      await provider.invoke("connect", {}, context());
      expect(factory).toHaveBeenCalledOnce();
      expect(factory.mock.calls[0]).toEqual([expect.objectContaining({ command: config.command, allowedApps: config.allowedApps, cwd: process.cwd(), callTimeoutMs: 10000, signal: expect.any(AbortSignal) })]);
      expect(await provider.invoke("observe", { scope }, context())).toEqual(observation);
      expect(await provider.invoke("act", action, context())).toEqual({ status: "executed" });
      expect(await provider.invoke("waitForChange", { scope, revision: "r1", timeoutMs: 0 }, context())).toEqual({ changed: false, observation });
    } finally { await Promise.all([provider.close(), provider.close()]); }
    expect(client.close).toHaveBeenCalledOnce();
    await expect(provider.invoke("connect", {}, context())).rejects.toThrow("closed");
  });
  it("rejects unknown methods, scopes and arguments before dispatch", async () => {
    const { provider, factory } = setup();
    for (const [method, args] of [["eval", {}], ["connect", { app: "Notes" }], ["observe", { scope: { app: "Other" } }], ["observe", { scope, unknown: true }], ["observe", { scope: { ...scope, extra: true } }], ["act", { ...action, action: { ...action.action, text: "x".repeat(4097) } }], ["waitForChange", { scope, revision: "r1", timeoutMs: -1 }]] as const) {
      await expect(provider.invoke(method, args, context())).rejects.toThrow();
    }
    expect(factory).not.toHaveBeenCalled(); await provider.close();
  });
  it("copies trusted configuration and uses component cwd as an explicit fallback", async () => {
    const mutable = structuredClone(config);
    const { client } = setup();
    const factory = vi.fn(async () => client);
    const provider = new MacOSHarnessProvider(mutable, factory, "/component-root");
    mutable.allowedApps.push("Forbidden"); mutable.command[0] = "forbidden";
    try {
      await provider.invoke("connect", {}, { ...context(), cwd: "" });
      expect(factory).toHaveBeenCalledWith(expect.objectContaining({ cwd: "/component-root", command: config.command, allowedApps: config.allowedApps }));
    } finally { await provider.close(); }
  });
  it("rejects overload and closes late connections after cancellation without resurrecting", async () => {
    const late = deferred<MacOSHarnessClient>();
    const { client } = setup();
    const factory = vi.fn(() => late.promise);
    const provider = new MacOSHarnessProvider(config, factory);
    const controller = new AbortController();
    const connecting = provider.invoke("connect", {}, context(controller.signal));
    await vi.waitFor(() => expect(factory).toHaveBeenCalledOnce());
    await expect(provider.invoke("connect", {}, context())).rejects.toThrow("busy");
    controller.abort("PRIVATE_DIAGNOSTIC");
    await expect(connecting).rejects.toThrow("interrupted");
    late.resolve(client);
    await vi.waitFor(() => expect(client.close).toHaveBeenCalledOnce());
    await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("connect");
    await provider.close();
  });
  it.each(["cancel", "timeout", "close"])("reports unknown sent effects on %s, never late success", async kind => {
    const late = deferred<unknown>();
    const { provider, client } = setup({ act: vi.fn(() => late.promise) }, { ...config, callTimeoutMs: 100 });
    await provider.invoke("connect", {}, context());
    const controller = new AbortController();
    const pending = provider.invoke("act", action, context(controller.signal));
    expect(client.act).toHaveBeenCalledOnce();
    await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("busy");
    if (kind === "cancel") controller.abort("PRIVATE_DIAGNOSTIC");
    if (kind === "close") await provider.close();
    expect(await pending).toMatchObject({ status: "outcome_unknown" });
    late.resolve({ status: "executed" });
    await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow();
    await provider.close(); expect(client.close).toHaveBeenCalledOnce();
  });
  it("sanitizes client errors and rejects malformed/scope-mismatched results", async () => {
    for (const observe of [async () => { throw new Error("PRIVATE_DIAGNOSTIC"); }, async () => ({}), async () => ({ ...observation, scope: { app: "Forbidden" } })]) {
      const { provider, client } = setup({ observe });
      await provider.invoke("connect", {}, context());
      await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("macOS Harness call failed or interrupted; explicitly reconnect");
      await provider.close(); expect(client.close).toHaveBeenCalledOnce();
    }
  });
  it("does not let a cancelled late reply disconnect a fresh explicit connection", async () => {
    const late = deferred<unknown>();
    const first = setup({ act: () => late.promise }).client;
    const second = setup().client;
    const factory = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    const provider = new MacOSHarnessProvider(config, factory);
    try {
      await provider.invoke("connect", {}, context());
      const controller = new AbortController();
      const pending = provider.invoke("act", action, context(controller.signal));
      controller.abort();
      expect(await pending).toMatchObject({ status: "outcome_unknown" });
      await provider.invoke("connect", {}, context());
      late.resolve({ status: "outcome_unknown" });
      await new Promise(resolve => setTimeout(resolve, 0));
      expect(await provider.invoke("observe", { scope }, context())).toEqual(observation);
      expect(second.close).not.toHaveBeenCalled();
    } finally { await provider.close(); }
  });
  it("bounds abandoned injected factories and closes all late clients", async () => {
    const late = deferred<MacOSHarnessClient>();
    const { client } = setup();
    const factory = vi.fn(() => late.promise);
    const provider = new MacOSHarnessProvider(config, factory);
    for (let i = 0; i < 16; i++) {
      const controller = new AbortController();
      const pending = provider.invoke("connect", {}, context(controller.signal));
      await Promise.resolve();
      controller.abort();
      await expect(pending).rejects.toThrow("interrupted");
    }
    await expect(provider.invoke("connect", {}, context())).rejects.toThrow("busy");
    expect(factory).toHaveBeenCalledTimes(16);
    late.resolve(client);
    await vi.waitFor(() => expect(client.close).toHaveBeenCalledTimes(16));
    await provider.close();
  });
  it("reports cleanup failure without raw diagnostics and blocks reconnect", async () => {
    const { provider, factory } = setup({ close: () => { throw new Error("PRIVATE_DIAGNOSTIC"); } });
    await provider.invoke("connect", {}, context());
    await expect(provider.invoke("observe", { scope }, context(AbortSignal.abort()))).rejects.toThrow("cancelled");
    await expect(provider.invoke("connect", {}, context())).rejects.toThrow("failed or interrupted");
    expect(factory).toHaveBeenCalledOnce();
    await expect(provider.close()).rejects.toThrow("macOS Harness cleanup failed");
  });
  it("requires explicit reconnect after transport loss and pre-abort", async () => {
    const { provider, client, factory } = setup();
    await provider.invoke("connect", {}, context());
    const aborted = AbortSignal.abort("PRIVATE_DIAGNOSTIC");
    await expect(provider.invoke("observe", { scope }, context(aborted))).rejects.toThrow("cancelled");
    await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("connect");
    expect(factory).toHaveBeenCalledOnce();
    await provider.close(); expect(client.close).toHaveBeenCalledOnce();
  });
});

describe("macOS Harness real offline pipes", () => {
  it.skipIf(process.platform === "win32")("close kills the owned process group including descendants", async () => {
    const { client, probe } = await pipe("descendant");
    const descendantPid = (probe.fixture as typeof probe.fixture & { descendantPid: number }).descendantPid;
    try {
      expect(descendantPid).toBeGreaterThan(0);
      expect(() => process.kill(descendantPid, 0)).not.toThrow();
    } finally { await client.close(); }
    dead(probe.fixture.pid);
    await vi.waitFor(() => dead(descendantPid));
  });
  it("passes trusted argv exactly with repeated --app, no shell, and invocation cwd", async () => {
    const { client, probe } = await pipe();
    try {
      expect(probe.fixture.argv).toEqual(["serve", "--app", "Notes", "--app", "com.example.Test App"]);
      expect(probe.fixture.cwd).toBe(process.cwd());
      expect(await client.act(action)).toEqual({ status: "executed" });
      expect(await client.waitForChange({ scope, revision: "r1", timeoutMs: 0 })).toMatchObject({ changed: true, observation: { scope, truncated: false } });
    } finally { await Promise.all([client.close(), client.close()]); }
    dead(probe.fixture.pid); expect(client.isConnected()).toBe(false);
  });
  it.each(["partial", "stderr", "boundary"])("accepts %s transport behavior without diagnostics in output", async mode => {
    const { client, probe } = await pipe(mode);
    try { expect(await client.observe({ scope })).toMatchObject({ scope, truncated: false }); }
    finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it("serializes 16 pending requests, rejects overflow, and uses monotonic IDs", async () => {
    const { client, probe } = await pipe("delay");
    try {
      const calls = Array.from({ length: 16 }, () => client.observe({ scope }));
      await expect(client.observe({ scope })).rejects.toThrow("limit");
      const results = await Promise.all(calls) as Array<{ fixture: { sequence: number; requestId: number } }>;
      expect(results.map(r => r.fixture.sequence)).toEqual(Array.from({ length: 16 }, (_, i) => i + 2));
      expect(results.map(r => r.fixture.requestId)).toEqual(Array.from({ length: 16 }, (_, i) => i + 2));
      expect(client.isConnected()).toBe(true);
    } finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it.each(["malformed", "oversized", "boundary-over", "wrong-id", "bad-envelope", "bad-result", "bad-scope", "bad-utf8", "eof", "exit"])("fatally closes %s replies and omits private contents", async mode => {
    const { client, probe } = await pipe(mode);
    try {
      await expect(client.observe({ scope })).rejects.toThrow("macOS Harness transport failed or interrupted; explicitly reconnect");
      expect(client.isConnected()).toBe(false);
      await expect(client.observe({ scope })).rejects.toThrow("reconnect");
    } finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it.each(["malformed", "oversized", "wrong-id", "bad-envelope", "bad-result", "bad-utf8", "exit", "error"])("reports act uncertainty on %s, never retries", async mode => {
    const { client, probe } = await pipe(mode);
    try { expect(await client.act(action)).toMatchObject({ status: "outcome_unknown" }); }
    finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it("sanitizes a valid remote error envelope", async () => {
    const { client, probe } = await pipe("error");
    try { await expect(client.observe({ scope })).rejects.toThrow("macOS Harness transport failed or interrupted; explicitly reconnect"); }
    finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it.each(["cancel", "timeout", "close"])("kills the child on %s with honest sent-act outcome", async kind => {
    const { client, probe } = await pipe("hang", 2000);
    const controller = new AbortController();
    const pending = client.act(action, { signal: controller.signal });
    if (kind === "cancel") controller.abort("PRIVATE_DIAGNOSTIC");
    if (kind === "close") await client.close();
    try { expect(await pending).toMatchObject({ status: "outcome_unknown" }); }
    finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it("cancelling a queued unsent act kills all calls without claiming an effect", async () => {
    const { client, probe } = await pipe("hang");
    const active = expect(client.observe({ scope })).rejects.toThrow("reconnect");
    const controller = new AbortController();
    const queued = expect(client.act(action, { signal: controller.signal })).rejects.toThrow("reconnect");
    controller.abort();
    try { await Promise.all([active, queued]); }
    finally { await client.close(); }
    dead(probe.fixture.pid);
  });
  it("does not expose spawn errors or create a client for an aborted start", async () => {
    const options = { command: ["/not/a/PRIVATE_DIAGNOSTIC-command"], allowedApps: ["Notes"], cwd: process.cwd(), callTimeoutMs: 1000, signal: new AbortController().signal };
    await expect(createMacOSHarnessClient(options)).rejects.toThrow("macOS Harness transport failed or interrupted; explicitly reconnect");
    await expect(createMacOSHarnessClient({ ...options, signal: AbortSignal.abort("PRIVATE_DIAGNOSTIC") })).rejects.toThrow("macOS Harness transport failed or interrupted; explicitly reconnect");
  });
  it("runs the production lazy factory, reconnects explicitly after timeout, and disposes", async () => {
    const provider = new MacOSHarnessProvider({ command: [process.execPath, fixture, "hang"], allowedApps: ["Notes"], callTimeoutMs: 2000 });
    let firstPid = 0; let secondPid = 0;
    try {
      await provider.invoke("connect", {}, context());
      firstPid = (await provider.invoke("observe", { scope, maxElements: 1 }, context()) as any).fixture.pid;
      expect(await provider.invoke("act", action, context())).toMatchObject({ status: "outcome_unknown" });
      await expect(provider.invoke("observe", { scope }, context())).rejects.toThrow("connect");
      await provider.invoke("connect", {}, context());
      secondPid = (await provider.invoke("observe", { scope, maxElements: 1 }, context()) as any).fixture.pid;
      expect(secondPid).not.toBe(firstPid);
    } finally { await provider.close(); }
    dead(firstPid); dead(secondPid);
  });
});
