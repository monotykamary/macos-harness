import { expect, it } from "vitest";
import { validateInteractionResult } from "../contract.js";

it("preserves bounded checkbox/value evidence and rejects malformed or duplicate handles", () => {
  const observation = { scope: { app: "Test" }, observationId: "o1", revision: "r1", candidates: [{ id: "c1", role: "AXCheckBox", label: "Enabled", operations: ["press"], value: "1", checked: true }] };
  expect(validateInteractionResult("observe", observation)).toBe(observation);
  const wait = { changed: false, observation };
  expect(validateInteractionResult("waitForChange", wait)).toBe(wait);
  for (const candidate of [{ ...observation.candidates[0], checked: "true" }, { ...observation.candidates[0], value: "x".repeat(4097) }]) {
    expect(() => validateInteractionResult("observe", { ...observation, candidates: [candidate] })).toThrow("contract");
  }
  expect(() => validateInteractionResult("observe", { ...observation, candidates: [...observation.candidates, ...observation.candidates] })).toThrow("Duplicate");
  expect(() => validateInteractionResult("observe", { ...observation, extra: "x".repeat(128 * 1024) })).toThrow("size");
});
