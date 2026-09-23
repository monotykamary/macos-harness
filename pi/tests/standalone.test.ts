import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import { expect, it } from "vitest";

it("runs outside the sibling layout with installed peers and no Fabric runtime", () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const temp = mkdtempSync(join(tmpdir(), "macos-pi-standalone-"));
  try {
    writeFileSync(join(temp, "package.json"), JSON.stringify({ type: "module" }));
    for (const name of readdirSync(root).filter(name => name.endsWith(".ts"))) {
      const source = readFileSync(join(root, name), "utf8");
      const emitted = transpileModule(source, { compilerOptions: { module: ModuleKind.ESNext, target: ScriptTarget.ES2022 } }).outputText;
      expect(emitted).not.toMatch(/from ["']pi-fabric/);
      expect(source).not.toContain("../../pi-fabric");
      writeFileSync(join(temp, name.replace(/\.ts$/, ".js")), emitted);
    }
    // Only the host's declared TypeBox peer is installed; no Fabric/Pi runtime.
    mkdirSync(join(temp, "node_modules"));
    symlinkSync(join(root, "node_modules/typebox"), join(temp, "node_modules/typebox"), "dir");
    const result = execFileSync(process.execPath, ["--input-type=module", "-e", `
      import assert from "node:assert/strict";
      import extension from "./extension.js";
      let definition;
      extension({ events: { on() {}, emit(name, value) {
        assert.equal(name, "pi-fabric:component:register:v1"); definition = value.component;
      } } });
      assert.equal(definition.name, "macos-harness");
      assert.deepEqual(definition.configSchema.required, ["command", "allowedApps"]);
      let provider;
      await definition.activate({ invocation: { cwd: process.cwd() }, provide(p) { provider = p; } }, {
        command: ["never-start-this-command"], allowedApps: ["Test"]
      });
      assert.equal((await provider.list()).length, 4);
      await provider.close();
      console.log("standalone registration/activation/close: ok");
    `], { cwd: temp, encoding: "utf8", timeout: 10000 });
    expect(result.trim()).toBe("standalone registration/activation/close: ok");
  } finally { rmSync(temp, { recursive: true, force: true }); }
});
