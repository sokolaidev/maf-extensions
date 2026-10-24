/** Audit a pinned upstream launcher and a preflight proposal with an inert SDK. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const REVISION = "0cbf9b5481de42e1ea4207c1b27fd50441e51c0b";
const SOURCE_SHA256 = "dd70a621b515c8055368a86f42103dedbec864f5f346855b2213aa9a5c143510";
const insertion = "    const spawned = options.pty\n";
const proposal = `    const admission = sdk.probe(spawnRequest);
    if (
      !admission || typeof admission !== "object" || Array.isArray(admission) ||
      admission.tier !== "base-container" || admission.needsDaclAugmentation !== false ||
      "error" in admission || !Array.isArray(admission.warnings) ||
      !admission.warnings.every((warning) => typeof warning === "string")
    ) {
      throw new Error("BaseContainer request preflight refused");
    }
`;

const sourcePath = process.argv[2];
assert.equal(process.argv.length, 3, "Supply the unmodified pinned launcher source path");
const source = readFileSync(sourcePath);
assert.equal(createHash("sha256").update(source).digest("hex"), SOURCE_SHA256);
const text = source.toString("utf8");
assert.equal(text.split(insertion).length, 2, "Proposal insertion must be unique");
const modules = {
  upstream: text,
  proposal: text.replace(insertion, proposal + insertion),
};
const cases = {
  base: () => ({ tier: "base-container", needsDaclAugmentation: false, warnings: [] }),
  dacl: () => ({ tier: "appcontainer-dacl", needsDaclAugmentation: true, warnings: [] }),
  bfs: () => ({ tier: "appcontainer-bfs", needsDaclAugmentation: false, warnings: [] }),
  absent: () => ({ error: "unsupported", warnings: [] }),
  malformed: () => null,
  contradictory: () => ({ tier: "base-container", needsDaclAugmentation: false, warnings: [], error: "failed" }),
  augmentation: () => ({ tier: "base-container", needsDaclAugmentation: true, warnings: [] }),
  warnings: () => ({ tier: "base-container", needsDaclAugmentation: false, warnings: "invalid" }),
  error: () => { throw new Error("probe failed"); },
};
const rows = [];
for (const [variant, contents] of Object.entries(modules)) {
  const { launchSandbox } = await import("data:text/javascript;base64," + Buffer.from(contents).toString("base64"));
  for (const pty of [false, true]) {
    for (const [name, result] of Object.entries(cases)) {
      let probes = 0;
      let spawns = 0;
      let probed;
      let launched;
      const request = { command: "audit-only-never-executed", containment: { type: "processcontainer" }, timeoutMs: 100 };
      const initial = structuredClone(request);
      const spawn = async (value) => {
        spawns++;
        launched = structuredClone(value);
        return { wait: async () => ({ exitCode: 0 }), kill() {} };
      };
      const sdk = {
        probe(value) { probes++; probed = structuredClone(value); return result(); },
        spawn,
        spawnWithPty: spawn,
      };
      const signals = { attach() {}, signaled: () => false, exitCode: () => undefined };
      const io = { stdin: { resume() {}, pause() {} }, stdout: { write() {} }, stderr: { write() {} } };
      let failure;
      try {
        assert.equal(await launchSandbox(sdk, request, { pty }, signals, io), 0);
      } catch (error) {
        failure = error;
      }
      const allowed = variant === "upstream" || name === "base";
      assert.equal(probes, variant === "upstream" ? 0 : 1);
      assert.equal(spawns, Number(allowed));
      assert.equal(failure !== undefined, !allowed);
      assert.deepEqual(request, initial, "Caller request must remain unchanged");
      if (variant === "proposal" && allowed) {
        assert.deepEqual(probed, launched, "Probe must receive the effective spawn request, including PTY timeout");
      }
      rows.push(`| ${variant} | ${pty ? "PTY" : "plain"} | ${name} | ${probes} | ${spawns} | ${allowed ? "mock completed" : "refused"} |`);
    }
    let attempts = 0;
    const error = new Error("native creation refused");
    const fail = async () => { attempts++; throw error; };
    await assert.rejects(
      launchSandbox({ probe: cases.base, spawn: fail, spawnWithPty: fail },
        { command: "audit-only-never-executed" }, { pty },
        { attach() {}, signaled: () => false, exitCode: () => undefined }, {}),
      (observed) => observed === error,
    );
    assert.equal(attempts, 1, "Creation refusal must not trigger a second spawn attempt");
  }
}
console.log(`# OpenClaw MXC request-admission source audit

Source revision: ${REVISION}.
Launcher SHA-256: ${SOURCE_SHA256}.
Node: ${process.version}.

Only the pinned launcher JavaScript was executed, with inert SDK/process/stdio doubles. No SDK/native library was loaded, no workload was launched and no package was installed. The proposal is an in-memory source edit, not an installed-plugin patch or qualification result. A mock spawn count is a call to a test double, not a native process.

| Variant | Path | Injected probe result | Probe calls | Mock spawn calls | Outcome |
|---|---|---|---|---|---|
${rows.join("\n")}

All 36 admission cases and four creation-refusal cases passed. The proposal probes the effective request on plain and PTY paths; fallback/error results refuse before the mock spawn. Creation errors propagate without retry. Probe hangs/deadlines, actual SDK validation, runtime mode changes, native identities and containment were not tested. The proposal does not enforce BaseContainer at native creation and is not ready for production adoption.
`);
