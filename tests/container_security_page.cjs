const {test} = require('node:test');
const assert = require('node:assert/strict');
const {status, verifySnapshot, history, latestMonitor} = require('../docs/security/status/status.js');
const at = Date.parse('2026-01-02T00:00:00Z');
const digest = 'sha256:' + 'b'.repeat(64);
const run = {id: 123, run_attempt: 1, status: 'completed', conclusion: 'success', updated_at: '2026-01-01T12:00:00Z'};
const record = {state: 'completed', registryDigest: digest, latestAttempt: {digest, monitorRunId: '123', monitorRunAttempt: 1, assessedAt: '2026-01-01T00:00:00Z', outcome: 'clean'}};

test('fresh clean status requires the actual latest successful attempt', () => {
  assert.equal(status(record, run, at), 'clean');
  assert.equal(status(record, {...run, run_attempt: 2}, at), 'unavailable');
  assert.equal(status(record, {...run, conclusion: 'failure'}, at), 'unavailable');
  assert.equal(status(record, {...run, status: 'in_progress'}, at), 'unavailable');
  assert.equal(status(record, run, at + 24 * 3600000), 'stale');
  assert.equal(status(record, run, at - 48 * 3600000), 'unavailable');
});

test('known current findings survive workflow failure', () => {
  const vulnerable = {...record, latestAttempt: {...record.latestAttempt, outcome: 'vulnerable'}};
  assert.equal(status(vulnerable, {...run, conclusion: 'failure'}, at), 'vulnerable');
  assert.equal(status(vulnerable, {...run, run_attempt: 2}, at), 'unavailable');
});

test('retirement needs expiry or an abandoned candidate with explicit absence proof', () => {
  const old = {...record, supersededAt: '2025-01-01T00:00:00Z'};
  assert.equal(status(old, run, at), 'no-longer-monitored');
  assert.equal(status({...old, state: 'incomplete'}, run, at), 'clean');
  const abandoned = {...record, state: 'abandoned', publicExposure: 'absent', abandonedAt: '2025-01-01T00:00:00Z', absenceProof: {digest, checkedAt: '2026-01-01T00:00:00Z', digestMissing: true, versionTagMissing: true}};
  assert.equal(status(abandoned, run, at), 'no-longer-monitored');
  assert.equal(status({...abandoned, absenceProof: {...abandoned.absenceProof, versionTagMissing: false}}, run, at), 'clean');
});

test('stale deployed catalogue cannot establish current status', async () => {
  const raw = JSON.stringify({sequence: 1});
  await assert.rejects(verifySnapshot(raw, [{sequence: 2, sha256: digest, size: raw.length}]), /differs/);
});

test('history binds exact bytes, ancestry and the actual Git tag', async () => {
  const source = 'a'.repeat(40);
  const document = {sequence: 1, sourceCommit: source, ancestors: [], previous: null};
  const raw = JSON.stringify(document);
  const hash = 'sha256:' + require('node:crypto').createHash('sha256').update(raw).digest('hex');
  const records = [{sequence: 1, sha256: hash, size: raw.length, source, tag: 'security-history-000000000001'}];
  global.fetch = async () => ({ok: true, json: async () => ({object: {type: 'commit', sha: source}})});
  assert.deepEqual(await verifySnapshot(raw, records), document);
  global.fetch = async () => ({ok: true, json: async () => ({object: {type: 'commit', sha: 'c'.repeat(40)}})});
  await assert.rejects(verifySnapshot(raw, records), /different source/);
  await assert.rejects(verifySnapshot(raw, [{sequence: 0, sha256: digest}, ...records]), /ancestry/);
});

test('failed GitHub request and mutable history cannot leave green evidence', async () => {
  global.fetch = async () => ({ok: false});
  await assert.rejects(history(), /unavailable/);
  global.fetch = async url => ({ok: true, json: async () => url.includes('matching-refs') ? [{ref: 'refs/tags/security-history-000000000001', object: {type: 'commit', sha: 'a'.repeat(40)}}] : {tag_name: 'security-history-000000000001', draft: false, immutable: false}});
  await assert.rejects(history(), /not immutable/);
});

test('a rerun of an older monitor beats creation order', async () => {
  const common = {event: 'schedule', head_branch: 'main', path: '.github/workflows/container-image-monitor.yml', head_repository: {full_name: 'sokolaidev/maf-extensions'}, status: 'completed', conclusion: 'success'};
  global.fetch = async () => ({ok: true, json: async () => ({total_count: 2, workflow_runs: [
    {...common, id: 200, run_attempt: 1, updated_at: '2026-01-01T00:00:00Z'},
    {...common, id: 100, run_attempt: 2, updated_at: '2026-01-02T00:00:00Z', conclusion: 'failure'},
  ]})});
  const latest = await latestMonitor();
  assert.equal(latest.id, 100);
  assert.equal(latest.conclusion, 'failure');
});

test('same-second attempts cannot be ordered by creation ID', async () => {
  const common = {event: 'schedule', head_branch: 'main', path: '.github/workflows/container-image-monitor.yml', head_repository: {full_name: 'sokolaidev/maf-extensions'}, status: 'completed', updated_at: '2026-01-02T00:00:00Z'};
  global.fetch = async () => ({ok: true, json: async () => ({total_count: 2, workflow_runs: [
    {...common, id: 200, run_attempt: 1, conclusion: 'success'},
    {...common, id: 100, run_attempt: 2, conclusion: 'failure'},
  ]})});
  await assert.rejects(latestMonitor(), /ambiguous update times/);
});

test('browser history lookup has constant request count across years of releases', async () => {
  const source = 'a'.repeat(40);
  const refs = Array.from({length: 10000}, (_, i) => ({ref: `refs/tags/security-history-${String(i + 1).padStart(12, '0')}`, object: {type: 'commit', sha: source}}));
  const requests = [];
  global.fetch = async url => {
    requests.push(url);
    if (url.endsWith('/git/matching-refs/tags/security-history-')) return {ok: true, json: async () => refs};
    assert.ok(url.endsWith('/releases/tags/security-history-000000010000'), url);
    return {ok: true, json: async () => ({id: 42, tag_name: 'security-history-000000010000', draft: false, immutable: true, target_commitish: source, assets: [{name: 'catalogue.json', state: 'uploaded', digest, size: 100}]})};
  };
  const records = await history();
  assert.equal(records.length, 10000);
  assert.equal(records.at(-1).sha256, digest);
  assert.equal(requests.length, 2);
});

test('monitor lookup bounds API requests and refuses a truncated result set', async () => {
  let requests = 0;
  global.fetch = async url => {
    requests++;
    assert.ok(new URL(url).searchParams.get('created').startsWith('>='));
    const batch = Array.from({length: 100}, (_, i) => ({id: requests * 100 + i}));
    return {ok: true, json: async () => ({total_count: 1001, workflow_runs: batch})};
  };
  await assert.rejects(latestMonitor(at), /bound|limit|Incomplete/);
  assert.equal(requests, 1);
});

test('render exposes assessment metadata, monitoring expiry and fix availability', () => {
  const vm = require('node:vm');
  const fs = require('node:fs');
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve('../docs/security/status/status.js'), 'utf8'), context);
  const node = () => ({textContent: '', children: [], append(child) { this.children.push(child); }, replaceChildren() { this.children = []; }});
  const rows = node();
  context.document = {getElementById: id => id === 'releases' ? rows : node(), createElement: node};
  context.value = {catalogue: {releases: {'bicep/0.1.0': {...record, delivery: 'complete', supersededAt: '2026-01-01T00:00:00Z', latestAttempt: {...record.latestAttempt, database: {built: '2025-12-31T00:00:00Z', from: 'https://example.test/db', schemaVersion: '6', checksum: digest}}, lastKnownVulnerable: {findings: [{id: 'CVE-example', severity: 'High', fix: {state: 'fixed', versions: ['1.2.3']}}]}}}}};
  context.run = run;
  context.at = at;
  vm.runInContext('render(value, run, at)', context);
  const text = rows.children[0].children.map(n => n.textContent).join(' ');
  for (const expected of ['https://example.test/db', '2025-12-31T00:00:00Z', '2026-04-01T00:00:00.000Z', 'fixed', '1.2.3', 'completed / success', 'Outcome: clean']) assert.ok(text.includes(expected), expected);
  const wrong = 'sha256:' + 'c'.repeat(64);
  context.value.catalogue.releases['bicep/0.1.0'].state = 'incomplete';
  context.value.catalogue.releases['bicep/0.1.0'].unexpectedDigests = {[wrong]: {digest: wrong, discoveredAt: '2026-01-01T00:00:00Z', latestAttempt: {...record.latestAttempt, digest: wrong, outcome: 'vulnerable'}}};
  vm.runInContext('render(value, run, at)', context);
  assert.equal(rows.children.length, 2);
  assert.match(rows.children[0].children[1].textContent, /failed: unexpected public bytes/);
  assert.match(rows.children[1].children[0].textContent, /unexpected image/);
  assert.equal(rows.children[1].children[2].textContent, 'vulnerable');
});

test('unexpected digests prevent retirement until independently absent', () => {
  const wrong = 'sha256:' + 'c'.repeat(64);
  const entry = {digest: wrong, discoveredAt: '2025-12-01T00:00:00Z'};
  const abandoned = {...record, state: 'abandoned', publicExposure: 'absent', abandonedAt: '2025-12-01T00:00:00Z', absenceProof: {digest, digestMissing: true, versionTagMissing: true, checkedAt: '2026-01-01T00:00:00Z'}, unexpectedDigests: {[wrong]: entry}};
  assert.equal(status(abandoned, run, at), 'unavailable');
  entry.absenceProof = {digest: wrong, digestMissing: true, checkedAt: '2026-01-01T01:00:00Z'};
  assert.equal(status(abandoned, run, at), 'no-longer-monitored');
});
