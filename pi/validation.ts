import { Value } from "typebox/value";

/** Local, fail-closed validation; never echo private arguments or diagnostics. */
export function validationMessage(schema: Record<string, unknown>, value: unknown): string | undefined {
  try {
    if (Value.Check(schema, value)) return undefined;
  } catch { /* Invalid schemas and values fail closed. */ }
  return "Schema validation failed";
}
