/** Exercise the supplied launcher with inert SDK/native fixtures, never MXC. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, watch, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";

const source = path.resolve(process.argv[2]);
const baseline = process.argv.includes("--baseline");
const cleanupRegression = process.argv.includes("--cleanup-regression");
const launcherSha256 = createHash("sha256").update(readFileSync(source)).digest("hex");
if (baseline) assert.equal(launcherSha256, "dd70a621b515c8055368a86f42103dedbec864f5f346855b2213aa9a5c143510");
const root = mkdtempSync(path.join(tmpdir(), "openclaw-preflight-proof-"));
const launcher = path.join(root, "launcher.mjs");
copyFileSync(source, launcher);
if (cleanupRegression) {
  const text = readFileSync(launcher, "utf8");
  const deadline = "const PROBE_TIMEOUT_MS = 30_000;";
  assert.equal(text.split(deadline).length, 2);
  writeFileSync(launcher, text.replace(deadline, "const PROBE_TIMEOUT_MS = 120_000;"));
}
const sdkRoot = path.join(root, "node_modules/@microsoft/mxc-sdk");
mkdirSync(sdkRoot, { recursive: true });
writeFileSync(path.join(sdkRoot, "package.json"), JSON.stringify({ type: "module", exports: { "./v1": "./fixture.mjs" } }));
const arch = process.arch === "arm64" ? "arm64" : "x64";
const native = path.join(root, "bin", arch);
mkdirSync(native, { recursive: true });
const events = path.join(root, "events.txt");
const probePid = path.join(root, "probe-pid.txt");
writeFileSync(path.join(sdkRoot, "fixture.mjs"), `
import { appendFileSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
const event = (value) => appendFileSync(process.env.PROOF_EVENTS, JSON.stringify(value) + "\\n");
export function probe(request) {
  event({ kind: "probe", request });
  writeFileSync(process.env.PROOF_PID + ".pending", String(process.pid));
  renameSync(process.env.PROOF_PID + ".pending", process.env.PROOF_PID);
  switch (request.environment.CASE) {
    case "fallback": return { tier: "appcontainer-dacl", needsDaclAugmentation: true, warnings: [] };
    case "null": return null;
    case "error": throw new Error(request.environment.SECRET);
    case "contradictory": return { tier: "base-container", needsDaclAugmentation: false, warnings: [], error: "failed" };
    case "malformed": process.stdout.write("not-json"); process.exit(0);
    case "oversized": process.stdout.write("x".repeat(128 * 1024)); break;
    case "changed": appendFileSync(path.join(process.env.MXC_FFI_DIR, "mxc_ffi.dll"), "changed"); break;
    case "changed-executor": appendFileSync(path.join(process.env.MXC_FFI_DIR, "wxc-exec.exe"), "changed"); break;
    case "hang": Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0); break;
  }
  return { tier: "base-container", needsDaclAugmentation: false, warnings: [] };
}
async function create(request) {
  event({ kind: "spawn", request });
  if (request.environment.CASE === "creation-error") throw new Error("native creation refused");
  return { wait: async () => ({ exitCode: 0 }), kill() {} };
}
export const spawn = create;
export const spawnWithPty = create;
`);
const env = {
  ...process.env,
  MXC_BIN_DIR: path.join(root, "bin"),
  MXC_FFI_DIR: native,
  PROOF_EVENTS: events,
  PROOF_PID: probePid,
};
const rows = [];
let probeHasExited = false;
function reset() {
  probeHasExited = false;
  writeFileSync(path.join(native, "mxc_ffi.dll"), "inert-dll");
  writeFileSync(path.join(native, "wxc-exec.exe"), "inert-executor");
  writeFileSync(events, "");
  rmSync(probePid, { force: true });
}
function request(name) {
  return {
    command: "never-execute secret-command",
    environment: { CASE: name, SECRET: "do-not-disclose-secret-value" },
    containment: { type: "processcontainer" },
    workingDirectory: "synthetic-private-path",
    filesystem: { readonlyPaths: ["synthetic-private-path"], readwritePaths: [] },
    timeoutMs: 1000,
  };
}
function history() {
  return readFileSync(events, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}
function ownedProbePid() {
  const pid = Number(readFileSync(probePid, "utf8"));
  assert.ok(Number.isSafeInteger(pid) && pid > 0 && pid !== process.pid, "Invalid fixture probe PID");
  return pid;
}
function assertGone() {
  const pid = ownedProbePid();
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  probeHasExited = true;
}
function isAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === "ESRCH") return false; throw error; }
}
async function stopOwnedProbe() {
  if (probeHasExited || !existsSync(probePid)) return;
  const pid = ownedProbePid();
  try { process.kill(pid, "SIGKILL"); }
  catch (error) { if (error.code !== "ESRCH") throw error; }
  const deadline = Date.now() + 5000;
  while (isAlive(pid) && Date.now() < deadline) await delay(10);
  assertGone();
}
async function withWatchdog(operation, cleanup, timeoutMs = 40_000) {
  let timer;
  let expired = false;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      expired = true;
      cleanup().then(() => reject(new Error("Replay watchdog expired")), reject);
    }, timeoutMs);
  });
  try {
    let result;
    try { result = await Promise.race([operation, timeout]); }
    catch (error) { if (expired) await timeout; throw error; }
    if (expired) await timeout;
    return result;
  } finally {
    clearTimeout(timer);
  }
}
async function run(name, pty = false, watchdogMs = 40_000) {
  reset();
  const dir = mkdtempSync(path.join(root, "payload-"));
  const file = path.join(dir, "payload.json");
  writeFileSync(file, JSON.stringify({ request: request(name), options: { pty, debug: true } }));
  const child = spawn(process.execPath, [launcher, "--payload-file", file], { env, stdio: ["ignore", "pipe", "pipe"] });
  const stdout = [], stderr = [];
  child.stdout.on("data", (x) => stdout.push(x));
  child.stderr.on("data", (x) => stderr.push(x));
  const closed = once(child, "close");
  const cleanup = async () => {
    try { await stopOwnedProbe(); }
    finally { child.kill("SIGKILL"); await closed; }
  };
  try {
    const [code] = await withWatchdog(closed, cleanup, watchdogMs);
    return { code, stderr: Buffer.concat(stderr).toString(), stdout: Buffer.concat(stdout).toString(), events: history() };
  } catch (error) {
    await cleanup();
    throw error;
  }
}
try {
  if (cleanupRegression) {
    await assert.rejects(run("hang", false, 2000), /Replay watchdog expired/);
    assertGone();
    assert.equal(history().filter((x) => x.kind === "spawn").length, 0);
    rows.push(`| fixture | copied launcher deadline extended; SHA-256 ${createHash("sha256").update(readFileSync(launcher)).digest("hex")} | control |`);
    rows.push("| watchdog | delayed supervisor; fixture probe terminated and exit verified | passed |");
  } else if (baseline) {
    const result = await run("fallback");
    assert.equal(result.code, 0);
    assert.equal(result.events.filter((x) => x.kind === "probe").length, 0);
    assert.equal(result.events.filter((x) => x.kind === "spawn").length, 1);
    rows.push("| baseline | fallback bypasses request probe and reaches inert spawn | reproduced |");
  } else {
    for (const pty of [false, true]) {
      const result = await run("allowed", pty);
      assert.equal(result.code, 0, result.stderr);
      assert.equal(result.events.length, 2);
      const [probed, spawned] = result.events;
      assert.equal(probed.kind, "probe");
      assert.equal(spawned.kind, "spawn");
      assert.deepEqual(probed.request, spawned.request);
      assert.equal(spawned.request.timeoutMs, pty ? 6000 : 1000);
      const line = result.stderr.split("\n").find((x) => x.startsWith("[mxc] preflight "));
      const record = JSON.parse(line.slice("[mxc] preflight ".length));
      assert.equal(record.requestSha256, createHash("sha256").update(JSON.stringify(spawned.request)).digest("hex"));
      assert.equal(record.pty, pty);
      assert.equal(record.environmentVariableCount, 2);
      assert.equal(record.readonlyPathCount, 1);
      assert.equal(record.native.nativeLibrarySha256, createHash("sha256").update("inert-dll").digest("hex"));
      for (const secret of ["secret-command", "do-not-disclose", "synthetic-private-path", root]) assert.ok(!result.stderr.includes(secret));
      assertGone();
      rows.push(`| ${pty ? "PTY" : "plain"} | same effective request; bounded diagnostic; probe exited | passed |`);
    }
    for (const name of ["fallback", "null", "error", "contradictory", "malformed", "oversized", "changed", "changed-executor", "hang", "creation-error"]) {
      const result = await run(name);
      assert.equal(result.code, 127, `${name}: ${result.stderr}`);
      assert.equal(result.events.filter((x) => x.kind === "spawn").length, name === "creation-error" ? 1 : 0);
      assert.ok(!result.stderr.includes("do-not-disclose-secret-value"));
      if (name === "hang") assert.match(result.stderr, /preflight timed out/);
      assertGone();
      rows.push(`| plain | ${name}; probe exited; no retry | passed |`);
    }
    // The abort starts only after the synchronous fixture probe announces its PID.
    reset();
    const previous = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
    Object.assign(process.env, env);
    const { probeRequest } = await import(pathToFileURL(launcher));
    const abort = new AbortController();
    const observer = watch(root, () => { if (existsSync(probePid)) abort.abort(); });
    try {
      await assert.rejects(
        withWatchdog(probeRequest(request("hang"), abort.signal), stopOwnedProbe),
        /preflight cancelled/,
      );
      assertGone();
      assert.equal(history().filter((x) => x.kind === "spawn").length, 0);
    } finally {
      observer.close();
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
    }
    rows.push("| supervisor | abort synchronous probe; join child exit | passed |");
  }
  console.log(`# MXC preflight launcher proof\n\nNode: ${process.version}; platform: ${process.platform}.\nLauncher SHA-256: ${launcherSha256}.\n\nInert SDK and synthetic native files only. No MXC library or workload executed.\n\n| Path | Contract | Result |\n| --- | --- | --- |\n${rows.join("\n")}\n`);
} finally {
  await stopOwnedProbe();
  rmSync(root, { recursive: true, force: true });
}
