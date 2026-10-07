import { fileURLToPath } from "node:url";
import { expect, it, vi } from "vitest";
import type { FabricInvocationContext } from "pi-fabric/protocol";
import { configSchema } from "../config.js";
import { interactionDescriptors, validateInteractionResult } from "../contract.js";
import { MacOSHarnessProvider, type MacOSHarnessClient } from "../macos.js";
import { createMacOSHarnessClient } from "../macos-rpc.js";
import { validationMessage } from "../validation.js";

const scope = { app: "Test" };
const observation = { scope, observationId: "o1", revision: "r1", candidates: [{ id: "c1", role: "OCRText", label: "Play", operations: ["click"], source: "ocr", bounds: { x: 1, y: 2, width: 3, height: 4 }, confidence: 0.95 }], changeSource: "ax_notifications", ocrStatus: "used" };
const settled = { reacted: true, settled: true, timedOut: false, observation };
const context = { cwd: process.cwd(), signal: undefined, parentToolCallId: "test", nestedToolCallId: "test", extensionContext: {} as FabricInvocationContext["extensionContext"], update() {} } satisfies FabricInvocationContext;
const config = { command: ["macos-harness"], allowedApps: ["Test"] };

it("advertises bounded settle arguments and validates nested evidence", () => {
  const descriptor = interactionDescriptors("macos", { type: "object" }).find(item => item.name === "settle")!;
  expect(descriptor.risk).toBe("read");
  expect(validationMessage(descriptor.inputSchema, { scope, revision: "r", quietMs: -1 })).toBeTruthy();
  expect(validationMessage(descriptor.inputSchema, { scope, revision: "r", reactionMs: true })).toBeTruthy();
  expect(validateInteractionResult("settle", settled)).toBe(settled);
  expect(() => validateInteractionResult("settle", { ...settled, settled: "true" })).toThrow("contract");
  expect(() => validateInteractionResult("settle", { ...settled, observation: { ...observation, candidates: [...observation.candidates, ...observation.candidates] } })).toThrow("Duplicate");
});

it("rejects invalid OCR provenance, confidence and geometry", () => {
  for (const override of [{ source: "unknown" }, { confidence: 1.5 }, { bounds: { x: 0, y: 0, width: -1, height: 1 } }]) {
    expect(() => validateInteractionResult("observe", { ...observation, candidates: [{ ...observation.candidates[0], ...override }] })).toThrow("contract");
  }
});

it("validates explicit OCR grant configuration", () => {
  for (const ocr of ["never", "auto", "always"]) expect(validationMessage(configSchema, { ...config, ocr, ocrRecognitionLevel: "fast" })).toBeUndefined();
  for (const override of [{ ocr: true }, { ocr: "vision-cloud" }, { ocrRecognitionLevel: "unknown" }]) {
    expect(validationMessage(configSchema, { ...config, ...override })).toBeTruthy();
    expect(() => new MacOSHarnessProvider({ ...config, ...override } as any)).toThrow("configuration");
  }
});

it("passes settle and OCR grants through the provider without inference", async () => {
  const client: MacOSHarnessClient = { isConnected: () => true, observe: async () => observation, act: async () => ({ status: "executed" }), waitForChange: async () => ({ changed: false, observation }), settle: vi.fn(async () => settled), close: vi.fn() };
  const factory = vi.fn(async () => client);
  const provider = new MacOSHarnessProvider({ ...config, ocr: "auto", ocrRecognitionLevel: "accurate" }, factory);
  try {
    await provider.invoke("connect", {}, context);
    expect(factory).toHaveBeenCalledWith(expect.objectContaining({ ocr: "auto", ocrRecognitionLevel: "accurate" }));
    expect(await provider.invoke("settle", { scope, revision: "r1" }, context)).toEqual(settled);
    expect(client.settle).toHaveBeenCalledOnce();
  } finally { await provider.close(); }
});

it("serializes OCR grants as literal CLI flags, not shell input", async () => {
  const fixture = fileURLToPath(new URL("./fixtures/harness-macos-child.mjs", import.meta.url));
  const client = await createMacOSHarnessClient({ command: [process.execPath, fixture, "normal"], allowedApps: ["Test"], ocr: "auto", ocrRecognitionLevel: "fast", cwd: process.cwd(), callTimeoutMs: 3000, signal: new AbortController().signal });
  try {
    const result = await client.observe({ scope }) as { fixture: { argv: string[] } };
    expect(result.fixture.argv.slice(-4)).toEqual(["--ocr", "auto", "--ocr-recognition-level", "fast"]);
  } finally { await client.close(); }
});
