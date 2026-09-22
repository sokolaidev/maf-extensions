# Docker Sandboxes as a backend: what it would be entitled to claim

> An exploration, not a proposal: whether the suite should grow a `maf-sandbox-docker-sbx` backend driving Docker Sandboxes, and what it could honestly declare. Read against [`../backends/writing-a-backend.md`](../backends/writing-a-backend.md), [`../policy-isolation.md`](../policy-isolation.md), [`../network.md`](../network.md) and [`../capabilities.md`](../capabilities.md). Nothing is decided here and no package exists. The plain-container record is [`docker-backend.md`](docker-backend.md), which set this product aside as a separate backend. The sibling service record is [`daytona-backend.md`](daytona-backend.md).

Read on 2026-09-22 from Docker's own sources: the manual under `docs.docker.com/ai/sandboxes/`, the `sbx` CLI reference, the release notes, three Docker blog posts, and the issue tracker at `github.com/docker/sbx-releases`. The CLI reference renders client-side, so it was read as its source in `github.com/docker/docs` (`data/sbx_cli/*.yaml`, `content/manuals/ai/sandboxes/**`). Nothing was installed or run. Issue-tracker reports are user reports, not Docker statements, and are marked as such. The probe list at the end is what a live install would settle.

## What it is now

Docker Sandboxes is a standalone CLI, `sbx`, with a background daemon, `sandboxd`. It needs neither Docker Desktop nor Docker Engine, except to build a custom template. The old `docker sandbox` Desktop plugin was removed in Docker Desktop 4.80.0 (2026-06-29). Docker CLI 29.8 on this machine prints that it "is deprecated and has been removed".

| Fact | Value |
|---|---|
| Hosts | Windows 11 with Windows Hypervisor Platform; macOS 14+ on Apple silicon; Ubuntu 24.04+ with KVM and the user in the `kvm` group |
| Hypervisor | Docker's own VMM over Hypervisor.framework, WHP and KVM (blog, 2026-04-16) |
| Install | `winget install -h Docker.sbx`, `brew install docker/tap/sbx`, or the `docker-sbx` apt package |
| Account | `sbx login` with a Docker account, required for every run; free, including commercial use |
| Source | Closed. The releases repo says "License: Proprietary — Docker Inc." |
| Status | "Early Access". Several sub-features are marked experimental |
| Version | 0.43.0 on 2026-09-15; microVM-based since Desktop 4.58 (2026-01-26) |

The CLI collects telemetry by default; `SBX_NO_TELEMETRY=1` opts out. A backend should document that and leave the choice to the host.

## Why ask at all

The suite's default floor is `MICROVM`, and today only two backends clear it. ACAS is remote, paid and Azure-only. Hyperlight runs a packaged Python runtime, not a POSIX guest with a shell. Docker and WSLC serve a real POSIX guest locally, but only after the host lowers the floor to `CONTAINER`.

Docker Sandboxes would be the first backend that runs a **local POSIX guest with a shell at the default floor**, on all three developer platforms. It is also free. `ubuntu-24.04` hosted runners have KVM, and Docker's blog (2026-08-21) reports a full run passing there. So the live leg could run in CI without a paid account. No other `MICROVM` backend offers that.

## Where it would sit on the ladder

"Every sandbox runs inside a lightweight microVM with its own Linux kernel", one VM per sandbox. Inside the VM, a private Docker Engine runs the workload as a container, and "the agent has no path to your host Docker daemon". The workload runs as a non-root `agent` user **with sudo**. Docker says so plainly: "The hypervisor boundary is the isolation control, not in-VM privilege separation."

So the rung is `MICROVM`, subject to the four conditions. The product ships with defaults that fail two of them. A backend would have to turn those defaults off, and check that they stay off.

**(1) A hardware virtualization boundary.** Holds, per the vendor, on all three hosts.

**(2) No host control-plane credential in the guest.** The credential design fits: an HTTP proxy on the host injects API keys, and the guest sees only a sentinel value. But **SSH agent forwarding is on by default**, and "any process inside the sandbox can ask the forwarded agent to authenticate or sign data". That is host authority in the guest. Forwarding uses the `SSH_AUTH_SOCK` of whichever client "creates, starts, or joins" the sandbox, unless the host-wide `ssh.agentSocketPath` setting names a socket. So the backend must remove `SSH_AUTH_SOCK` from the environment of every `sbx` call. It must also refuse to run when `ssh.agentForwardingEnabled` is on and `ssh.agentSocketPath` is set. Values passed with `-e` or `--env-file` are readable in the guest, so the backend never passes secrets that way.

**(3) `CLOSED` or `ALLOWLIST` enforced.** Enforced outside the VM. All outbound TCP goes through a proxy on the host, DNS goes through a resolver that applies the policy, and "direct external UDP and ICMP are blocked". But the policy is host-wide state that the backend does not own. That is the finding, and it has [its own section](#egress-host-wide-state-the-backend-can-narrow-but-not-own).

**(4) Explicit guest-to-host channels only.** The defaults cross the boundary in several ways. A workspace mount (virtiofs, "changes in either direction are instant"). A shared skills store, mounted read-only by default and shared across sandboxes. SSH agent forwarding. Clipboard writes. Published ports. Host services, through the special hostname Docker gives the guest for the host. A backend would create **mountless** sandboxes (`sbx create shell` with no path, working in `/home/agent/workspace`), pass `--skills off`, publish no ports and install no kits. Kits "install as root" and can add allow rules. After that, the only channels left are the ones the backend drives: `exec` and `cp`.

The workspace mount is ruled out in any case. Docker documents that a hard link in a mounted workspace reaches the file outside it: "the agent can read and modify the underlying file". A backend that mounted a host directory would hand the guest whatever that directory links to.

## Egress: host-wide state the backend can narrow but not own

The grammar is richer than ours and translates cleanly. `example.com` is one host on any port. `*.example.com` is **one** subdomain level. `**.example.com` is any depth. Our `*.example.com` matches any depth and never the base domain, so it maps to `**.example.com`. Whether `**.example.com` also matches the base domain is not documented. If it does, the translation over-allows by one host, and the backend must refuse any wildcard entry until a probe settles it.

The problem is how rules combine. With no organization governance, the effective policy for one sandbox is:

- **allow** = the global allow rules, plus that sandbox's allow rules, plus any rules its kits add;
- **deny** = the same three sources, and "deny rules take precedence over allow rules".

Global rules apply to every sandbox, including ones already running. The default is picked once with `sbx policy init <allow-all|balanced|deny-all>`, and "Balanced" allows broad wildcards such as `*.googleapis.com`. The user's own interactive sandboxes share that global policy.

So a per-sandbox rule can only add allows or add denies. It can **narrow** a sandbox's egress, but it cannot **define** it. Each mode needs a different answer:

- **`CLOSED` is declarable.** Create the sandbox with `--deny-network "**"`. A deny wins over every allow, whatever the global policy says now or later. Docker describes this flag as "safe under centralized governance because a local deny can only narrow, never widen, egress". This needs two checks first. Does a `**` deny also cover IP literals and CIDR-matched destinations? The docs say an allowed hostname "isn't checked against CIDR rules". And does it cover the guest's special hostname for the host? A user report ([sbx-releases#546](https://github.com/docker/sbx-releases/issues/546)) says a deny rule for that name is listed but not enforced. If either check fails, `CLOSED` is not enforced.
- **`ALLOWLIST` is declarable only as a stated host posture.** The effective allow set equals the spec's only when the global policy has no allow rules, the `shell` agent's built-in kit adds none, and nobody adds one later. The backend can check the first two when it acquires a sandbox (`sbx policy ls <sandbox> --json`) and refuse on a mismatch. It cannot stop a later global `sbx policy allow network` from widening a running sandbox. It would sit beside ACAS group identity as "trusted host configuration": declared, and documented in the README where the host operator reads it.
- **Under organization governance, local allow rules are inactive.** "Only organization allow rules grant access." The per-sandbox allowlist then does nothing, and the effective set is the organization's. `CLOSED` still holds, because local denies still apply. `ALLOWLIST` must be refused.

A per-sandbox allow cannot be set at create time for local sandboxes; `--allow-network` is cloud-only. It takes a separate `sbx policy allow network --sandbox <name>` after `create`. Nothing runs in between, because `create` does not start a workload. That ordering still needs a probe.

HTTPS goes through a forward proxy that terminates TLS with a certificate the guest trusts. Other TCP is forwarded without TLS termination. Rules match on host and port; nothing matches on HTTP method, so `EGRESS_METHODS` stays withheld. `sbx policy log --json` reports decisions per sandbox. That could support `observes_egress=True`, the first backend to claim it. Whether its entries can be tied to one call window is a probe.

## The file plane: one host-side command

There is one file command, `sbx cp SRC DST`, where one side is `SANDBOX:PATH`. It has a `-L/--follow-link` flag that is off by default. It places a copied directory at the destination, and creates the destination if it is missing. There is no stat, list or delete command, and no API.

This is the plain Docker backend's position, with less to work with:

- **Where `cp` runs is not documented.** `sbx exec` flags "match the behavior of 'docker exec'", and a private engine runs the workload inside the VM. So `cp` is most likely `docker cp` on that inner engine, run by `sandboxd` through the guest. If so, the answer comes from an engine outside the workload container but inside the VM. That is weaker than Docker's host engine and stronger than Daytona's in-workload daemon.
- **Stat has two routes.** Our Docker backend stats a path from the first tar header that `docker cp` writes to stdout. If `sbx cp SANDBOX:path -` streams a tar, the same helpers (`tar_header_from_block`, `sandbox_entry_from_tar_header`) apply, and stat stays engine-answered. If it does not, the only route is `stat_by_asking_the_guest` over `exec`, as a declared posture.
- **Reading out lands on the host.** `sbx cp` from a sandbox writes into a host path. A link in the guest could come out as a link on the host. Unless `cp` streams to stdout, the backend must copy into a fresh private directory, refuse any non-regular entry there, and read the bytes itself. It must never extract into a directory a kind will later read.
- **The check-then-act window stays open.** [#1130](https://github.com/sokolaidev/maf-extensions/issues/1130) closed Docker's window with `docker pause`. `sbx` has no local pause; only cloud `stop` suspends a sandbox. The window is a residual to state.
- **The reach rule holds for an uncomfortable reason.** The workload user has sudo, so the guest can already change anything a root `cp` could land. As on Daytona's container class, the REACH probes find nothing to swap. The README should say so.

A user report ([sbx-releases#545](https://github.com/docker/sbx-releases/issues/545)) says virtiofs shows a symlink as a regular file on macOS. That concerns workspace mounts, which the backend would not use, but it is a reason to run the link probes on every host platform.

## exec: argv form, most of the rest unknown

`sbx exec [flags] SANDBOX COMMAND [ARG...]` takes argv and "doesn't start a shell". It has `-w`, `-e`, `-u` and `-i`, and it starts a stopped sandbox first. That covers `an-argv-sequence-runs` and `working-directory-is-honoured` on paper. The rest is not documented:

- **Separate stdout and stderr.** Not documented. `docker exec` without `-t` keeps them apart, so the probe will probably pass.
- **Exit codes.** A user report ([sbx-releases#504](https://github.com/docker/sbx-releases/issues/504), open) says a missing sandbox and a command's own exit 1 look the same. That is the WSLC problem again. Check the sandbox exists (`sbx ls --json`) before each call, or run a wrapper that prints a marker, so the two outcomes differ.
- **Timeout.** There is no flag, so the deadline is host-side. Killing the `sbx` client may not stop the guest process. A user report ([sbx-releases#385](https://github.com/docker/sbx-releases/issues/385), closed) says a hung `exec` once wedged every sandbox operation. So the timeout must also kill the guest process tree, probably with a second `exec`, and needs a bounded cleanup allowance.
- **Byte fidelity.** Probably exact over a pipe. A probe settles it.
- **Detach.** The CLI reference lists `-d`, but the usage page says "Detached execution (`-d`) isn't supported". A user report ([sbx-releases#505](https://github.com/docker/sbx-releases/issues/505)) says it hangs. The backend should not use it.

## Lifecycle: names, no labels, no TTL

Sandboxes have names, not labels. A name is two or more characters of letters, digits, hyphens and periods. There is `ls --json`, `stop`, `rm`, `prune --filter until=`, but no `inspect`. So ownership goes into the name: a fixed prefix plus a hash of `(SandboxKey, kind)`, as the guide allows. Purge lists with `sbx ls --json` and matches on the prefix. Whether a duplicate `create` fails decides whether names can resolve the get-or-create race, or whether the backend must serialize creates itself.

Local sandboxes "are not TTL-managed". A crashed host leaves VMs behind, and each one defaults to half the host's memory (`--memory`, 512 MiB to 32 GiB) and all its CPUs. The backend must always pass `--cpus` and `--memory`, and it needs an operator cleanup path, as the Docker backend has. After `create`, a local sandbox "stops automatically when no sessions keep it running", so a warm reacquire pays a VM start on each `exec`. How long that takes is not published; "start in seconds" is the only claim.

`sbx template save` snapshots a sandbox's filesystem into a template, but not its processes, since memory capture is cloud-only. `SNAPSHOT` needs processes and files both reset. A "reset" of delete-then-create-from-template might meet that, but only if it is cheaper than a plain create.

Custom images must extend `docker/sandbox-templates:<variant>` and match the agent. A `shell` sandbox runs Docker's shell template, so the host cannot pick an arbitrary image. That constrains more than our Docker backend does. It also means the guest's tools are Docker's choice, and the POSIX conformance harness needs `sh`, `cat`, `printf`, `stat` and `curl` in that image.

## What it could declare

| Member | Verdict | What it rests on |
|---|---|---|
| `isolation` | `MICROVM` | Per-sandbox VM with its own kernel. Conditions 2 and 4 hold only with SSH forwarding off, skills off and no mounts, checked at acquire |
| `EXEC` | Declare | `sbx exec` argv form, with a host deadline and a guest kill. Exit-code ambiguity handled by a marker or an existence check |
| `FILES_IN` | Declare | `sbx cp` host→guest. Every refusal is the backend's until a probe shows how `cp` treats a linked destination |
| `FILES_OUT` | Declare, posture depends on a probe | Engine-answered through a tar on stdout if `sbx cp … -` works; otherwise guest-answered over `exec` |
| `FILES_DELETE` | Declare | Guest-authority `rm` over `exec`; the guest is effectively root, so nothing is out of reach |
| `RECLAIM` | Declare | Removal over `exec` as the workload user, which the guest can already do |
| `HOST_TOOLS` | Declare | Follows from `EXEC`, `FILES_IN` and `FILES_OUT` |
| `FILES_LIST` | Withhold | No native listing; the same reason the Docker backend withholds it |
| `RUN_CODE` | Withhold | The runtime is the template's, not the backend's |
| `SNAPSHOT` | Withhold | No local process snapshot |
| `ATTACHED_IDENTITY` | Withhold | Proxy-injected secrets are the right shape, but the core contract is not built |
| `EGRESS_METHODS` | Withhold | No method rules |
| `egress_modes` | `{CLOSED}`, plus `ALLOWLIST` as a checked host posture | See [the egress section](#egress-host-wide-state-the-backend-can-narrow-but-not-own) |
| `os_families` | `{POSIX}` | Linux guest |
| `observes_egress` | Open | `sbx policy log --json` might support it |

## What the package would cost

**Dependencies.** None. Like the Docker and WSLC backends, it drives a CLI through subprocesses. The host installs `sbx` and signs in.

**A closed, early-access binary.** Every claim above rests on vendor documentation plus probes against a proprietary binary that ships a release every week or two (0.39.0 to 0.43.0 in four weeks). The live suite is the only evidence, and it must run against the version users install, not once.

**Host-wide side effects.** `sbx policy init` must run once before the first sandbox, and it sets policy for every sandbox on the machine. A backend must never run it. If it has not been run, the backend refuses and says so. The same goes for the SSH settings: the backend reads them and refuses, and never changes them.

**CI.** The live job needs a Docker account's access token as a secret, and `sbx login --password-stdin`. It also needs `sbx policy init deny-all` on the runner, which is fine there because the runner is disposable. Whether the hosted runner's nested virtualization runs the conformance suite at a usable speed is a measurement. Docker's own CI example went through gh-aw, not raw `sbx`.

**Code.** Close to the Docker backend in size (3699 lines of `_backend.py`), minus the proxy, which Docker Sandboxes supplies. It needs new work for name-based ownership, the policy check, and guest-side timeout cleanup.

**Release.** A new package publishes last ([`../../../RELEASING.md`](../../../RELEASING.md)).

## What reading could not answer

In roughly this order:

1. Does `--deny-network "**"` refuse everything: a denied hostname, an IP literal, the guest's hostname for the host, and a host a global rule allows? Measured with both egress-suite controls.
2. Does `sbx cp SANDBOX:path -` stream a tar to stdout? If so, does its first header describe a final symlink as a link?
3. Does `sbx cp` to a sandbox write through a destination whose final component is a symlink? As which user?
4. Does `sbx cp` out of a sandbox recreate a guest symlink on the host, and with `-L` off, where does it point?
5. Does `sbx exec` keep stdout and stderr apart, and are both byte-exact for invalid UTF-8 and NUL?
6. What exit status separates "no such sandbox" from the command's own failure?
7. Does killing the `sbx exec` client stop the guest process tree? If not, what does?
8. With `SSH_AUTH_SOCK` unset in the environment of every `sbx` call, does the guest get no agent socket?
9. Does `sbx policy ls <sandbox> --json` list global, kit and per-sandbox rules with their source, so an effective allow set can be computed? Does a `shell` sandbox's built-in kit add any allow rule?
10. Does `**.example.com` also match `example.com`?
11. Do two concurrent `sbx create` calls with the same name fail the second?
12. Cold create, warm `exec` on a stopped sandbox, and `rm`, timed on each host platform and on `ubuntu-24.04`.
13. Can `sbx policy log --json` entries be tied to one sandbox and one time window?
14. Does anything in the guest reach `sandboxd` or its credentials? The guest has sudo, so any socket or token the VM holds is the workload's.

## Verdict, held loosely

Worth doing, and more useful than Daytona. It fills the one gap no shipped backend fills: a local POSIX guest at the default `MICROVM` floor, on Windows, macOS and Linux, at no cost, with a free CI leg. Its boundary is the right shape: one VM per sandbox, egress enforced on the host, credentials injected by a proxy. And it adds no Python dependency.

The catch is ownership. The backend owns none of what makes its claim true. The egress policy, SSH forwarding and the default mode are host-wide settings the user and other tools can change, some of them while a sandbox runs. So the honest package declares what it can pin itself: `CLOSED`, through a per-sandbox deny that nothing can override. Everything else is a host posture it checks at acquire and documents. Its file plane is thinner than Docker's (no pause, no stat, `cp` only). It sits on a closed, early-access binary that changes every week or two.

Start with probes 1, 2, 5 and 7. They decide whether `CLOSED`, an engine-answered `FILES_OUT` and an honest `EXEC` are available at all. If they pass, this is a better next backend than Daytona. If the `**` deny leaks, the package is a `MICROVM` backend with an empty `egress_modes`, which serves nothing under the default policy. Then it is not worth building yet.
