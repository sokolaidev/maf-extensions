"use strict";

const REPOSITORY = "sokolaidev/maf-extensions";
const API = `https://api.github.com/repos/${REPOSITORY}`;
const MONITOR = "container-image-monitor.yml";
const HOUR = 3600000;
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const date = value => { const n = Date.parse(value); assert(Number.isFinite(n), "Invalid evidence time"); return n; };
const digest = value => typeof value === "string" && /^sha256:[a-f0-9]{64}$/.test(value);

async function get(url) {
  const response = await fetch(url, {cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(20000)});
  assert(response.ok, "GitHub evidence unavailable (including API rate limits)");
  return response;
}

async function list(path, field = null) {
  const values = [];
  for (let page = 1; page <= 10000; page++) {
    const response = await (await get(`${API}/${path}?per_page=100&page=${page}`)).json();
    const batch = field ? response[field] : response;
    assert(Array.isArray(batch), "Invalid GitHub evidence listing");
    values.push(...batch);
    if (batch.length < 100) {
      if (field) assert(values.length === response.total_count, "Incomplete or changing monitor history");
      return values;
    }
  }
  throw new Error("Evidence listing exceeded its pagination limit");
}

async function history() {
  const releases = (await list("releases")).filter(r => !r.draft && r.tag_name.startsWith("security-history-"));
  const records = releases.map(r => {
    assert(/^security-history-[0-9]{12}$/.test(r.tag_name) && r.immutable === true, "History is not immutable");
    assert(Array.isArray(r.assets) && r.assets.length === 1, "Ambiguous history assets");
    const a = r.assets[0];
    assert(a.name === "catalogue.json" && a.state === "uploaded" && digest(a.digest), "Incomplete history asset");
    const sequence = Number(r.tag_name.slice("security-history-".length));
    assert(sequence > 0 && Number.isSafeInteger(r.id), "Invalid history identity");
    return {sequence, sha256: a.digest, id: r.id, source: r.target_commitish, tag: r.tag_name, size: a.size};
  }).sort((a, b) => a.sequence - b.sequence);
  assert(new Set(records.map(r => r.sequence)).size === records.length, "Conflicting history sequence");
  return records;
}

async function latestMonitor() {
  const runs = (await list(`actions/workflows/${MONITOR}/runs`, "workflow_runs"))
    .filter(r => ["schedule", "workflow_dispatch", "workflow_run"].includes(r.event) && r.head_branch === "main");
  for (const run of runs) {
    assert(run.path === `.github/workflows/${MONITOR}` && run.head_repository?.full_name === REPOSITORY, "Unexpected monitor source");
    assert(Number.isSafeInteger(run.id) && Number.isSafeInteger(run.run_attempt), "Invalid monitor attempt");
    date(run.updated_at);
  }
  runs.sort((a, b) => date(b.updated_at) - date(a.updated_at) || b.id - a.id || b.run_attempt - a.run_attempt);
  assert(runs.length > 0, "No authoritative monitoring attempt exists");
  assert(runs.length === 1 || date(runs[0].updated_at) !== date(runs[1].updated_at), "Latest monitor attempts have ambiguous update times");
  const r = runs[0];
  return {id: r.id, run_attempt: r.run_attempt, status: r.status, conclusion: r.conclusion, updated_at: r.updated_at};
}

function status(record, monitor, at) {
  if (record.state === "completed" && record.supersededAt && at >= date(record.supersededAt) + 90 * 24 * HOUR) return "no-longer-monitored";
  const proof = record.absenceProof;
  if (record.state === "abandoned" && record.publicExposure === "absent" && proof?.digest === record.registryDigest && proof.digestMissing === true && proof.versionTagMissing === true && date(proof.checkedAt) >= date(record.abandonedAt) && at >= date(proof.checkedAt)) return "no-longer-monitored";
  const a = record.latestAttempt;
  if (!monitor || !a || a.digest !== record.registryDigest || a.monitorRunId !== String(monitor.id) || a.monitorRunAttempt !== monitor.run_attempt || date(a.assessedAt) > at || date(monitor.updated_at) > at) return "unavailable";
  if (a.outcome === "vulnerable") return "vulnerable";
  if (monitor.status !== "completed" || monitor.conclusion !== "success" || a.outcome !== "clean") return "unavailable";
  return at - date(a.assessedAt) >= 48 * HOUR ? "stale" : "clean";
}

async function verifySnapshot(raw, records) {
  const value = JSON.parse(raw);
  const head = records.at(-1);
  if (!head) {
    assert(value.sequence === 0 && Object.keys(value.catalogue.releases).length === 0, "Deployed catalogue is no longer authoritative");
    return value;
  }
  const bytes = new TextEncoder().encode(raw);
  const hashed = "sha256:" + Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
  assert(hashed === head.sha256 && bytes.length === head.size && value.sequence === head.sequence && value.sourceCommit === head.source, "Pages snapshot differs from the latest immutable catalogue");
  assert(same([...(value.ancestors || []), {sequence: value.sequence, sha256: hashed}], records.map(r => ({sequence: r.sequence, sha256: r.sha256}))), "History ancestry is incomplete or conflicting");
  assert(same(value.previous, value.ancestors.at(-1) || null), "Invalid history predecessor");
  let object = (await (await get(`${API}/git/ref/tags/${head.tag}`)).json()).object;
  for (let n = 0; n < 8 && object?.type === "tag"; n++) {
    assert(/^[a-f0-9]{40}$/.test(object.sha), "Invalid history tag");
    object = (await (await get(`${API}/git/tags/${object.sha}`)).json()).object;
  }
  assert(object?.type === "commit" && object.sha === value.sourceCommit, "History tag has a different source");
  return value;
}

function render(value, monitor, at) {
  const rows = document.getElementById("releases");
  rows.replaceChildren();
  const states = [];
  for (const [name, r] of Object.entries(value.catalogue.releases).sort()) {
    assert(/^[a-z][a-z0-9-]*\/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(name) && digest(r.registryDigest), "Invalid release identity");
    const state = status(r, monitor, at);
    states.push(state === "clean" && (r.state !== "completed" || r.delivery !== "complete") ? "incomplete" : state);
    const tr = document.createElement("tr");
    const values = [name, `${r.state} / ${r.delivery}`, state, r.latestAttempt?.assessedAt || "Unavailable", (r.lastKnownVulnerable?.findings || []).map(f => `${f.id} (${f.severity})`).join(", ") || "None recorded"];
    for (const [index, text] of values.entries()) {
      const td = document.createElement("td");
      td.textContent = text;
      if (index === 0) { const p = document.createElement("p"); const code = document.createElement("code"); code.textContent = r.registryDigest; p.append(code); td.append(p); }
      if (index === 2) td.className = state;
      tr.append(td);
    }
    rows.append(tr);
  }
  const active = states.filter(s => s !== "no-longer-monitored");
  document.getElementById("summary").textContent = states.length === 0 ? "No image releases recorded under this contract." : active.length === 0 ? "No images currently monitored." : active.every(s => s === "clean") ? "All active images have completed release delivery and fresh scans with no known High/Critical findings at the time checked." : "Security evidence needs attention. Review each image's delivery and monitoring status below.";
}

let busy = false;
let verified = null;
async function refresh() {
  if (busy) return;
  busy = true;
  verified = null;
  document.getElementById("summary").textContent = "Checking authoritative GitHub evidence…";
  try {
    const first = await history();
    const raw = await (await get("./catalogue.json")).text();
    const value = await verifySnapshot(raw, first);
    const monitor = first.length ? await latestMonitor() : null;
    assert(same(first, await history()), "History changed during verification; refresh again");
    if (monitor) assert(same(monitor, await latestMonitor()), "Monitor changed during verification; refresh again");
    const at = Date.now();
    render(value, monitor, at);
    verified = {value, monitor, at};
    document.getElementById("checked").textContent = `Live evidence checked ${new Date(at).toISOString()}. Status is as of that check; refresh to detect later attempts.`;
  } catch (error) {
    document.getElementById("summary").textContent = `Monitoring status unavailable: ${error.message}. A cached page cannot establish current status.`;
    document.getElementById("releases").replaceChildren();
    document.getElementById("checked").textContent = "Current evidence could not be verified. Use GitHub Releases and the consumer verifier to inspect retained findings.";
  } finally { busy = false; }
}

if (typeof document !== "undefined") {
  document.getElementById("refresh").addEventListener("click", refresh);
  refresh();
  setInterval(() => { if (verified) render(verified.value, verified.monitor, Date.now()); }, 60000);
}
if (typeof module !== "undefined") module.exports = {status, verifySnapshot, history, latestMonitor};
