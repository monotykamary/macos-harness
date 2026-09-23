import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { FabricComponentDiscovery, FabricComponentRegistration } from "pi-fabric/protocol";
import { macosHarnessComponent } from "./component.js";

/** Literal v1 bus names keep Fabric entirely optional at runtime. */
export default function macosHarnessExtension(pi: ExtensionAPI): void {
  pi.events.on("pi-fabric:component:discover:v1", (value: unknown) => {
    const discovery = value as Partial<FabricComponentDiscovery> | null;
    if (discovery?.version === 1 && typeof discovery.register === "function") {
      discovery.register(macosHarnessComponent, { overwrite: true });
    }
  });
  // Fabric may already have captured this definition before runtime discovery.
  const registration: FabricComponentRegistration = { version: 1, component: macosHarnessComponent, overwrite: true };
  pi.events.emit("pi-fabric:component:register:v1", registration);
}
