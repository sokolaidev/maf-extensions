# Docker Sandboxes as a backend: what it would be entitled to claim

> An exploration, not a proposal: whether the suite should grow a `maf-sandbox-docker-sbx` backend driving Docker Sandboxes, and what it could honestly declare. Read against [`../backends/writing-a-backend.md`](../backends/writing-a-backend.md), [`../policy-isolation.md`](../policy-isolation.md), [`../network.md`](../network.md) and [`../capabilities.md`](../capabilities.md). Nothing is decided here and no package exists. The plain-container record is [`docker-backend.md`](docker-backend.md), which set this product aside as a separate backend. The sibling service record is [`daytona-backend.md`](daytona-backend.md).

Two sources, and every claim below says which one it rests on.

- **Read** on 2026-09-22 from Docker's own sources: the manual under `docs.docker.com/ai/sandboxes/`, the `sbx` CLI reference, the release notes, three Docker blog posts, and the issue tracker at `github.com/docker/sbx-releases`. The CLI reference renders client-side, so it was read as its source in `github.com/docker/docs` (`data/sbx_cli/*.yaml`, `content/manuals/ai/sandboxes/**`). Issue-tracker reports are user reports, not Docker statements, and are marked as such.
- **Measured** the same day with `sbx` **v0.38.0** on Windows 11, with the Balanced network preset already in place. The current release is v0.45; the docs describe the current release, and the two disagree in places. Every measured claim is a v0.38.0-on-Windows claim until it is repeated on the current version and on macOS and Linux.

## What it is now

Docker Sandboxes is a standalone CLI, `sbx`, with a background daemon, `sandboxd`. It needs neither Docker Desktop nor Docker Engine, except to build a custom template. The old `docker sandbox` Desktop plugin was removed in Docker Desktop 4.80.0 (2026-06-29). Docker CLI 29.8 on this machine prints that it "is deprecated and has been removed".

| Fact | Value |
|---|---|
| Hosts | Windows 11 with Windows Hypervisor Platform; macOS 14+ on Apple silicon; Ubuntu 24.04+ with KVM and the user in the `kvm` group |
| Hypervisor | Docker's own VMM over Hypervisor.framework, WHP and KVM (blog, 2026-04-16) |
| Install | `winget install -h Docker.sbx`, `brew install docker/tap/sbx`, or the `docker-sbx` apt package. On Windows it lands in `%LOCALAPPDATA%\DockerSandboxes\bin`, which a non-interactive shell may not have on `PATH` |
| Account | `sbx login` with a Docker account, required for every command; free, including commercial use. A saved login can be revoked, and then every command fails until a person completes a browser device flow |
| Source | Closed. The releases repo says "License: Proprietary — Docker Inc." |
| Status | "Early Access". Several sub-features are marked experimental |
| Version | v0.45 current; microVM-based since Desktop 4.58 (2026-01-26) |
| Guest (measured) | Ubuntu 26.04, kernel 7.0.12, x86_64. The workload is `agent` (uid 1000) in groups `sudo` and `docker`, with passwordless `sudo`. A private `dockerd` runs as root beside it |

The CLI collects telemetry by default; `SBX_NO_TELEMETRY=1` opts out. A backend should document that and leave the choice to the host.

## Why ask at all

The suite's default floor is `MICROVM`, and today only two backends clear it. ACAS is remote, paid and Azure-only. Hyperlight runs a packaged Python runtime, not a POSIX guest with a shell. Docker and WSLC serve a real POSIX guest locally, but only after the host lowers the floor to `CONTAINER`.

Docker Sandboxes would be the first backend that runs a **local POSIX guest with a shell at the default floor**, on all three developer platforms. It is also free. `ubuntu-24.04` hosted runners have KVM, and Docker's blog (2026-08-21) reports a full run passing there. So the live leg could run in CI without a paid account. No other `MICROVM` backend offers that.

## Where it would sit on the ladder

"Every sandbox runs inside a lightweight microVM with its own Linux kernel", one VM per sandbox. The workload has root in the guest through `sudo`, and Docker says so plainly: "The hypervisor boundary is the isolation control, not in-VM privilege separation."

So the rung is `MICROVM`, subject to the four conditions.

**(1) A hardware virtualization boundary.** Holds, per the vendor, on all three hosts.

**(2) No host control-plane credential in the guest.** The credential design fits: a proxy on the host injects API keys, and the guest sees only a sentinel. Measured, the guest environment carries `SBX_CRED_<SERVICE>_MODE=apikey` for eight services and no values. The docs say **SSH agent forwarding is on by default** and follows the `SSH_AUTH_SOCK` of whichever client creates, starts or joins the sandbox. On v0.38.0 on Windows it did not happen: with `SSH_AUTH_SOCK` pointed at the running OpenSSH agent, the guest got no socket and no keys. v0.38.0 also has no `ssh.agentForwardingEnabled` setting, so the feature is newer than that build. The backend still has to strip `SSH_AUTH_SOCK` from every `sbx` call and refuse when forwarding is configured, and the probe must be repeated on the current version.

**(3) `CLOSED` or `ALLOWLIST` enforced.** Enforced outside the VM, and measured. The catch is that the policy is host-wide state the backend does not own. See [the egress section](#egress-closed-holds-the-allowlist-is-shared).

**(4) Explicit guest-to-host channels only.** This is the condition that needs the most work. Measured on a plain `shell` sandbox, the guest can reach:

- **The workspace**, a read-write virtiofs mount of a host directory. v0.38.0 cannot create a sandbox without one: `sbx create` takes `AGENT PATH [PATH...]`. The docs' "mountless" sandbox is a newer feature.
- **An MCP gateway on the host** at `MCP_GATEWAY_URL`, reachable through the proxy **even under a deny-all rule**. It answers `initialize` and offers `mcp-add`, `mcp-find`, `mcp-exec`, `mcp-config-set` and `code-mode`. Its catalog is the MCP servers registered on the host with `sbx mcp add`. There were none on the test host, so `mcp-add fetch` failed with "not registered". On a host with a registered server, a guest could load that server and use its authority, whatever the egress policy says. The backend must refuse when `sbx mcp ls` lists anything, and re-check at every acquire.
- **A browser-open endpoint** on the proxy (`/_sbx/browser-open`), which the guest's `xdg-open` wrapper uses to open a URL in the host's browser. It is policy-checked: under deny-all it returned 403 and the log records it as proxy type `browser-open`. Under an allowlist, a guest can make the host's browser visit any allowed host.
- **A clipboard endpoint** (`/_sbx/clipboard`) and a `clipboard-bridge` process. Clipboard writes were not tested, because that would overwrite the test host's clipboard. Image paste from the host is off by default (`clipboard.imagePaste=false`).
- **Host services** through the special hostname Docker gives the guest for the host, but only where a policy rule allows `localhost:<port>`. Denied by default, and still denied by `deny **` when a per-sandbox allow names the port.

## The workspace: a better file plane than `cp`, on Windows

The docs present the workspace as the agent's project directory. It is also the most useful file channel the backend has. A backend would give each sandbox a fresh, empty host directory that the backend owns, and do file operations on the host side of it with host-native, no-follow calls. That is the strongest possible source for a filesystem path check: not the guest, not an engine, but the host's own filesystem.

Measured, on a Windows host:

| Probe | Result |
|---|---|
| Guest path | The host path translated, not the same path. `C:\Users\…\maf-probe-ws` appears as `/c/Users/ANTONS~1/…/maf-probe-ws`. It carries the host user's short name into the guest, and `work_dir` is dictated by the host path |
| Guest creates a symlink in the workspace | `ln: Permission denied`. The guest cannot plant links a host-side read could follow |
| Host junction in the workspace pointing outside it | **Followed.** The guest read a file outside the workspace through it. Docker's "symlinks pointing outside the workspace scope are not followed" does not cover Windows junctions |
| Host hard link in the workspace to a file outside it | The guest read and rewrote the outside file, as Docker documents |
| Guest writes | Land as the host user's files. The guest sees everything as `agent`, mode 777; a `chmod 4755` is stored and read back, and the guest mount is `nosuid` |
| Names Windows cannot hold | Silently changed. `trail.` became `trail`, `a:b` was stored with a private-use character, and writing `a` overwrote an existing `A` |
| Read-only extra mount (`PATH:ro`) | Holds. Guest root can remount it `rw` inside the guest, but writes still fail with "Read-only file system", so the host enforces it |
| Guest root remounts the main workspace `ro` | Succeeds, inside the guest only |

So the workspace is safe as a file plane under three conditions, all of which the backend controls. The directory is new and empty, so no host-made junction or hard link exists in it. The guest cannot create links in it, which holds on Windows and **is not yet measured on macOS or Linux**. And every guest-supplied name that the host filesystem would change is refused before it is used: trailing dots and spaces, reserved characters, reserved device names, and case collisions. That last rule is a POSIX guest on a case-insensitive host, and it needs care, because the guest itself can make two names differing only in case and the host silently merges them.

On those conditions, `FILES_OUT` and `FILES_LIST` are answered on the host with `lstat`, and `FILES_IN` writes land owned by the guest user with no authority gap. `RECLAIM` is a host-side delete of a directory the guest cannot plant links in. The window between check and act is narrower than anywhere else in the suite, because the guest cannot create the thing a swap would need.

## `sbx cp`: measured, and weaker

`sbx cp` is the only other file command, and there is no stat, list or delete command. Measured:

| Probe | Result |
|---|---|
| Bytes | Exact in both directions. A 200 MB file copied out in 1.9 s. There is no size bound, so `max_bytes` has to be checked before the copy |
| Copy in, ownership | Always **root, mode 0755**, whatever the source file was. Copy-in runs as root: it wrote into a `0700` root directory. A kind's input would not be writable by the guest without a `chown` |
| Copy in, final component a symlink | The link is **replaced** by a regular file, not written through. A dangling link is replaced too |
| Copy in, a parent is a symlink | **Followed.** The file landed in the link's target |
| Copy in, missing parent | Refused, with a `500` and a `tar` error. Parents are not created |
| Copy in, relative path | Refused: "container path must be absolute" |
| Copy out, final component a symlink | Recreates the link **on the host**, with the guest's target (`/etc/hostname`). On Windows without symlink privilege this fails. On a macOS or Linux host it would create a host link pointing at the host's own file |
| Copy out, dangling symlink | "not found". `cp` follows the link to check existence, then copies the link itself |
| Copy out, a directory holding a link | Fails part-way on Windows, leaving the files already extracted. Exit 1 |
| Copy out, parent a symlink | Followed. `-L` follows the final link as well |
| Copy out to `-` | Writes a file literally named `-` in the working directory and exits 0, the WSLC bug again |
| Copy into a stopped sandbox | Starts it first |

So there is no tar stream to read a header from, and every stat on this route is guest-answered over `exec`. A copy out must go to a fresh private host directory, with every entry checked as a regular file before a byte is read. `sbx pause` does not exist locally, so the check-then-act window stays open. On this route the guest's `sudo` is what makes the reach rule hold: nothing a root `cp` lands is beyond what the guest could already change.

The workspace route is better on every row. `cp` stays useful as a fallback, for paths outside the workspace.

## exec: measured, mostly good

`sbx exec [flags] SANDBOX COMMAND [ARG...]` takes argv and does not start a shell. About 0.35 s per call.

| Probe | Result |
|---|---|
| Streams | Separate. `echo OUT; echo ERR >&2` gave each on its own stream |
| Bytes | Exact on both streams, including NUL, `\xff` and CRLF, and on stdin |
| Exit codes | Faithful: 1, 3, 127 and 255 came back as given |
| Argv | Passed verbatim: spaces, `$HOME`, quotes, `*`, a newline and a backslash each arrived as one element. **An empty element is refused** with `400 Bad Request: cmd element N is empty` |
| stdin | Passed through even without `-i`; empty when none is given |
| `-w` and `-e` | Honoured. A missing `-w` directory fails before the command runs |
| No such sandbox | Exit 1 and an `ERROR:` line on stderr, which a command's own exit 1 cannot be told from by status alone ([sbx-releases#504](https://github.com/docker/sbx-releases/issues/504)) |
| Runtime errors | A missing binary or missing working directory reports **on stdout**, with CRLF and exit 127: `OCI runtime exec failed: …` |
| `-d` | Does not detach. `-d sleep 30` blocked for 30.4 s, matching [sbx-releases#505](https://github.com/docker/sbx-releases/issues/505) |
| Deadline | There is no timeout flag. **Killing the `sbx` client leaves the guest process running**: a loop kept ticking after its client was killed. A guest-side `timeout 2` did not bound the call either: it returned after 10.3 s, because an orphaned `sleep` held the output pipe open |
| Process state | Persists between calls. A process started by one `exec` was still running at the next |
| Self-stop | Killing the guest's `sleep infinity` keeper stopped the sandbox; the next `exec` restarted it |

Most gaps close with one wrapper: run every command as `sh -c 'cd "$1" && shift && exec "$@"'` under `setsid`, with its process-group id written to a file. Then:

- a missing binary or directory becomes the shell's own error on stderr;
- a deadline kills the whole group with a second `exec`;
- a nonce printed by the wrapper tells "the command ran and exited 1" apart from "no sandbox".

The empty-argument refusal is the one gap a wrapper cannot close by quoting, because `sbx` rejects the request before it reaches the guest. The backend has to encode argv, for example as one base64 argument the wrapper decodes. The template carries every tool the POSIX conformance harness needs, including `curl`, `python3`, `setsid` and `base64`.

## Egress: CLOSED holds, the allowlist is shared

Measured, with the host on the Balanced preset (199 global allow rules, including `api.anthropic.com` and `**.openai.com:443`).

**A sandbox created with `--deny-network "**"` is closed, and proven by content, not by connect:**

- HTTP and HTTPS return 403 from the proxy, by hostname and by IP literal. That includes `api.anthropic.com`, which the global policy allows and a sandbox without the deny reached.
- DNS does not resolve a denied name. UDP to `8.8.8.8:53` got no reply.
- Raw TCP to an IP literal connects, because the transparent proxy accepts it, and then returns zero bytes. The egress suite must check content, as it does for ACAS.
- The host service returned 403, and so did the browser-open endpoint.
- A per-sandbox **allow added after the deny does not override it**: `example.com` and `localhost:8765` stayed 403. The #546 report (a listed deny for the host's hostname not enforced) did not reproduce on v0.38.0.

That is `CLOSED`, with one exception: the MCP gateway answered under the deny, which is why the host's registered-server list is a refusal condition.

**`ALLOWLIST` is shared with the host, and measured so:**

- Every new sandbox gets a per-sandbox allow for `openrouter.ai` that no one asked for (origin `scoped`).
- Global allows apply to every sandbox. A sandbox with a per-sandbox allow for `example.com` also reached `api.anthropic.com` through a global rule.
- `**.example.org` matched `example.org` as well as `www.example.org`. Our `*.example.org` excludes the base domain, so the translation needs a per-sandbox deny for the base. A deny wins, so the pair means exactly what the spec says.
- An exact `example.com` did not match `www.example.com`.

An exact allowlist is possible only if the backend denies every global allow and the automatic `openrouter.ai` rule, per sandbox, and refuses when one of them overlaps a host the spec asks for. That list is read at acquire and can change at any moment after it: a global rule added later widens a running sandbox. So `ALLOWLIST` stays a checked host posture, documented where the host operator reads it. Under organization governance, local allows are inactive and it must be refused.

**Observation.** `sbx policy log --json` reports blocked requests per sandbox name, with host, proxy type (`forward`, `transparent`, `browser-open`, DNS) and the matching rule. But entries are aggregated (`since`, `last_seen`, `count_since`), not per request, and **they outlive `sbx rm`**: a recreated sandbox with the same name inherited the old one's entries. A call window cannot be attributed from it, so `observes_egress` stays `False`. The template's own background traffic (`archive.ubuntu.com`, `download.docker.com`) shows up in it too.

## Lifecycle: measured

| Step | Time |
|---|---|
| Cold `create`, including a first pull of the ~600 MB `shell-docker` image | 24.9 s |
| Warm `create`, image cached | 3.6 s |
| `create` from a saved template | 3.8 s |
| `stop` | 0.7 to 5.5 s |
| `rm --force` | 0.4 to 0.6 s |
| `exec` on a stopped sandbox | about 1.5 s, restarting it |

**Names do the ownership work.** Two concurrent `create` calls with the same name produced one sandbox and a `409 Conflict`, so the name resolves the get-or-create race. `rm` removes the sandbox's own policy rules, and a recreated name starts clean. There are no labels, so the owner and `(SandboxKey, kind)` go into the name as a prefix plus a hash. Keep it short: a `+`, which the help text allows, is refused, and an 80-character name failed late with only `failed to run sandbox container`. Nothing was left behind.

**Nothing expires locally.** A crashed host leaves sandboxes behind, and each defaults to half the host's memory and all its CPUs. The backend always passes `--cpus` and `--memory`, and an operator path purges by name prefix through `sbx ls --json`.

## Templates and snapshots: measured

| Probe | Result |
|---|---|
| `template save` on a running sandbox | Refused. It prompts `Stop it now? (y/N)` and fails without input, so the backend stops first |
| `template save` on a stopped sandbox | 3.5 s. With `--output`, a 636 MB tar |
| What a template keeps | Files everywhere: `/home/agent`, a root-owned file in `/etc`, `/tmp`, shell rc changes |
| What it drops | Processes, and the workspace's contents, which live on the host |
| Policy | Not carried. A sandbox made from a template gets the default rules |
| An arbitrary image (`alpine:3.20`) as the template | Accepted on v0.38.0, although the docs say templates must extend Docker's. Its workload ran as **root**, with no `agent` user |

`SNAPSHOT` is within reach. A `stop` kills every process and keeps every file, so a reset can be "`rm`, then `create` from a template saved before the first workload". That meets the contract, because files and processes both return to the baseline. It costs 3.8 s, the same as a fresh create, so it earns a declaration only for a kind whose baseline is expensive to build, such as one with installed packages. Taking the baseline adds a stop and a save (about 4 to 9 s) once per sandbox.

Arbitrary images change two things. `RUN_CODE` stays withheld, because the runtime is the image's. And an image with no `sudo` and no `agent` user runs the workload as root, which on the `cp` route removes the only authority gap there was, and on the workspace route changes nothing.

## What it could declare

| Member | Verdict | What it rests on |
|---|---|---|
| `isolation` | `MICROVM` | A per-sandbox VM with its own kernel. Conditions 2 and 4 hold only with no MCP server registered and no SSH agent forwarded, checked at every acquire |
| `EXEC` | Declare | Argv form, separate byte-exact streams, faithful exit codes. Through a wrapper for runtime errors, deadlines and the no-sandbox case, with argv encoded |
| `FILES_IN` | Declare | A host-side write into the private workspace, owned by the guest user, with every name checked against the host filesystem's rules |
| `FILES_OUT` | Declare | A host-side `lstat` and read. The guest cannot make links there, which is proven on Windows only |
| `FILES_LIST` | Declare | A host-side listing with `lstat`. The first backend for which a listing is cheap and not guest-answered |
| `FILES_DELETE` | Declare | A host-side unlink inside the workspace |
| `RECLAIM` | Declare | A host-side delete of a directory the guest cannot plant links in |
| `HOST_TOOLS` | Declare | Follows from `EXEC`, `FILES_IN` and `FILES_OUT` |
| `SNAPSHOT` | Open | Delete and recreate from a baseline template meets the contract; worth it only when the baseline is expensive |
| `RUN_CODE` | Withhold | The runtime is the image's |
| `ATTACHED_IDENTITY` | Withhold | Proxy-injected secrets are the right shape, but the core contract is not built |
| `EGRESS_METHODS` | Withhold | No method rules |
| `egress_modes` | `{CLOSED}`, plus `ALLOWLIST` as a checked host posture | See [the egress section](#egress-closed-holds-the-allowlist-is-shared) |
| `os_families` | `{POSIX}` | Linux guest |
| `observes_egress` | `False` | The log is aggregated and keyed by a reusable name |

The workspace rows assume a Windows host. On macOS and Linux they wait on one probe, whether the guest can create a symlink in the workspace. If it can, those rows fall back to the `cp` route and guest-answered checks, and `FILES_LIST` is withheld.

## What the package would cost

**Dependencies.** None. Like the Docker and WSLC backends, it drives a CLI through subprocesses. The host installs `sbx` and signs in.

**A closed, early-access binary that moves fast.** v0.38.0 and the current docs already disagree on `create`'s workspace argument, detach, SSH forwarding, the name grammar and custom images. The live suite is the only evidence, and it must run against the version users install.

**Host-wide state.** The backend reads and never writes the global policy, the MCP registrations and the SSH settings. `sbx policy init` must have run before the first sandbox; if not, the backend refuses and says so.

**A login that can lapse.** On the test host the saved login had been revoked, and every command failed until someone completed a browser device flow. A backend cannot recover from that by itself. It needs a clear refusal naming `sbx login`, and CI needs `sbx login --password-stdin` with an access token secret.

**CI.** Whether the hosted `ubuntu-24.04` runner runs the conformance suite at a usable speed is a measurement. Docker's own CI example went through gh-aw, not raw `sbx`.

**Code.** Smaller than the Docker backend (3699 lines of `_backend.py`), because the proxy is Docker's and the file plane is the host filesystem. The new work is the exec wrapper, name-based ownership, the host-name rules for the workspace, and the acquire-time checks on policy, MCP and SSH.

**Release.** A new package publishes last ([`../../../RELEASING.md`](../../../RELEASING.md)).

## Still open

1. All of the above on the **current** `sbx` and on **macOS and Linux** hosts, starting with whether the guest can create a symlink in the workspace.
2. SSH agent forwarding on the current version, with `SSH_AUTH_SOCK` set and unset in the `sbx` client's environment.
3. What a registered MCP server gives a guest under deny-all: whether its own traffic passes the sandbox's policy.
4. Clipboard writes from the guest, and whether they are policy-checked like browser-open.
5. Whether raw TCP to an **allowed** destination carries data. An allowed `github.com:22` also returned zero bytes, so the non-proxy path is unverified in both directions.
6. Whether `**` in a deny covers IPv6 literals and CIDR-matched destinations.
7. Timing on `ubuntu-24.04` hosted runners.
8. Whether the host-name rules for the workspace are complete for NTFS and for APFS in its default case-insensitive mode.

## Verdict, held loosely

Worth doing, and now more clearly than from the docs alone. It is a local POSIX guest at the default `MICROVM` floor, with byte-exact separate streams, faithful exit codes, `CLOSED` that measured closed by content, and names that resolve the create race. The workspace mount turns out to be the best file plane in the suite on Windows: host-answered, cheap to list, and immune to guest-made links. It adds no Python dependency, and the live leg could run in CI for free.

The costs are real and mostly about ownership. The allowlist is shared with every other sandbox on the host. A host-registered MCP server would be a channel around every egress rule. The login lapses. And the binary changes shape between releases: the version measured here is seven releases old and differs from the docs in five places.

Next step, if it goes ahead: repeat the measurements on the current version on all three hosts. The macOS and Linux symlink probe decides whether the workspace file plane is cross-platform or Windows-only. Then build `EXEC` and the workspace file plane first, declare `CLOSED` only, and leave `ALLOWLIST` for a second pass.
