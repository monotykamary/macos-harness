import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { TextDecoder } from "node:util";
import { validationMessage } from "./validation.js";
import { interactionDescriptors, validateInteractionResult, type HarnessCallOptions } from "./contract.js";
import type { MacOSHarnessClient, MacOSHarnessClientOptions } from "./macos.js";

const MAX_FRAME_BYTES = 128 * 1024;
const MAX_PENDING = 16;
type Method = "observe" | "act" | "waitForChange";
type Pending = {
  id: number; method: Method; args: Record<string, unknown>; frame: string; sent: boolean;
  resolve(value: unknown): void; reject(error: Error): void;
  cleanup(): void;
};
const failure = () => new Error("macOS Harness transport failed or interrupted; explicitly reconnect");
const unknownOutcome = () => ({ status: "outcome_unknown", reason: "macOS Harness lost the reply after dispatch; reconnect and observe, never retry automatically." });
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);

/** A private process group owns the trusted argv prefix and any server descendants.
 * No shell, stderr capture, retries, or permission probes. All timers/listeners for
 * calls are released on settlement; SIGKILL invalidates the complete handle set.
 */
class MacOSHarnessRpcClient implements MacOSHarnessClient {
  readonly #child: ChildProcessByStdio<Writable, Readable, null>;
  readonly #schemas;
  readonly #queue: Pending[] = [];
  readonly #closedPromise: Promise<void>;
  readonly #ready: Promise<void>;
  #active: Pending | undefined;
  #nextId = 1;
  #buffer = Buffer.alloc(0);
  #dead = false;
  #spawned = false;
  #rejectStart!: (error: Error) => void;

  constructor(private readonly options: MacOSHarnessClientOptions) {
    if (options.signal.aborted) throw failure();
    this.#schemas = interactionDescriptors("macos", {
      type: "object", properties: { app: { type: "string", enum: [...options.allowedApps] } }, required: ["app"], additionalProperties: false,
    });
    try {
      this.#child = spawn(options.command[0]!, [...options.command.slice(1), "serve", ...options.allowedApps.flatMap(app => ["--app", app])], {
        cwd: options.cwd, shell: false, stdio: ["pipe", "pipe", "ignore"], detached: process.platform !== "win32",
      });
    } catch { throw failure(); }
    this.#closedPromise = new Promise(resolve => this.#child.once("close", () => { this.#shutdown(); resolve(); }));
    this.#ready = new Promise((resolve, reject) => {
      this.#rejectStart = reject;
      this.#child.once("spawn", () => {
        if (this.#dead || options.signal.aborted) { this.#shutdown(); reject(failure()); return; }
        this.#spawned = true;
        resolve();
      });
    });
    this.#child.on("error", () => this.#shutdown());
    this.#child.once("exit", () => this.#shutdown());
    this.#child.stdin.on("error", () => this.#shutdown());
    this.#child.stdout.on("error", () => this.#shutdown());
    this.#child.stdout.once("end", () => this.#shutdown());
    this.#child.stdout.on("data", (chunk: Buffer) => this.#receive(chunk));
  }

  async start(): Promise<void> {
    const abort = () => this.#shutdown();
    this.options.signal.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, this.options.callTimeoutMs);
    try {
      if (this.options.signal.aborted) this.#shutdown();
      await this.#ready;
      if (this.#dead || this.options.signal.aborted) throw failure();
    } catch {
      await this.close();
      throw failure();
    } finally {
      clearTimeout(timer);
      this.options.signal.removeEventListener("abort", abort);
    }
  }
  isConnected(): boolean { return this.#spawned && !this.#dead; }
  observe(args: Record<string, unknown>, options?: HarnessCallOptions) { return this.#request("observe", args, options); }
  act(args: Record<string, unknown>, options?: HarnessCallOptions) { return this.#request("act", args, options); }
  waitForChange(args: Record<string, unknown>, options?: HarnessCallOptions) { return this.#request("waitForChange", args, options); }

  #request(method: Method, args: Record<string, unknown>, options?: HarnessCallOptions): Promise<unknown> {
    const descriptor = this.#schemas.find(item => item.name === method)!;
    if (validationMessage(descriptor.inputSchema, args)) return Promise.reject(new Error("Invalid macOS Harness arguments"));
    if (options?.signal?.aborted) { this.#shutdown(); return Promise.reject(failure()); }
    if (!this.isConnected()) return Promise.reject(failure());
    if (this.#queue.length + Number(!!this.#active) >= MAX_PENDING) return Promise.reject(new Error("macOS Harness outstanding call limit reached"));
    if (!Number.isSafeInteger(this.#nextId)) { this.#shutdown(); return Promise.reject(failure()); }
    const id = this.#nextId++;
    let frame: string;
    let snapshot: Record<string, unknown>;
    try {
      frame = JSON.stringify({ id, method, args }) + "\n";
      if (Buffer.byteLength(frame) > MAX_FRAME_BYTES) throw failure();
      snapshot = JSON.parse(frame).args as Record<string, unknown>;
    } catch { return Promise.reject(new Error("Invalid macOS Harness request encoding or size")); }
    return new Promise((resolve, reject) => {
      const abort = () => this.#shutdown();
      const timer = setTimeout(abort, this.options.callTimeoutMs);
      const pending: Pending = {
        id, method, args: snapshot, frame, sent: false, resolve, reject,
        cleanup: () => { clearTimeout(timer); options?.signal?.removeEventListener("abort", abort); },
      };
      this.#queue.push(pending);
      options?.signal?.addEventListener("abort", abort, { once: true });
      if (options?.signal?.aborted) this.#shutdown();
      else this.#dispatch();
    });
  }
  #dispatch(): void {
    if (this.#dead || this.#active) return;
    const pending = this.#queue.shift();
    if (!pending) return;
    this.#active = pending;
    // Mark before write: a write error cannot prove that zero bytes reached the server.
    pending.sent = true;
    try { this.#child.stdin.write(pending.frame, error => { if (error) this.#shutdown(); }); }
    catch { this.#shutdown(); }
  }
  #receive(chunk: Buffer): void {
    if (this.#dead) return;
    let offset = 0;
    while (offset < chunk.length && !this.#dead) {
      const newline = chunk.indexOf(10, offset);
      const end = newline < 0 ? chunk.length : newline + 1;
      const segment = chunk.subarray(offset, end);
      if (this.#buffer.length + segment.length > MAX_FRAME_BYTES) { this.#shutdown(); return; }
      this.#buffer = Buffer.concat([this.#buffer, segment]);
      offset = end;
      if (newline < 0) return;
      const frame = this.#buffer;
      this.#buffer = Buffer.alloc(0);
      this.#reply(frame.subarray(0, frame.length - 1));
    }
  }
  #reply(frame: Buffer): void {
    const pending = this.#active;
    try {
      const reply: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(frame));
      if (!pending || !record(reply) || reply.id !== pending.id || Object.keys(reply).length !== 2 ||
          (Object.hasOwn(reply, "result") === Object.hasOwn(reply, "error"))) throw failure();
      if (Object.hasOwn(reply, "error")) {
        if (!record(reply.error) || Object.keys(reply.error).length !== 2 || typeof reply.error.code !== "string" || reply.error.code.length < 1 || reply.error.code.length > 128 ||
            typeof reply.error.message !== "string" || reply.error.message.length > 4096) throw failure();
        // Remote diagnostic text may contain private app contents or credentials.
        this.#settle(pending, undefined, true);
      } else {
        const result = validateInteractionResult(pending.method, reply.result);
        const observation = pending.method === "observe" ? result : pending.method === "waitForChange" ? (result as { observation: unknown }).observation : undefined;
        if (observation && (observation as { scope?: { app?: unknown } }).scope?.app !== (pending.args.scope as { app: string }).app) throw failure();
        this.#settle(pending, result);
      }
      // Do not let a second unsolicited frame in the same chunk match a queued call.
      queueMicrotask(() => this.#dispatch());
    } catch { this.#shutdown(); }
  }
  #settle(pending: Pending, value: unknown, failed = false): void {
    pending.cleanup();
    if (this.#active === pending) this.#active = undefined;
    if (!failed) pending.resolve(value);
    else if (pending.method === "act" && pending.sent) pending.resolve(unknownOutcome());
    else pending.reject(failure());
  }
  #shutdown(): void {
    if (this.#dead) return;
    this.#dead = true;
    this.#rejectStart(failure());
    if (this.#active) this.#settle(this.#active, undefined, true);
    for (const pending of this.#queue.splice(0)) this.#settle(pending, undefined, true);
    this.#buffer = Buffer.alloc(0);
    this.#child.stdin.destroy();
    const pid = this.#child.pid;
    if (pid) {
      try {
        if (process.platform !== "win32") process.kill(-pid, "SIGKILL");
        else this.#child.kill("SIGKILL");
      } catch { this.#child.kill("SIGKILL"); }
    }
  }
  close(): Promise<void> { this.#shutdown(); return this.#closedPromise; }
}

export async function createMacOSHarnessClient(options: MacOSHarnessClientOptions): Promise<MacOSHarnessClient> {
  const client = new MacOSHarnessRpcClient(options);
  await client.start();
  return client;
}
