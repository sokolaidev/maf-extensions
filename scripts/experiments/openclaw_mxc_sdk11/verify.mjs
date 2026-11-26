/** Check direct creation through the upstream launcher using inert SDK fixtures. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const source = path.resolve(process.argv[2]);
const launcherHash = createHash("sha256").update(readFileSync(source)).digest("hex");
assert.equal(launcherHash, "dd70a621b515c8055368a86f42103dedbec864f5f346855b2213aa9a5c143510");
const root = mkdtempSync(path.join(tmpdir(), "openclaw-sdk11-proof-"));
try {
  const launcher = path.join(root, "launcher.mjs");
  copyFileSync(source, launcher);
  const sdkRoot = path.join(root, "node_modules/@microsoft/mxc-sdk");
  mkdirSync(sdkRoot, { recursive: true });
  writeFileSync(path.join(sdkRoot, "package.json"), JSON.stringify({
    type: "module",
    exports: { "./v1": "./fixture.mjs" },
  }));
  writeFileSync(path.join(sdkRoot, "fixture.mjs"), `
import { appendFileSync } from "node:fs";
const event = (entry) => appendFileSync(process.env.PROOF_EVENTS, JSON.stringify(entry) + "\\n");
export function probe(request) {
  if (request !== undefined) throw new Error("unexpected per-command probe");
  event({ kind: "host-probe" });
  return { tier: "base-container", needsDaclAugmentation: false, warnings: [] };
}
async function create(kind, request) {
  event({ kind, request });
  if (request.environment.CASE === "refused") throw new Error("native creation refused");
  return { wait: async () => ({ exitCode: 7, timedOut: false }), kill() {} };
}
export const spawn = (request) => create("plain", request);
export const spawnWithPty = (request) => create("pty", request);
`);
  const native = path.join(root, "bin", process.arch === "arm64" ? "arm64" : "x64");
  mkdirSync(native, { recursive: true });
  writeFileSync(path.join(native, "wxc-exec.exe"), "inert-executor");
  writeFileSync(path.join(native, "mxc_ffi.dll"), "inert-dll");
  const events = path.join(root, "events.txt");
  const env = {
    ...process.env,
    MXC_BIN_DIR: path.join(root, "bin"),
    MXC_FFI_DIR: native,
    PROOF_EVENTS: events,
  };
  function run(args) {
    writeFileSync(events, "");
    const result = spawnSync(process.execPath, [launcher, ...args], {
      env, encoding: "utf8", timeout: 10_000, windowsHide: true,
    });
    assert.ifError(result.error);
    assert.equal(result.signal, null);
    return {
      ...result,
      events: readFileSync(events, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse),
    };
  }
  const host = run(["--probe"]);
  assert.equal(host.status, 0, host.stderr);
  assert.deepEqual(host.events, [{ kind: "host-probe" }]);
  assert.equal(JSON.parse(host.stdout).probe.tier, "base-container");
  const rows = ["| startup | existing host probe, no creation | passed |"];
  for (const pty of [false, true]) {
    for (const outcome of ["allowed", "refused"]) {
      const request = {
        command: "never-execute",
        environment: { CASE: outcome },
        containment: { type: "processcontainer" },
        timeoutMs: 1000,
      };
      const dir = mkdtempSync(path.join(root, "payload-"));
      const payload = path.join(dir, "payload.json");
      writeFileSync(payload, JSON.stringify({ request, options: { pty } }));
      const result = run(["--payload-file", payload]);
      assert.equal(result.status, outcome === "allowed" ? 7 : 127, result.stderr);
      assert.deepEqual(result.events, [{
        kind: pty ? "pty" : "plain",
        request: { ...request, timeoutMs: pty ? 6000 : 1000 },
      }]);
      if (outcome === "refused") assert.match(result.stderr, /native creation refused/);
      rows.push(`| ${pty ? "PTY" : "plain"} | ${outcome}; one creation, no request probe or retry | passed |`);
    }
  }
  console.log(`# Direct MXC creation fixture proof\n\nNode: ${process.version}; platform: ${process.platform}.\nLauncher SHA-256: ${launcherHash}.\n\nInert SDK/native files only. No MXC library or workload executed.\n\n| Path | Contract | Result |\n| --- | --- | --- |\n${rows.join("\n")}\n`);
} finally {
  rmSync(root, { recursive: true, force: true });
}
