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

async function monitorRuns(at) {
  const values = [];
  // 30 days to rerun + 35 days to finish + more than the 48-hour freshness window.
  const created = encodeURIComponent(`>=${new Date(at - 70 * 24 * HOUR).toISOString()}`);
  for (let page = 1; page <= 10; page++) {
    const response = await (await get(`${API}/actions/workflows/${MONITOR}/runs?created=${created}&per_page=100&page=${page}`)).json();
    const batch = response.workflow_runs;
    assert(Array.isArray(batch), "Invalid GitHub evidence listing");
    assert(Number.isSafeInteger(response.total_count) && response.total_count >= 0 && response.total_count <= 1000, "Monitor history exceeds the bounded query limit");
    values.push(...batch);
    if (batch.length < 100 || values.length === response.total_count) {
      assert(values.length === response.total_count && new Set(values.map(r => r.id)).size === values.length, "Incomplete or changing monitor history");
      return values;
    }
  }
  throw new Error("Evidence listing exceeded its pagination limit");
}

async function history() {
  const response = await get(`${API}/git/matching-refs/tags/security-history-`);
  assert(!response.headers?.get("link"), "Incomplete history reference index");
  const refs = await response.json();
  assert(Array.isArray(refs), "Invalid history reference index");
  const records = refs.map(r => {
    assert(/^refs\/tags\/security-history-[0-9]{12}$/.test(r.ref), "Invalid history reference");
    const tag = r.ref.slice("refs/tags/".length);
    const sequence = Number(tag.slice("security-history-".length));
    assert(sequence > 0 && ["commit", "tag"].includes(r.object?.type) && /^[a-f0-9]{40}$/.test(r.object?.sha), "Invalid history identity");
    return {sequence, tag, object: r.object};
  }).sort((a, b) => a.sequence - b.sequence);
  assert(new Set(records.map(r => r.sequence)).size === records.length, "Conflicting history sequence");
  const head = records.at(-1);
  if (head) {
    const r = await (await get(`${API}/releases/tags/${head.tag}`)).json();
    assert(r.tag_name === head.tag && r.draft === false && r.immutable === true, "History is not immutable");
    assert(Array.isArray(r.assets) && r.assets.length === 1, "Ambiguous history assets");
    const a = r.assets[0];
    assert(a.name === "catalogue.json" && a.state === "uploaded" && digest(a.digest), "Incomplete history asset");
    assert(Number.isSafeInteger(r.id) && r.id > 0 && Number.isSafeInteger(a.size) && a.size > 0, "Invalid history asset identity");
    Object.assign(head, {sha256: a.digest, id: r.id, source: r.target_commitish, size: a.size});
  }
  return records;
}

async function latestMonitor(at = Date.now()) {
  const runs = (await monitorRuns(at))
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

function unexpectedRecord(entry) {
  return {...entry, registryDigest: entry.digest, state: "unexpected", delivery: "not-approved"};
}

function monitoringEnd(record) {
  if (record.state === "completed" && record.supersededAt) return date(record.supersededAt) + 90 * 24 * HOUR;
  const proof = record.absenceProof;
  if (record.state === "unexpected" && proof?.digest === record.registryDigest && proof.digestMissing === true && date(proof.checkedAt) >= date(record.discoveredAt)) return date(proof.checkedAt);
  if (record.state === "abandoned" && record.publicExposure === "absent" && proof?.digest === record.registryDigest && proof.digestMissing === true && proof.versionTagMissing === true && date(proof.checkedAt) >= date(record.abandonedAt)) {
    const ends = Object.values(record.unexpectedDigests || {}).map(entry => monitoringEnd(unexpectedRecord(entry)));
    if (ends.every(end => end !== null)) return Math.max(date(proof.checkedAt), ...ends);
  }
  return null;
}

function status(record, monitor, at) {
  const end = monitoringEnd(record);
  if (end !== null && at >= end) return "no-longer-monitored";
  const a = record.latestAttempt;
  if (!monitor || !a || a.digest !== record.registryDigest || a.monitorRunId !== String(monitor.id) || a.monitorRunAttempt !== monitor.run_attempt || date(a.assessedAt) > at || date(monitor.updated_at) > at) return "unavailable";
  if (a.outcome === "vulnerable") return "vulnerable";
  if (Object.values(record.unexpectedDigests || {}).some(entry => monitoringEnd(unexpectedRecord(entry)) === null) || record.registryDiscovery?.outcome === "unavailable") return "unavailable";
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
  assert(Array.isArray(value.ancestors) && value.ancestors.every(a => digest(a.sha256)), "Invalid history ancestry");
  assert(same([...value.ancestors.map(a => a.sequence), value.sequence], records.map(r => r.sequence)), "History ancestry is incomplete or conflicting");
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
  const entries = Object.entries(value.catalogue.releases).sort().flatMap(([name, r]) => [[name, r], ...Object.values(r.unexpectedDigests || {}).map(entry => [name, unexpectedRecord(entry)])]);
  for (const [name, r] of entries) {
    assert(/^[a-z][a-z0-9-]*\/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(name) && digest(r.registryDigest), "Invalid release identity");
    const state = status(r, monitor, at);
    states.push(state === "clean" && (r.state !== "completed" || r.delivery !== "complete") ? "incomplete" : state);
    const tr = document.createElement("tr");
    const a = r.latestAttempt;
    const database = a?.database || r.lastKnownVulnerable?.database;
    const databaseText = database ? `Source: ${database.from}; built: ${database.built}; schema: ${database.schemaVersion}; checksum: ${database.checksum || "Unavailable"}${a?.database ? "" : " (last known vulnerable assessment)"}` : "Unavailable";
    const endTime = monitoringEnd(r);
    const end = endTime === null ? "Not scheduled" : new Date(endTime).toISOString();
    const workflow = monitor ? `Workflow ${monitor.id}, attempt ${monitor.run_attempt}: ${monitor.status} / ${monitor.conclusion || "pending"}` : "Workflow unavailable";
    const findings = (r.lastKnownVulnerable?.findings || []).map(f => `${f.id} (${f.severity}); fix: ${f.fix?.state || "unknown"}; versions: ${(f.fix?.versions || []).join(", ") || "not reported"}`).join("; ") || "None recorded";
    const publication = r.unexpectedDigests && Object.keys(r.unexpectedDigests).length ? " / failed: unexpected public bytes" : "";
    const values = [r.state === "unexpected" ? `${name} (unexpected image)` : name, `${r.state} / ${r.delivery}${publication}`, state, a?.assessedAt || "Unavailable", `Outcome: ${a?.outcome || "unavailable"}; ${workflow}`, databaseText, end, findings];
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
    const started = Date.now();
    const first = await history();
    const raw = await (await get("./catalogue.json")).text();
    const value = await verifySnapshot(raw, first);
    const monitor = first.length ? await latestMonitor(started) : null;
    assert(same(first, await history()), "History changed during verification; refresh again");
    if (monitor) assert(same(monitor, await latestMonitor(started)), "Monitor changed during verification; refresh again");
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
