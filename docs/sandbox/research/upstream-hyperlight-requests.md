# Three upstream requests the Hyperlight path waits on

> Drafted and filed 2026-09-25. Each section is one request: an opt-in file-preservation policy in `hyperlight-sandbox` ([hyperlight-dev/hyperlight-sandbox#227](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/227), open), which [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) is blocked on; eager validation of `allow_domain` method tokens in the same SDK's Python binding ([hyperlight-dev/hyperlight-sandbox#228](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/228), open), which the [#377](https://github.com/sokolaidev/maf-extensions/issues/377) measurement found missing; and digest-pinned base images with build provenance for the device-plugin image ([hyperlight-dev/hyperlight-on-kubernetes#15](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/issues/15), open) that [#1424](https://github.com/sokolaidev/maf-extensions/issues/1424) has to admit. Every measurement below was taken on 2026-09-25 against the exact 0.7.0 trio on Windows 11 x86-64 with WHP and CPython 3.13, except where a section says otherwise. The argument comes first in this repository's terms; the text to paste upstream follows under *What to file*, self-contained, with no relative link and no issue number only this repository can resolve. All three were posted the same day, verbatim.

## 1. `hyperlight-sandbox`: a policy that keeps files in the mutable directory across runs

`Sandbox` mounts `input_dir` read-only at `/input` and `output_dir` read-write at `/output`, and `WasmSandbox::run_impl` calls `prepare_for_run` before every execution, which is `clear_output_files`. So the one directory a guest may write to is emptied on the way in: a file the host staged there is gone before the first line of guest code runs, and a file the guest made is gone by the next run. [`hyperlight-backend.md`](hyperlight-backend.md) recorded this on the released source and rejected the two roads around it — a hidden prelude that copies inputs, and a privately patched native wheel — because each adds a file lifecycle nobody validated or abandons an installable dependency set. That leaves the request: an opt-in policy upstream, default unchanged, that preserves the directory through guest entry and reconciles the quota accounting against what is really on disk, since the clear is also where the cached byte and file counts are reset. [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) carries the adapter side and waits on nothing else.

Measured today, host-side, because `os.listdir('/output')` fails inside the guest with `OSError: [Errno 28] Invalid argument` and cannot be used to observe it:

```
host before run 1: ['staged.txt']
run 1: FileNotFoundError: [Errno 44] No such file or directory: '/output/staged.txt' | exit: 1
host after run 1: []
run 2: guest wrote made-by-guest.txt | exit: 0
host after run 2: ['made-by-guest.txt']
run 3: exists in run 3: False | exit: 0
host after run 3: []
```

### What to file

Target: `hyperlight-dev/hyperlight-sandbox` → New issue. The repository has no issue template. Filed as [hyperlight-dev/hyperlight-sandbox#227](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/227) (open).

**Title:** `Opt-in policy to preserve files in the mutable directory across runs; prepare_for_run always clears it`

**Body:**

`WasmSandbox::run_impl` calls `prepare_for_run` before entering the guest, and in 0.7.0 that is `clear_output_files`:

```rust
pub fn prepare_for_run(&mut self) -> Result<()> {
    self.clear_output_files()
}
```

So the only directory a guest may write to, `output_dir` mounted at `/output` with `DirPerms::MUTATE`, is emptied on the way into every run. A file the host places there before the first run is deleted before guest code sees it, and a file the guest creates is deleted by the next run. `input_dir` is read-only, so it cannot serve a workload that reads, edits and deletes its inputs.

Reproduced with the published Python SDK, `hyperlight-sandbox` 0.7.0 with backend-wasm 0.7.0 and python-guest 0.7.0, on Windows 11 x86-64 with WHP and CPython 3.13:

```python
import os, pathlib, tempfile
from hyperlight_sandbox import Sandbox

d = tempfile.mkdtemp()
pathlib.Path(d, "staged.txt").write_text("hello from the host")
sb = Sandbox(output_dir=d)
r = sb.run("print(open('/output/staged.txt').read())")
print(r.stderr.strip().splitlines()[-1], os.listdir(d))
sb.run("open('/output/made-by-guest.txt', 'w').write('x')")
print(os.listdir(d))
r = sb.run("import os; print(os.path.exists('/output/made-by-guest.txt'))")
print(r.stdout.strip(), os.listdir(d))
```

```
FileNotFoundError: [Errno 44] No such file or directory: '/output/staged.txt' []
['made-by-guest.txt']
False []
```

I understand the clear is deliberate: it is where the cached quota accounting is reset, and a run should not inherit a previous run's leftovers by default. I am not asking for the default to change.

**What I am asking for** is an opt-in policy, selected at build time and off by default, under which `prepare_for_run` leaves the mutable directory alone. Two properties matter more than the spelling:

- **Quotas are reconciled from disk, not from the cache.** With preservation on, the file count and byte totals must be recomputed from the real directory before guest entry, so a host-staged file counts against the limits and an over-quota directory fails the run before the guest starts rather than after it has partly written.
- **Reset stays explicit.** Snapshot restore or a dedicated reset call is where the directory is cleared under this policy, so an integrator can still return a sandbox to a known state on purpose. Preservation changes what happens on entry, not what restore means.

A `SandboxBuilder` option such as `.preserve_output_files(true)`, exposed on the Python `Sandbox` constructor beside `output_dir`, would be enough. If you would rather express it as a file-lifetime enum, that also works for us.

Alternatives I considered and would rather not ship: copying inputs in through a hidden guest prelude, which invents a second file lifecycle the runtime does not validate; staging to the read-only `input_dir`, which cannot be edited or deleted by the guest; and a privately patched native wheel, which leaves the published dependency set uninstallable. Happy to contribute the change if the shape is agreed.

## 2. `hyperlight-sandbox`: validate `methods` when `allow_domain` is queued, and keep the sibling rules

The method-enforcement measurement recorded on [#377](https://github.com/sokolaidev/maf-extensions/issues/377) found that a custom method token is accepted by `allow_domain` and then fails the next `run()` with `RuntimeError: invalid HTTP method`, after which every request is denied. Reading the Python binding explains all three facts. The `Sandbox` constructor is lazy: until the first `run()`, `allow_domain` only appends `(target, methods)` to `pending_networks` without parsing anything. The first `run()` builds the native sandbox, takes the whole pending list with `std::mem::take`, and parses each entry with `HttpMethod::parse_list(methods)?`. The `?` returns before `self.inner = Some(sandbox)`, so the built sandbox is dropped and the taken list is gone with it. The second `run()` finds `inner` still `None`, builds again with an empty pending list, and runs with no rule at all — which is why plain code succeeds afterwards and the valid GET rule queued beside the bad one is denied. After initialisation the same call raises immediately, so only the lazy path is wrong. For this suite the consequence is contained: the adapter will filter tokens before calling `allow_domain`, so the closed set in `egress_method_tokens` is what a spec may name. The upstream defect is still worth a report, because an integrator who does not know the constructor is lazy sees a failure two calls away from its cause and loses rules it never touched.

Measured today with the script in *What to file*:

```
== A: positive control, GET only ==
stdout: 200 | stderr:  | exit: 0

== B: a GET rule, then a custom token rule ==
allow_domain(PROPFIND): accepted, no error
run #1 raised: RuntimeError: invalid HTTP method: PROPFIND
run #2: stdout='plain code ran' stderr='' exit=0
GET after the failure: stdout=  | stderr: Err: ErrorCode_HttpRequestDenied() | exit: 1

== C: custom token on an already-initialised sandbox ==
allow_domain(PROPFIND) after init raised: RuntimeError invalid HTTP method: PROPFIND
```

### What to file

Target: `hyperlight-dev/hyperlight-sandbox` → New issue. The repository has no issue template. Filed as [hyperlight-dev/hyperlight-sandbox#228](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/228) (open).

**Title:** `Python: an invalid method token queued before the first run fails that run and silently drops every other queued allow_domain rule`

**Body:**

In the Python binding, `Sandbox` builds the native sandbox lazily on the first `run()`. Until then `allow_domain(target, methods)` stores the raw strings:

```rust
#[pyo3(signature = (target, methods=None))]
fn allow_domain(&mut self, target: &str, methods: Option<Vec<String>>) -> PyResult<()> {
    if let Some(sandbox) = self.inner.as_mut() {
        let methods = HttpMethod::parse_list(methods)
            .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
        sandbox
            .allow_domain(target, methods)
            .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
    } else {
        self.pending_networks.push((target.to_string(), methods));
    }
    Ok(())
}
```

The first `run()` then drains the queue after building:

```rust
for (target, methods) in std::mem::take(&mut self.pending_networks) {
    let methods = HttpMethod::parse_list(methods)
        .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
    sandbox
        .allow_domain(&target, methods)
        .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
}
self.inner = Some(sandbox);
```

Three things follow when one queued token is not a valid method. The error surfaces from `run()`, two calls away from the `allow_domain` that caused it. The `?` returns before `self.inner` is set, so the built sandbox is dropped and its build cost is paid again. And the queue was already taken, so every other rule queued before the first run, valid or not, is discarded: the next `run()` builds a sandbox with no network rules at all and never says so.

Reproduced on `hyperlight-sandbox` 0.7.0 with backend-wasm 0.7.0 and python-guest 0.7.0, Windows 11 x86-64, WHP, CPython 3.13:

```python
from hyperlight_sandbox import Sandbox

GET = "print(http_get('https://example.com/')['status'])"

sb = Sandbox()
sb.allow_domain("https://example.com", ["GET"])
print(sb.run(GET).stdout.strip())                    # 200: the GET rule works on its own

sb = Sandbox()
sb.allow_domain("https://example.com", ["GET"])
sb.allow_domain("https://example.com", ["PROPFIND"])  # accepted, no error
try:
    sb.run("print('plain')")
except RuntimeError as exc:
    print("first run:", exc)                         # invalid HTTP method: PROPFIND
print(sb.run("print('plain')").stdout.strip())       # plain: the second run works
r = sb.run(GET)
print(r.stderr.strip(), r.exit_code)                 # Err: ErrorCode_HttpRequestDenied() 1

sb = Sandbox()
sb.run("print('warm')")
sb.allow_domain("https://example.com", ["PROPFIND"])  # raises at once: the initialised path is right
```

```
200
first run: invalid HTTP method: PROPFIND
plain
Err: ErrorCode_HttpRequestDenied() 1
Traceback (most recent call last):
  ...
RuntimeError: invalid HTTP method: PROPFIND
```

**Expected:** the queued branch validates like the initialised one. Parse in `allow_domain` in both branches and queue the parsed `MethodFilter`, so a bad token raises from the call that supplied it and cannot take its siblings with it:

```rust
let methods = HttpMethod::parse_list(methods)
    .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
match self.inner.as_mut() {
    Some(sandbox) => sandbox.allow_domain(target, methods).map_err(...)?,
    None => self.pending_networks.push((target.to_string(), methods)),
}
```

with `pending_networks: Vec<(String, MethodFilter)>`. `parse_list` already enforces the list bound, so nothing else moves. If a drain failure should remain possible for some other reason, `self.inner` ought to be set before the loop, or the loop ought to run on the remaining entries, so one refused rule does not empty the policy. Happy to send the PR.

## 3. `hyperlight-on-kubernetes`: pin the device-plugin base images by digest and attest the published image

The AKS overlay in [`backends/hyperlight.md`](../backends/hyperlight.md) pins the plugin image by digest, and [#1424](https://github.com/sokolaidev/maf-extensions/issues/1424) asks what that digest proves. Today it proves immutability and nothing about origin. The pinned Dockerfile opens `FROM golang:1.25-alpine AS builder` and finishes on `FROM alpine:3.19`, both floating tags, so the same commit yields different bytes on different days and no digest can be tied back to a source revision by rebuilding. The publish workflow runs `docker/build-push-action@v6` with `contents: read` and `packages: write` only, no `id-token: write` and no `attestations: write`, and no attestation step, so nothing signs what it pushes. Verifying the pinned digest against GitHub's attestation store, `gh attestation verify oci://ghcr.io/hyperlight-dev/hyperlight-device-plugin@sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98 --owner hyperlight-dev`, answered `HTTP 404: Not Found` from the org's attestations endpoint on 2026-09-25, as it did on 2026-09-22. The plugin runs as root with `/var/lib/kubelet/device-plugins` and `/var/run/cdi` mounted, which is exactly the image an operator's admission policy should be able to check. #1424's acceptance list already says to send the reusable build changes upstream; this is the request that goes first.

### What to file

Target: `hyperlight-dev/hyperlight-on-kubernetes` → New issue. The repository has no issue template. Filed as [hyperlight-dev/hyperlight-on-kubernetes#15](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/issues/15) (open).

**Title:** `Pin the device-plugin base images by digest and publish build provenance for ghcr.io/hyperlight-dev/hyperlight-device-plugin`

**Body:**

I deploy the device plugin on AKS pinned by digest, and I would like the digest to prove where the image came from, not only that it has not changed. Two things stand in the way today.

**The build is not reproducible from a commit.** `device-plugin/Dockerfile` uses floating tags for both stages:

```dockerfile
FROM golang:1.25-alpine AS builder
...
FROM alpine:3.19
```

Either tag can move under the same source revision, so the bytes a given commit produces depend on the day it was built, and a consumer cannot rebuild to check a published digest.

**Nothing attests what the workflow pushes.** `publish-device-plugin.yml` builds with `docker/build-push-action@v6` under `contents: read` and `packages: write`, with no `id-token: write`, no `attestations: write` and no attestation step. Verifying a published digest against GitHub's attestation store finds nothing:

```
$ gh attestation verify oci://ghcr.io/hyperlight-dev/hyperlight-device-plugin@sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98 --owner hyperlight-dev
Error: HTTP 404: Not Found (https://api.github.com/orgs/hyperlight-dev/attestations/sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98?per_page=30&predicate_type=https%3A%2F%2Fslsa.dev%2Fprovenance%2Fv1)
```

This matters for this image in particular: the manifest runs it as root with `/var/lib/kubelet/device-plugins` and `/var/run/cdi` mounted, so a cluster admission policy that requires signed provenance for privileged images cannot admit it as published.

**What I am asking for:**

1. Pin both `FROM` lines by digest, `golang:1.25-alpine@sha256:…` and `alpine:3.19@sha256:…`, and let Dependabot's `docker` ecosystem move them.
2. Add `actions/attest-build-provenance` after the push, with `subject-name: ghcr.io/hyperlight-dev/hyperlight-device-plugin`, `subject-digest: ${{ steps.build.outputs.digest }}` and `push-to-registry: true`, and grant the job `id-token: write` and `attestations: write`. With that, `gh attestation verify … --owner hyperlight-dev` passes for every digest the workflow publishes, and a Kubernetes admission controller can require it.
3. Optionally `provenance: mode=max` and `sbom: true` on the build step, so the registry copy carries the same evidence for tooling that reads OCI attestations rather than GitHub's store.

I can send the PR for all three if that is welcome; it is confined to the Dockerfile and the publish workflow.
