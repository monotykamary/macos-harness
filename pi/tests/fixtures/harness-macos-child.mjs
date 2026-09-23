import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
const mode = process.argv[2];
// Inherit the bridge's private process group; never detach this descendant.
const descendant = mode === "descendant"
  ? spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" })
  : undefined;
let sequence = 0;
let handling = false;
const lines = createInterface({ input: process.stdin });
const send = value => process.stdout.write(JSON.stringify(value) + "\n");
lines.on("line", line => {
  const request = JSON.parse(line);
  const { id, method, args } = request;
  if (handling) { send({ id, error: { code: "concurrent", message: "Requests were not serialized" } }); return; }
  const observation = {
    scope: args.scope, observationId: `observation-${++sequence}`, revision: `revision-${sequence}`,
    candidates: [{ id: "button-1", role: "button", label: "Save", operations: ["press"] }], truncated: false,
    fixture: { pid: process.pid, cwd: process.cwd(), argv: process.argv.slice(3), sequence, requestId: id, ...(descendant ? { descendantPid: descendant.pid } : {}) },
  };
  const reply = () => {
    handling = false;
    send({ id, result: method === "act" ? { status: "executed" } : method === "waitForChange" ? { changed: true, observation } : observation });
  };
  // Explicit probe gets the PID for post-failure cleanup assertions.
  if (args.maxElements === 1) { reply(); return; }
  switch (mode) {
    case "hang": break;
    case "eof": process.stdout.end(); break;
    case "boundary":
    case "boundary-over": {
      observation.padding = "";
      const empty = JSON.stringify({ id, result: observation }) + "\n";
      observation.padding = "x".repeat(128 * 1024 - Buffer.byteLength(empty) + Number(mode === "boundary-over"));
      reply();
      break;
    }
    case "delay": handling = true; setTimeout(reply, 40); break;
    case "exit": process.exit(17); break;
    case "malformed": process.stdout.write("not JSON: PRIVATE_DIAGNOSTIC\n"); break;
    case "oversized": process.stdout.write("x".repeat(128 * 1024 + 1)); break;
    case "wrong-id": send({ id: id + 1, result: observation }); break;
    case "bad-envelope": send({ id, result: observation, error: { code: "bad", message: "PRIVATE_DIAGNOSTIC" } }); break;
    case "bad-result": send({ id, result: { status: "success", private: "PRIVATE_DIAGNOSTIC" } }); break;
    case "bad-scope": send({ id, result: { ...observation, scope: { app: "Forbidden" } } }); break;
    case "bad-utf8": process.stdout.write(Buffer.from([0xff, 10])); break;
    case "error": send({ id, error: { code: "private", message: "PRIVATE_DIAGNOSTIC" } }); break;
    case "partial": {
      const frame = Buffer.from(JSON.stringify({ id, result: observation }) + "\n");
      process.stdout.write(frame.subarray(0, 7));
      setTimeout(() => process.stdout.write(frame.subarray(7)), 5);
      break;
    }
    case "stderr": process.stderr.write("PRIVATE_DIAGNOSTIC\n"); reply(); break;
    default: reply();
  }
});
