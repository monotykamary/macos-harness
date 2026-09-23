import type { FabricComponentDefinition } from "pi-fabric/protocol";
import { configSchema, type MacOSHarnessConfig } from "./config.js";

/** Registration exposes the complete schema, but loads no resource engine. */
export const macosHarnessComponent: FabricComponentDefinition<MacOSHarnessConfig> = {
  name: "macos-harness",
  description: "Managed app-allowlisted macOS Harness pipe; process starts only on explicit macos.connect",
  configSchema, provides: ["macos"], guarantee: "managed",
  async activate(context, config) {
    const { MacOSHarnessProvider } = await import("./macos.js");
    const provider = new MacOSHarnessProvider(config, undefined, context.invocation.cwd);
    // The managed provider lease owns close; no second component-level disposer.
    try { context.provide(provider); } catch (error) { await provider.close(); throw error; }
  },
};
