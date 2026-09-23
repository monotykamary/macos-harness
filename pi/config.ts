export interface MacOSHarnessConfig {
  /** Trusted argv prefix, never a shell command. */
  command: string[];
  allowedApps: string[];
  callTimeoutMs?: number;
}
export const configSchema: Record<string, unknown> = {
  type: "object",
  properties: {
    command: {
      type: "array", minItems: 1, maxItems: 32,
      items: { type: "string", minLength: 1, maxLength: 4096, pattern: "^[^\\u0000-\\u001f\\u007f]+$" },
      description: "Trusted argv prefix; host appends serve --app APP for each grant, without a shell. Spawn cwd is the connect invocation cwd, falling back to the component activation cwd.",
    },
    allowedApps: {
      type: "array", minItems: 1, maxItems: 32, uniqueItems: true,
      items: { type: "string", minLength: 1, maxLength: 256, pattern: "^[^\\u0000-\\u0020\\u007f](?:[^\\u0000-\\u001f\\u007f]*[^\\u0000-\\u0020\\u007f])?$" },
      description: "Exact application names or bundle identifiers granted to this connection. No automatic app discovery or permission approval.",
    },
    callTimeoutMs: { type: "integer", minimum: 100, maximum: 60000, default: 10000, description: "Connect and call deadline. Timeout/cancellation kills the child and invalidates all handles; explicitly reconnect. Default 10000 ms." },
  },
  required: ["command", "allowedApps"], additionalProperties: false,
};
