import { validationMessage } from "./validation.js";
import type { FabricActionDescriptor, FabricInvocationContext, FabricProvider } from "pi-fabric/protocol";
import { interactionDescriptors, validateInteractionResult, type HarnessInteraction } from "./contract.js";

import { configSchema, type MacOSHarnessConfig } from "./config.js";
export type { MacOSHarnessConfig } from "./config.js";

export interface MacOSHarnessClientOptions {
  command: readonly string[];
  allowedApps: readonly string[];
  cwd: string;
  callTimeoutMs: number;
  signal: AbortSignal;
}
export interface MacOSHarnessClient extends HarnessInteraction {
  isConnected(): boolean;
}
/** Resolves after child spawn, not after macOS permission verification. */
export type MacOSHarnessClientFactory = (options: MacOSHarnessClientOptions) => Promise<MacOSHarnessClient>;
const createClient: MacOSHarnessClientFactory = async options => {
  const { createMacOSHarnessClient } = await import("./macos-rpc.js");
  return createMacOSHarnessClient(options);
};
const unknownOutcome = () => ({ status: "outcome_unknown", reason: "macOS Harness call interrupted after dispatch; do not retry automatically. Explicitly reconnect and observe." });

export class MacOSHarnessProvider implements FabricProvider {
  readonly name = "macos";
  readonly description = "Explicit, app-allowlisted macOS Harness connection; no automatic launch or permission approval";
  readonly #config: MacOSHarnessConfig;
  readonly #descriptors: FabricActionDescriptor[];
  #client: MacOSHarnessClient | undefined;
  #active: AbortController | undefined;
  #pending = new Set<Promise<unknown>>();
  #closed = false;
  #closing = new Set<Promise<void>>();
  #closeError = false;

  constructor(config: MacOSHarnessConfig, private readonly factory: MacOSHarnessClientFactory = createClient, private readonly cwd = process.cwd()) {
    if (validationMessage(configSchema, config as unknown as Record<string, unknown>)) throw new Error("Invalid macOS Harness configuration");
    this.#config = { command: [...config.command], allowedApps: [...config.allowedApps], callTimeoutMs: config.callTimeoutMs ?? 10000 };
    const grantNote = `Exact granted apps: ${JSON.stringify(this.#config.allowedApps)}. Handles belong to this connection only; reconnect invalidates them.`;
    this.#descriptors = [
      {
        name: "connect", description: `Spawn the host-configured macOS Harness server. Success confirms process spawn only, not OS permissions. ${grantNote}`,
        inputSchema: { type: "object", properties: {}, additionalProperties: false }, risk: "execute",
        effect: { kind: "emission", resources: ["harness:macos"], ordering: "ordered" },
      },
      ...interactionDescriptors("macos", {
        type: "object", properties: { app: { type: "string", enum: [...this.#config.allowedApps] } }, required: ["app"], additionalProperties: false,
      }, grantNote).map(descriptor => ({ ...descriptor, effect: { kind: "emission" as const, resources: ["harness:macos"], ordering: "ordered" as const } })),
    ];
  }

  async list() { return structuredClone(this.#descriptors); }
  async describe(name: string) { return structuredClone(this.#descriptors.find(descriptor => descriptor.name === name)); }

  #dispose(client: HarnessInteraction): void {
    const closing = Promise.resolve().then(() => client.close()).then(() => {}, () => { this.#closeError = true; });
    this.#closing.add(closing);
    void closing.finally(() => this.#closing.delete(closing));
  }
  #disconnect(): void {
    const client = this.#client;
    this.#client = undefined;
    if (client) this.#dispose(client);
  }

  async invoke(name: string, args: Record<string, unknown>, context: FabricInvocationContext): Promise<unknown> {
    if (this.#closed) throw new Error("macOS Harness provider closed");
    const descriptor = this.#descriptors.find(item => item.name === name);
    if (!descriptor) throw new Error("Unknown macOS Harness action");
    // Do not echo untrusted argument paths, app contents, argv or abort reasons.
    if (validationMessage(descriptor.inputSchema, args)) throw new Error("Invalid macOS Harness arguments");
    if (context.signal?.aborted) { this.#active?.abort(); this.#disconnect(); throw new Error("macOS Harness call cancelled; explicitly reconnect"); }
    // One outstanding provider operation: reject overload rather than retain an unbounded queue.
    if (this.#active || this.#pending.size >= 16) throw new Error("macOS Harness busy; one outstanding operation permitted");
    if (this.#client && !this.#client.isConnected()) this.#disconnect();
    if (name !== "connect" && !this.#client) throw new Error("Call macos.connect before interacting; explicitly reconnect after interruption");
    if (name === "connect" && this.#client) return { connected: true, permissionsVerified: false };
    const controller = new AbortController();
    this.#active = controller;
    const abort = () => controller.abort();
    context.signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, this.#config.callTimeoutMs!);
    let dispatched = false;
    let onAbort!: () => void;
    const interrupted = new Promise<never>((_resolve, reject) => {
      onAbort = () => { this.#disconnect(); reject(new Error("macOS Harness call interrupted; explicitly reconnect")); };
      controller.signal.addEventListener("abort", onAbort, { once: true });
    });
    const work = async () => {
      if (name === "connect") {
        await Promise.all(this.#closing);
        if (this.#closeError) throw new Error("macOS Harness cleanup failed");
        if (controller.signal.aborted || this.#closed) throw new Error("macOS Harness connection interrupted");
        const client = await this.factory({ ...this.#config, callTimeoutMs: this.#config.callTimeoutMs!, cwd: context.cwd || this.cwd, signal: controller.signal });
        if (controller.signal.aborted || this.#closed) { this.#dispose(client); throw new Error("macOS Harness connection interrupted"); }
        this.#client = client;
        return { connected: true, permissionsVerified: false };
      }
      const client = this.#client!;
      dispatched = true;
      let result: unknown;
      switch (name) {
        case "observe": result = await client.observe(args, { signal: controller.signal }); break;
        case "act": result = await client.act(args, { signal: controller.signal }); break;
        case "waitForChange": result = await client.waitForChange(args, { signal: controller.signal }); break;
        default: throw new Error("Unknown macOS Harness action");
      }
      if (controller.signal.aborted || this.#closed) throw new Error("macOS Harness call interrupted");
      const validated = validateInteractionResult(name, result);
      const observation = name === "waitForChange" ? (validated as { observation?: unknown }).observation : name === "observe" ? validated : undefined;
      if (observation && (observation as { scope?: { app?: unknown } }).scope?.app !== (args.scope as { app: string }).app) throw new Error("macOS Harness scope mismatch");
      if (name === "act" && (validated as { status: string }).status === "outcome_unknown") this.#disconnect();
      return validated;
    };
    const task = work();
    this.#pending.add(task);
    void task.then(() => this.#pending.delete(task), () => this.#pending.delete(task));
    try {
      const result = await Promise.race([task, interrupted]);
      if (controller.signal.aborted || this.#closed) throw new Error("macOS Harness call interrupted");
      return result;
    } catch {
      this.#disconnect();
      if (name === "act" && dispatched) return unknownOutcome();
      throw new Error("macOS Harness call failed or interrupted; explicitly reconnect");
    } finally {
      clearTimeout(timer);
      context.signal?.removeEventListener("abort", abort);
      controller.signal.removeEventListener("abort", onAbort);
      if (this.#active === controller) this.#active = undefined;
    }
  }

  async close(): Promise<void> {
    this.#closed = true;
    this.#active?.abort();
    this.#disconnect();
    await Promise.all(this.#closing);
    if (this.#closeError) throw new Error("macOS Harness cleanup failed");
  }
}
