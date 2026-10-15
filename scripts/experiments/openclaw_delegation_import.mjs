import fs from "node:fs";
import fsPromises from "node:fs/promises";
import net from "node:net";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";

// Discovery is observed without calling the credential helper or starting a Gateway.
const attempts = [];
const deny = (operation) => () => {
  attempts.push(operation);
  const error = new Error("probe refused import side effect");
  error.code = "PROBE_SIDE_EFFECT";
  throw error;
};
for (const name of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"]) {
  childProcess[name] = deny(`process.${name}`);
}
net.Socket.prototype.connect = deny("network.connect");
net.Server.prototype.listen = deny("network.listen");
globalThis.fetch = deny("network.fetch");
for (const name of ["writeFile", "appendFile", "mkdir", "rm", "rename", "unlink"]) {
  fs[name] = deny(`files.${name}`);
  fs[`${name}Sync`] = deny(`files.${name}Sync`);
  fsPromises[name] = deny(`files.${name}`);
}
syncBuiltinESMExports();

const imports = [];
for (const specifier of ["openclaw/plugin-sdk/plugin-entry", "openclaw/plugin-sdk/secret-egress-runtime"]) {
  try {
    const loaded = await import(specifier);
    imports.push({ specifier, status: "imported", exports: Object.keys(loaded).sort() });
  } catch (error) {
    imports.push({
      specifier,
      status: "failed",
      code: typeof error?.code === "string" && /^[A-Z_]+$/.test(error.code) ? error.code : "IMPORT_FAILED",
    });
  }
}
await new Promise((resolve) => setImmediate(resolve));
process.stdout.write(JSON.stringify({ imports, side_effect_attempts: attempts }));
