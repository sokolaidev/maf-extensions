# Docker Sandboxes as a backend: what it would be entitled to claim

> An exploration, not a proposal: whether the suite should grow a `maf-sandbox-docker-sbx` backend driving Docker Sandboxes, and what it could honestly declare. Read against [`../backends/writing-a-backend.md`](../backends/writing-a-backend.md), [`../policy-isolation.md`](../policy-isolation.md), [`../network.md`](../network.md) and [`../capabilities.md`](../capabilities.md). Nothing is decided here and no package exists. The plain-container record is [`docker-backend.md`](docker-backend.md), which set this product aside as a separate backend. The sibling service record is [`daytona-backend.md`](daytona-backend.md).

Two sources, and every claim below says which one it rests on.

- **Read** on 2026-09-22 from Docker's own sources: the manual under `docs.docker.com/ai/sandboxes/`, the `sbx` CLI reference, the release notes, three Docker blog posts, and the issue tracker at `github.com/docker/sbx-releases`. The CLI reference renders client-side, so it was read as its source in `github.com/docker/docs` (`data/sbx_cli/*.yaml`, `content/manuals/ai/sandboxes/**`). Issue-tracker reports are user reports, not Docker statements, and are marked as such.
- **Measured** on one Windows 11 host with the Balanced network preset already in place: first with `sbx` **v0.38.0** (2026-09-22), then with the current **v0.45.1** (2026-09-23). The body describes v0.45.1. Where v0.38.0 behaved differently, both are given, because the difference says how fast the surface moves. macOS and Linux were not measured.

## What it is now

Docker Sandboxes is a standalone CLI, `sbx`, with a background daemon, `sandboxd`. It needs neither Docker Desktop nor Docker Engine, except to build a custom template. The old `docker sandbox` Desktop plugin was removed in Docker Desktop 4.80.0 (2026-06-29).

| Fact | Value |
|---|---|
| Hosts | Windows 11 with Windows Hypervisor Platform; macOS 14+ on Apple silicon; Ubuntu 24.04+ with KVM and the user in the `kvm` group |
| Architecture (measured, Windows) | `sandboxd` drives a host-side containerd. Each sandbox is a container task run by the `io.containerd.nerdbox.v1` runtime, whose shim creates one WHP partition per sandbox and boots a kernel into it |
| Install | `winget install -h Docker.sbx`, `brew install docker/tap/sbx`, or the `docker-sbx` apt package. On Windows it lands in `%LOCALAPPDATA%\DockerSandboxes\bin`, which a non-interactive shell may not have on `PATH` |
| Account | `sbx login` with a Docker account, required for every command; free, including commercial use. A saved login can be revoked, and then every command fails until a person completes a browser device flow |
| Source | Closed. The releases repo says "License: Proprietary — Docker Inc." |
| Status | "Early Access". Several sub-features are marked experimental |
| Guest (measured) | Ubuntu 26.04, kernel 7.0.12, x86_64. The workload is `agent` (uid 1000) in groups `sudo` and `docker`, with passwordless `sudo`. A private `dockerd` runs as root beside it |

The CLI collects telemetry by default; `SBX_NO_TELEMETRY=1` opts out. A backend should document that and leave the choice to the host.

## Why ask at all

The suite's default floor is `MICROVM`, and today only two backends clear it. ACAS is remote, paid and Azure-only. Hyperlight runs a packaged Python runtime, not a POSIX guest with a shell. Docker and WSLC serve a real POSIX guest locally, but only after the host lowers the floor to `CONTAINER`.

Docker Sandboxes would be the first backend that runs a **local POSIX guest with a shell at the default floor**, on all three developer platforms. It is also free. `ubuntu-24.04` hosted runners have KVM, and Docker's blog (2026-08-21) reports a full run passing there. So the live leg could run in CI without a paid account. No other `MICROVM` backend offers that.

## Where it would sit on the ladder

"Every sandbox runs inside a lightweight microVM with its own Linux kernel", one VM per sandbox, and the host-side log confirms one hypervisor partition per sandbox. The workload has root in the guest through `sudo`, and Docker says so plainly: "The hypervisor boundary is the isolation control, not in-VM privilege separation."

So the rung is `MICROVM`, subject to the four conditions.

**(1) A hardware virtualization boundary.** Holds: one WHP partition per sandbox, measured on Windows.

**(2) No host control-plane credential in the guest.** The credential design fits: a proxy on the host injects credentials into requests, and the guest sees only a sentinel. Two measured facts turn that design into a condition the backend has to check.

- **Stored secrets follow the sandbox's egress.** The test host had a global `github` service secret, and every sandbox reported `SBX_CRED_GITHUB_MODE=apikey`, meaning the proxy would inject that token into the guest's requests to GitHub's domains. With the secret removed, a new sandbox reported `none` for all eight services, so the modes follow the host's stored secrets. Under `CLOSED` nothing reaches them. Under an allowlist naming a GitHub host, the guest acts with the host user's GitHub credential. The backend must read `sbx secret ls` at acquire and refuse any allowlist that overlaps a stored secret's domains.
- **SSH agent forwarding is on by default** (`ssh.agentForwardingEnabled=true`). Every sandbox gets `/run/ssh-agent.sock`, even with no `SSH_AUTH_SOCK` on the host. On this Windows host the socket could not reach the OpenSSH agent: `ssh-add -l` answered "communication with agent failed", with and without `SSH_AUTH_SOCK` pointed at the agent's pipe. The host agent held no keys, so a working bridge would not have shown any either. A backend cannot rest a credential claim on a bridge that happened to fail on one platform, so it must refuse unless `ssh.agentForwardingEnabled` is `false`. That setting is host-wide and needs a daemon restart. v0.38.0 had no such setting and no socket.

**(3) `CLOSED` or `ALLOWLIST` enforced.** Enforced outside the VM, and measured. The catch is that the policy is host-wide state the backend does not own. See [the egress section](#egress-closed-holds-the-allowlist-is-shared).

**(4) Explicit guest-to-host channels only.** Measured on a `shell` sandbox, the guest can reach:

- **A workspace**, a read-write virtiofs mount of a host directory, if one is given. v0.45.1 can create a sandbox without one (working directory `/home/agent/workspace`), and `--skills off` keeps the shared skills store out. v0.38.0 required a workspace and had no `--skills` flag.
- **An MCP gateway on the host** at `MCP_GATEWAY_URL`, reachable through the proxy **even under a deny-all rule**. It answers `initialize` and offers `mcp-add`, `mcp-find`, `mcp-exec`, `mcp-config-set` and `code-mode`. Its catalog is the MCP servers registered on the host with `sbx mcp add`; there were none, so nothing could be added. On a host with a registered server, a guest could load it and use its authority, whatever the egress policy says. The backend must refuse when `sbx mcp ls` lists anything, and re-check at every acquire.
- **A browser-open endpoint** on the proxy (`/_sbx/browser-open`), which the guest's `xdg-open` wrapper uses to open a URL in the host's browser. It is policy-checked: 403 under deny-all, and 403 without an allow rule on a sandbox with no deny. Under an allowlist, a guest can make the host's browser visit any allowed host.
- **A clipboard endpoint** (`/_sbx/clipboard`) and a `clipboard-bridge` process. Clipboard writes were not tested, because that would overwrite the test host's clipboard. Image paste from the host is off by default.
- **Host services** through the special hostname Docker gives the guest for the host, only where a policy rule allows `localhost:<port>`. Denied by default, and still denied by `deny **` when a per-sandbox allow names the port.

## The workspace: a file plane answered by the host

The docs present the workspace as the agent's project directory. It is also a file channel the backend could own: give each sandbox a fresh, empty host directory, and do file operations on the host side of it with host-native, no-follow calls. That is the strongest source a filesystem path check can have: not the guest, not an engine, but the host's own filesystem. The cost is that the guest's working directory is the host path translated (`C:\Users\…\ws` appears as `/c/Users/<SHORTNAME~1>/…/ws`), which carries the host user's name into the guest and dictates `work_dir`.

Measured, on a Windows host:

| Probe | v0.45.1 | v0.38.0 |
|---|---|---|
| Host junction in the workspace pointing outside it | Not followed: "No such file" | **Followed**: the guest read the outside file |
| Host hard link to a file outside it | The guest rewrote the outside file | Same |
| Guest `ln -s` in the workspace | **Exits 0 and creates nothing**, visible to neither side | `Permission denied` |
| Guest hard link, FIFO | A hard link works. A FIFO is a FIFO to the guest and an empty regular file to the host | Not tested |
| A name ending in a dot (`trail.`) | Kept | Silently became `trail` |
| `a:b`, `q?x` | Stored on the host with private-use characters. `a:b` then **vanishes from the guest's own listing** | Stored, still listed |
| `a` written beside `A` | Overwrote `A`: the host is case-insensitive | Same |
| `PATH:ro` mount | Holds. Guest root can remount it `rw` in the guest, but writes still fail, so the host enforces it | Same |
| Guest writes | Land as the host user's files. The guest sees `agent`, the mount is `nosuid` | Same |

So the workspace is a safe file plane on three conditions, all of which the backend controls. The directory is new and empty, so no host-made junction or hard link exists in it. The guest cannot create links in it: true on both versions on Windows, although v0.45.1 reports success while doing nothing, and **not measured on macOS or Linux**. And every guest-supplied name the host filesystem would change or hide is refused before use: reserved characters, reserved device names, and case collisions. The last is a POSIX guest on a case-insensitive host, and the guest itself can make two names differing only in case that the host merges.

On those conditions, `FILES_OUT` and `FILES_LIST` are host-side `lstat` calls, `FILES_IN` lands owned by the guest user with no authority gap, and `RECLAIM` is a host-side delete of a directory the guest cannot plant links in. The window between check and act is narrower than anywhere else in the suite, because the guest cannot create the thing a swap would need.

## `sbx cp`: measured, and weaker

`sbx cp` is the only other file command; there is no stat, list or delete command. Both versions behaved the same:

| Probe | Result |
|---|---|
| Bytes | Exact in both directions. A 200 MB file copied out in 1.9 s. There is no size bound, so `max_bytes` has to be checked before the copy |
| Copy in, ownership | Always **root, mode 0755**. Copy-in runs as root: it wrote into a `0700` root directory. A kind's input would not be writable by the guest without a `chown` |
| Copy in, final component a symlink | The link is **replaced** by a regular file, not written through |
| Copy in, a parent is a symlink | **Followed** |
| Copy in, missing parent or relative path | Refused: a `500` with a `tar` error, and "container path must be absolute" |
| Copy out, final component a symlink | Recreates the link **on the host**, with the guest's target. On Windows without symlink privilege this fails; on macOS or Linux it would create a host link pointing at the host's own file |
| Copy out, dangling symlink | "not found": `cp` follows the link to check existence, then copies the link itself |
| Copy out, a directory holding a link | Fails part-way on Windows, leaving an empty directory behind |
| Copy out, parent a symlink | Followed. `-L` follows the final link as well |
| Copy out to `-` | Writes a file literally named `-` in the working directory and exits 0, the WSLC bug again |
| Into a stopped sandbox | Starts it first |

So on this route every stat is guest-answered over `exec`, a copy out must land in a fresh private host directory and be checked before a byte is read, and with no local pause the check-then-act window stays open. The guest's `sudo` is what makes the reach rule hold here. `cp` stays useful as a fallback for paths outside the workspace, or for a mountless sandbox.

## exec: measured, and good after a wrapper

`sbx exec [flags] SANDBOX COMMAND [ARG...]` takes argv and does not start a shell. About 0.4 s per call.

| Probe | Result |
|---|---|
| Streams | Separate |
| Bytes | Exact on both streams and on stdin, including NUL, `\xff` and CRLF |
| Exit codes | Faithful: 1, 3, 127 and 255 |
| Argv | Verbatim: spaces, `$HOME`, quotes, `*`, a newline and a backslash each arrived as one element. **An empty element is refused** with `400 Bad Request: cmd element N is empty` |
| stdin | Passed through even without `-i`; empty when none is given |
| `-w`, `-e`, `-u` | Honoured |
| No such sandbox | Exit 1 and an `error:` line on stderr. By status alone it is a command's own exit 1 ([sbx-releases#504](https://github.com/docker/sbx-releases/issues/504)) |
| Runtime errors | A missing binary or working directory reports **on stdout**, with CRLF and exit 127: `OCI runtime exec failed: …` |
| `-d` | Refused outright on v0.45.1 ("--detach is not supported for exec"). On v0.38.0 it blocked for the full run |
| Deadline | There is no timeout flag. **Killing the `sbx` client leaves the guest process running.** A guest `timeout 2` did not bound the call either: it returned after 10.5 s, because an orphaned child held the output pipe. **Killing the process group does**: a command started under `setsid` and killed with `kill -9 -<pgid>` returned in 2.4 s with no orphan left |
| Process state | Persists between calls, until the sandbox auto-stops **30 s after the last session ends**. That stop kills every process and keeps every file |

One wrapper closes every gap but one. Run each command as `setsid sh -c 'echo $$ > <pgid file>; cd "$1" && shift && exec "$@"'`. Then:

- a missing binary or directory becomes the shell's own error on stderr;
- a deadline kills the whole group with a second `exec`;
- a nonce printed by the wrapper tells "the command ran and exited 1" apart from "no sandbox".

The empty argument is the exception, because `sbx` rejects the request before it reaches the guest. The backend has to encode argv, for example as one base64 argument the wrapper decodes. The template carries every tool the POSIX conformance harness needs, including `curl`, `python3`, `setsid`, `base64` and `jq`.

## Egress: CLOSED holds, the allowlist is shared

Measured, with the host on the Balanced preset (199 global allow rules, including `api.anthropic.com` and `**.openai.com:443`).

**A sandbox created with `--deny-network "**"` is closed, proven by content, not by connect:**

- HTTP and HTTPS return 403 from the proxy, by hostname, IPv4 literal and IPv6 literal. That includes `api.anthropic.com`, which the global policy allows and a sandbox without the deny reached.
- DNS does not resolve a denied name, and UDP to `8.8.8.8` got no reply. A UDP egress feature exists but is experimental and off.
- Raw TCP to a denied destination connects, because the transparent proxy accepts it, and then returns zero bytes. The egress suite must check content, as it does for ACAS. On v0.45.1 an **allowed** raw destination does carry data: an HTTP reply from `example.com`'s address and GitHub's SSH banner. So the transparent path is proven in both directions. On v0.38.0 it returned nothing either way.
- The host service and browser-open returned 403.
- A per-sandbox **allow added after the deny does not override it**. The #546 report (a listed deny for the host's hostname not enforced) did not reproduce.

That is `CLOSED`, with the MCP gateway as the one exception, which is why the host's registered-server list is a refusal condition.

**`ALLOWLIST` is shared with the host:**

- Global allows apply to every sandbox, including running ones. A sandbox with a per-sandbox allow for `example.com` also reached `api.anthropic.com` through a global rule.
- `**.example.org` matched `example.org` as well as `www.example.org`. Adding a per-sandbox deny for `example.org` blocked the base and kept `www.example.org`, which is exactly our `*.example.org`. An exact `example.com` did not match `www.example.com`.
- v0.38.0 also gave every new sandbox an unrequested per-sandbox allow for `openrouter.ai`. v0.45.1 does not.

An exact allowlist is possible only if the backend denies every global allow, per sandbox, and refuses when one of them overlaps a host the spec asks for. That list is read at acquire and can change at any moment after it: a global rule added later widens a running sandbox. Add the stored-secret check from condition 2, and `ALLOWLIST` is a checked host posture, documented where the host operator reads it. Under organization governance, local allows are inactive and it must be refused.

**Observation.** `sbx policy log --json` reports `blocked_hosts` and, on v0.45.1, `allowed_hosts`, per sandbox name, with proxy type (`forward`, `transparent`, `browser-open`, DNS) and the matching rule. But entries are aggregated (`since`, `last_seen`, `count_since`), not per request, and **they outlive `sbx rm`**: a recreated sandbox with the same name inherited 15 blocked and 4 allowed entries. A call window cannot be attributed from it, so `observes_egress` stays `False`.

## Lifecycle: measured

| Step | v0.45.1 | v0.38.0 |
|---|---|---|
| Cold `create`, including a first ~600 MB image pull | not repeated | 24.9 s |
| Warm `create` | 3.1 to 4.2 s, with or without a workspace | 3.6 s |
| `create` from a saved template | 4.0 s | 3.8 s |
| `stop` | 5.5 s ("state preserved") | 0.7 to 5.5 s |
| `rm --force` | 0.5 s | 0.4 to 0.6 s |
| `exec` or `cp` on a stopped sandbox | about 1.4 s, restarting it | about 1.5 s |

**Names do the ownership work.** Two concurrent `create` calls with the same name produced one sandbox and a `409 Conflict`, so the name resolves the get-or-create race. `rm` removes the sandbox's own policy rules, and a recreated name starts with none. There are no labels, so the owner and `(SandboxKey, kind)` go into the name as a prefix plus a hash, within 63 characters and without `+`. v0.45.1 refuses both up front; v0.38.0 accepted a long name and failed late.

**Nothing expires locally.** A crashed host leaves sandboxes behind, each defaulting to half the host's memory and all its CPUs. The backend always passes `--cpus` and `--memory`, and an operator path purges by name prefix.

**The engine socket lives in `%TEMP%`, and it vanished once.** During the v0.45.1 run, the host-side engine socket directory (`%TEMP%\sboxd-<id>`) disappeared between two commands, with no error in the daemon log. The test volume was nearly full, and Windows cleans temporary files when space runs low, but the cause was not proven. From then on every sandbox, including one the tests never touched, failed with `500 … backend unavailable`, and **`sbx ls` printed "No sandboxes found"**. `sbx daemon restart` recreated the socket, and every sandbox came back intact. Two consequences for a backend. A purge or get-or-create must never read an empty listing as absence without a successful health check first (`sbx diagnose` passed throughout, so it is not that check). And "backend unavailable" is a daemon fault to report, not a sandbox to replace.

## Templates and snapshots: measured

Both versions behaved the same.

| Probe | Result |
|---|---|
| `template save` on a running sandbox | Refused. It prompts `Stop it now? (y/N)` and fails without input, so the backend stops first |
| `template save` on a stopped sandbox | 3.1 s. With `--output`, a 636 MB tar |
| What a template keeps | Files everywhere: `/home/agent`, a root-owned file in `/etc`, `/tmp` |
| What it drops | Processes, and a workspace's contents, which live on the host |
| Policy | Not carried. A sandbox made from a template gets no per-sandbox rules |
| An arbitrary image (`alpine:3.20`) as the template | Accepted, although the docs say templates must extend Docker's. Its workload ran as **root**, with no `agent` user |
| `template rm` without a terminal | Needs `--force` on v0.45.1 |

`SNAPSHOT` is within reach. A reset can be "`rm`, then `create` from a template saved before the first workload", and that meets the contract, because files and processes both return to the baseline. It costs about 4 s, the same as a fresh create, so it earns a declaration only for a kind whose baseline is expensive to build. Taking the baseline adds a stop and a save, about 9 s, once per sandbox.

Arbitrary images change two things. `RUN_CODE` stays withheld, because the runtime is the image's. And an image with no `sudo` and no `agent` user runs the workload as root. That removes the only authority gap on the `cp` route, and changes nothing on the workspace route.

## What it could declare

Measured against sbx v0.45.1 on Windows. Tracked as [#1412](https://github.com/sokolaidev/maf-extensions/issues/1412).

**Isolation: `MICROVM`.** Each sandbox gets its own hypervisor partition. Conditions 2 and 4 hold only while three host facts hold. The backend checks all three at every acquire and refuses if any fails:

- SSH agent forwarding is off (`ssh.agentForwardingEnabled=false`). It is on by default, so the host operator has to change it.
- No MCP server is registered on the host (`sbx mcp ls` is empty). The MCP gateway is reachable under deny-all.
- No allowed host overlaps a stored secret's domains (`sbx secret ls`).

**Capabilities:**

| Member | Verdict | What it rests on |
|---|---|---|
| `EXEC` | Declare | Argv passes verbatim; streams come back separate and byte-exact, with faithful exit codes. A `setsid` wrapper does four things: kills the whole process group at the deadline, moves runtime errors to stderr, tells a missing sandbox from a failing command, and decodes argv (sbx refuses an empty argument) |
| `FILES_IN` | Declare | A host-side write into the private workspace, owned by the guest user. Names the host filesystem would change, hide or merge are refused |
| `FILES_OUT` | Declare | A host-side `lstat`, then a read |
| `FILES_LIST` | Declare | A host-side listing with `lstat`: the first backend where a listing is cheap and not answered by the guest |
| `FILES_DELETE` | Declare | A host-side unlink inside the workspace |
| `RECLAIM` | Declare | A host-side delete of a directory the guest cannot plant links in |
| `HOST_TOOLS` | Declare | Follows from `EXEC`, `FILES_IN` and `FILES_OUT` |
| `SNAPSHOT` | Withhold for now | Delete and recreate from a template saved before the first workload resets files and processes both. It costs about 4 s, the same as a fresh create, so it earns a declaration only when the baseline is expensive |
| `RUN_CODE` | Withhold | Any image is accepted, so the runtime is the image's |
| `ATTACHED_IDENTITY` | Withhold | Proxy-injected secrets are the right shape, but they are host-wide rather than per sandbox, and the core contract is not built |
| `EGRESS_METHODS` | Withhold | No rules by HTTP method |

**The other declarations:**

| Field | Value | What it rests on |
|---|---|---|
| `egress_modes` | `{CLOSED}` | A per-sandbox `--deny-network "**"` beats every global allow and every later allow, measured by content. `ALLOWLIST` is possible only as a host posture checked at acquire: deny each global allow per sandbox, and accept that a global rule added later widens a running sandbox. `UNRESTRICTED` is not worth declaring |
| `os_families` | `{POSIX}` | Linux guest |
| `isolation_scopes` | `{CONVERSATION}` | `CALL` is possible, at about 4.5 s per call for a create and a delete |
| `observes_egress` | `False` | The policy log is aggregated, keyed by a name that can be reused, and outlives `rm` |
| `attached_identity` | `NO_ATTACHED_IDENTITY` | As above |

**The Windows-only dependency.** The five file capabilities and `RECLAIM` all rest on one measured fact: the guest cannot create links in its workspace. On Windows, `ln -s` exits 0 and creates nothing. On macOS and Linux this is not measured. If the guest can create links there, those rows fall back to `sbx cp` with checks the guest answers, and `FILES_LIST` is withheld.

**A first version** declares `MICROVM`, `EXEC`, the workspace file capabilities, `RECLAIM`, `HOST_TOOLS` and `CLOSED` only, after the macOS and Linux link probe.

## What the package would cost

**Dependencies.** None. Like the Docker and WSLC backends, it drives a CLI through subprocesses. The host installs `sbx` and signs in.

**A closed, early-access binary that moves fast.** Between v0.38.0 and v0.45.1, measured a day apart on one host, these changed:

- workspaces became optional;
- `-d` went from blocking to refused;
- SSH forwarding appeared, on by default;
- the automatic `openrouter.ai` allow disappeared;
- junctions stopped being followed;
- guest `ln -s` went from "permission denied" to a silent no-op;
- trailing dots stopped being stripped;
- allowed raw TCP started carrying data;
- name validation moved up front.

Several of these are security-relevant, in both directions. The live suite is the only evidence, and it must run against the version users install.

**Host-wide state.** The backend reads and never writes the global policy, stored secrets, MCP registrations and SSH settings. `sbx policy init` must have run before the first sandbox. And `ssh.agentForwardingEnabled=false` is a host change the operator has to make, because the default fails condition 2.

**A daemon that can lose its engine.** The socket in `%TEMP%` is a single point of failure for every sandbox on the host, and the failure reads as "no sandboxes". The backend needs to recognize it and report it rather than recreate.

**A login that can lapse.** A revoked login fails every command until a person completes a browser device flow. A backend needs a clear refusal naming `sbx login`, and CI needs `sbx login --password-stdin` with an access token secret.

**CI.** Read, not run, on 2026-09-23. The answer depends on the runner.

| Runner | Can it run sbx? | Evidence |
|---|---|---|
| `ubuntu-24.04` / `ubuntu-latest`, x64 | **Yes** | Standard Linux runners expose `/dev/kvm` (GitHub changelog, 2024-04-02). Docker's blog (2026-08-21) reports a full run there taking 11 min 16 s. gh-aw's generated setup, in `actions/setup/sh/`, is the working recipe: check that `/dev/kvm` exists, install `docker-sbx` from Docker's apt repository, `sudo chmod 666 /dev/kvm`, start the daemon, log in with a Docker access token, `sbx policy init`, then a create/exec/rm smoke test |
| `ubuntu-24.04-arm` | Unknown | sbx ships `linux-arm64` `.deb` packages, but GitHub documents no KVM on its arm64 runners |
| `windows-2022` / `windows-2025` | No, as documented | sbx requires Windows 11 with Windows Hypervisor Platform. GitHub's Windows runners are Windows Server, with Hyper-V installed but not enabled |
| `macos-*` (Apple silicon) | No | GitHub: "Nested-virtualization is not supported due to the limitation of Apple's Virtualization Framework." |

Four constraints follow for a live job on the Linux runner:

- It needs a Docker account's access token as a secret, and `sbx login --password-stdin`. This repository has none today.
- Secrets are not passed to pull requests from forks, so the job runs after merge or by dispatch, as the Docker live job already does.
- The job changes `/dev/kvm` permissions and runs `sbx policy init` on a machine it throws away, which is fine there and never acceptable in the backend itself.
- A public-repository runner has 4 CPUs, 16 GB of memory and 14 GB of disk, so every sandbox needs explicit `--cpus` and `--memory`, and the ~600 MB template eats into the disk.

Whether the conformance suites finish in a usable time is still a measurement. So is whether a raw `sbx` job works without gh-aw. gh-aw has since deprecated its `docker-sbx` runtime in favour of plain Docker, citing its setup cost, cold start and platform constraints.

**Code.** Smaller than the Docker backend (3699 lines of `_backend.py`), because the proxy is Docker's and the file plane is the host filesystem. The new work is the exec wrapper, name-based ownership, the host-name rules for the workspace, and the acquire-time checks on policy, secrets, MCP and SSH.

**Release.** A new package publishes last ([`../../../RELEASING.md`](../../../RELEASING.md)).

## Still open

1. All of the above on **macOS and Linux** hosts, starting with whether the guest can create a symlink in the workspace.
2. SSH agent forwarding where the bridge works (macOS and Linux), with a key loaded, to confirm what `ssh.agentForwardingEnabled=false` removes.
3. What a registered MCP server gives a guest under deny-all: whether its own traffic passes the sandbox's policy.
4. Clipboard writes from the guest, and whether they are policy-checked like browser-open.
5. What deleted the engine socket directory, and whether the daemon ever recovers without a restart.
6. A live run on `ubuntu-24.04` and `ubuntu-24.04-arm` hosted runners: KVM, install, `sbx diagnose`, then the conformance suites and their timing.
7. Whether the host-name rules for the workspace are complete for NTFS, and for APFS in its default case-insensitive mode.

## Verdict, held loosely

Worth doing. It is a local POSIX guest at the default `MICROVM` floor, with byte-exact separate streams, faithful exit codes, a deadline that works through a process-group kill, `CLOSED` that measured closed by content, and names that resolve the create race. The workspace mount is the best file plane in the suite on Windows: host-answered, cheap to list, and closed to guest-made links. It adds no Python dependency, and the live leg could run in CI for free.

The costs are about ownership and churn. Every host-wide setting that makes the claim true is somebody else's:

- the global policy;
- stored secrets;
- MCP registrations;
- SSH forwarding, which is on by default.

The backend can only read those and refuse. The daemon can lose its engine and report the loss as an empty host. And seven releases changed nine behaviours this record depends on.

Next step, if it goes ahead: repeat these measurements on macOS and Linux. The symlink probe decides whether the workspace file plane is cross-platform or Windows-only. Then build `EXEC` and the workspace file plane first, declare `CLOSED` only, and leave `ALLOWLIST` for a second pass.
