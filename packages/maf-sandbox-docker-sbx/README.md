# maf-sandbox-docker-sbx

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker-sbx)](https://pypi.org/project/maf-sandbox-docker-sbx/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-docker-sbx)](https://pypi.org/project/maf-sandbox-docker-sbx/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxDockerSbxExperimentalWarning`.

Run sandbox commands in Docker Sandboxes, one microVM with its own Linux kernel per sandbox, on your own Windows, macOS or Linux machine. The backend drives Docker's `sbx` CLI and has no Python dependency beyond `maf-sandbox`.

This is an independent package, not a Docker or Microsoft product.

## Quickstart

```bash
pip install maf-sandbox-docker-sbx
```

```python
from maf_sandbox import SandboxRouter
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig

backend = SbxSandboxBackend(SbxSandboxConfig())
router = SandboxRouter([backend])
```

The backend clears the router's default `MICROVM` minimum, so no `min_isolation` is needed.

The Python event loop must support subprocesses. Windows' default Proactor loop does; `WindowsSelectorEventLoopPolicy` does not.

## Requirements

Install `sbx` and sign in once:

- Windows 11 with the Windows Hypervisor Platform: `winget install -h Docker.sbx`. It lands in `%LOCALAPPDATA%\DockerSandboxes\bin`, which may not be on `PATH`; pass that path as `sbx_path`.
- macOS 14 or later on Apple silicon: `brew install docker/tap/sbx`.
- Ubuntu 24.04 or later with KVM, and your user in the `kvm` group: the `docker-sbx` apt package.

Then prepare the host once:

```bash
sbx login
sbx policy init deny-all
sbx settings set ssh.agentForwardingEnabled false
sbx daemon restart
```

The backend checks two host-wide settings at every acquire and refuses with `SbxHostNotConfined` when either fails. It reads them and never writes them. Because the SSH setting takes effect only after `sbx daemon restart`, each new sandbox is also checked for the forwarded agent socket before any workload runs. That check runs in the template's own `sh`, so it catches a daemon that was not restarted, not a template that lies: use templates you trust.

- **SSH agent forwarding must be off.** It is on by default, and it gives every sandbox a socket to the host's SSH agent.
- **No MCP server may be registered** (`sbx mcp ls`). The host's MCP gateway answers a sandbox even under a deny-all rule.

A lapsed login fails every `sbx` command until a person signs in again; the backend raises `SbxLoginRequired`. In CI, pipe an access token to `sbx login --username <user> --password-stdin`.

`sbx` collects telemetry by default. Set `SBX_NO_TELEMETRY=1` in the host's environment to opt out.

## Supported operations

| Setting | Behavior |
|---|---|
| Isolation | `MICROVM` |
| Capabilities | `EXEC`, `HOST_TOOLS`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM`, `EGRESS_METHODS`, `EGRESS_PATHS` |
| Guest OS | POSIX |
| Network | `CLOSED`, `ALLOWLIST` with method and path rules |
| Isolation scope | `CONVERSATION` |
| Egress observation | No |
| Command output | 8 MiB of stdout and stderr together, or the budget a caller passes to `exec_bounded`; more kills the command and refuses the call |

`RUN_CODE` is not declared, because any image is accepted and the runtime is the image's. `SNAPSHOT` is not declared. `HOST_TOOLS` uses one held `sbx exec` session per transport run to prevent idle auto-stop. The session starts before guest launch and ends after process and transport-file cleanup, including when a host tool outlasts the run deadline. An unexpected session exit refuses further host calls and retires the sandbox; an in-flight host effect may already have completed, so the run must not be replayed automatically.

Host-tool response and request budgets are folded into the workload's file-transfer declaration. Choose registry and workload limits that fit the backend's `DEFAULT_SANDBOX_LIMITS`; declaring host tools does not raise those limits. The [CodeAct integration test](https://github.com/sokolaidev/maf-extensions/blob/main/tests/test_sbx_codeact_e2e.py) shows a one-call registry with a 1 KiB response budget and 4 MiB workload budgets.

`spec.image_id`, or else `spec.image`, is passed to `sbx create --template`, and a warm acquire refuses a spec that changes either. Without it the sandbox uses Docker's `shell` template, where commands run as `agent` (uid 1000). An image without that user runs commands as root. Commands run in a user namespace of their own, so `sudo` does not work in them, even in Docker's template. The namespace maps only the command's own uid and gid. Files owned by anyone else show as uid 65534 (`nobody`), and `chown` to another user fails, also in a template that runs as root. Permission checks still use the real owners.

Any Linux image works as a template if it has `/bin/sh` and `/bin/bash` and the tools listed under [Commands](#commands). Without `/bin/bash`, `sbx` cannot start the sandbox. An image built locally reaches `sbx` through a tar:

```bash
docker build -t my-image:local .
docker save -o my-image.tar my-image:local
sbx template load my-image.tar
```

Then pass `image="my-image:local"`.

## Files: a workspace answered by the host

Each sandbox mounts one fresh, private host directory, `<workspace_root>/<sandbox name>/ws-<random>`, and nothing else. Every create gets a new one, so a new sandbox never mounts an earlier one's files, and `sbx ls` tells which create made a sandbox. Every command binds that mount again at the storage base's parent, inside a user and mount namespace of its own, and then runs as the image's user. With the default storage base `/maf-sandbox/work`, that parent is `/maf-sandbox`. `sbx` lets only its own templates mount as root, and the namespace needs no such capability, so any template works. At create, root only makes the empty directory the mount goes over.

Stats, reads, listings and writes act on the host side of the mount. No path check is answered inside the guest.

- On macOS and Linux each path component is opened relative to its parent with `O_NOFOLLOW`, so a link the guest creates between a check and an operation makes the operation fail rather than follow it. On Linux the guest can create links in its workspace, and the plane refused to read or write through each one, measured with `sbx` v0.45.1.
- On Windows there are no descriptor-relative calls. The backend refuses reparse points, and relies on the guest being unable to create a link in its workspace, measured with `sbx` v0.45.1.
- Writes land in a temporary file and are renamed into place, so a write never goes through what stood at the destination.
- `remove` and `reclaim` both remove in the guest, at the guest's own authority. `remove` first runs the host-side path check, then `rm -f`, which refuses a directory swapped in after the check, or `rm -rf` when recursive. `reclaim` checks placement, then runs the host-side check on the target, then `rm -rf`: a relative target must name a child of the working directory, and an absolute one may be anywhere strictly inside the workspace. A guest keeps seeing a name for seconds after a host-side delete, so a host-side removal would leave it acting on a file that is gone.
- Names the host would change, hide or merge are refused: a name that stats but is not listed verbatim, such as a case variant on a case-insensitive host. On Windows, reserved characters, reserved device names and a trailing dot or space are refused too. The name check and the operation are separate calls, and neither NTFS nor APFS offers an exact-case open, so a guest racing them can make an alias appear in between. That redirects the operation only to another name inside the guest's own workspace, which the guest could reach anyway.

File methods reach only paths under the storage base's parent. Any other absolute path is refused with `ValueError`. `work_dir` must have a parent other than `/`, and that parent must not already exist in the image.

`workspace_root` defaults to a per-user state directory: `%LOCALAPPDATA%\maf-sandbox-docker-sbx\workspaces`, `~/Library/Application Support/maf-sandbox-docker-sbx` or `$XDG_STATE_HOME/maf-sandbox-docker-sbx/workspaces`. It is never the temporary directory, which a host may clean.

## Commands

`exec` runs argv verbatim, with separate, byte-exact streams and the command's own exit code. Every command runs through a small `sh` wrapper. The image needs `sh`, `base64`, `setsid`, `mount`, `unshare` with `--map-user` and `--map-group` (util-linux 2.37.2 on Ubuntu 22.04 has both), `mkdir`, `cat`, `rm` and `sleep`, and acquire checks for all of them:

- argv is base64-encoded, because `sbx` refuses an empty argument;
- a nonce on stderr marks where the command's own stderr starts, so a missing sandbox is never read as a command that exited 1;
- the command runs in its own process group. When `timeout` expires, the backend kills that group with a second `sbx exec`, bounded by `exec_cleanup_timeout_seconds`, and then raises `TimeoutError`. Killing the `sbx` client alone would leave the command running. The same command first leaves a cancel file, which the wrapper checks after recording its group, so a command that has not started yet never will. Only the expired command is touched; a sibling call in the same sandbox keeps running. If the kill cannot run at all, the sandbox is retired: every handle to it refuses further use, and the next acquire replaces it. The retirement is written to the sandbox's record, so a backend in another process, or after a restart, replaces it too. The kill reaches only the command's own process group. A command that starts its own session, or rewrites the pid file its user can write, outlives it; only disposal ends every process in the sandbox.
- output past 8 MiB, stdout and stderr together, ends the command the same way and raises `SandboxExecOutputLimitExceeded`, so a guest printing without end cannot fill host memory. The sandbox is kept unless the kill fails. `exec_bounded(..., max_output_bytes=n)` does the same with a budget of `n` bytes, which the wrapper's nonce line counts against.

A missing working directory exits 125 with the shell's message on stderr.

A host-tool run also requires `mkdir`, `mv` and `nohup`; the held-session startup checks these before releasing the transport to launch the program. The workload supplies its interpreter.

## Network access

Every sandbox is created with `--deny-network "**"`. A per-sandbox deny beats every global allow rule and every allow added later, so the sandbox has no network whatever the host's global policy says. A denied raw TCP connection still connects to Docker's proxy and then carries no data.

`Egress.ALLOWLIST` opens exactly the hosts in `egress_allow`, with `EgressRule` methods and paths enforced by the `sbx` proxy, so the backend declares `EGRESS_METHODS` and `EGRESS_PATHS`. The sandbox is created closed, gets its rules, and only then loses the `**` deny, so no command runs while it is open wider. `api.example.com` allows that host on every port. `*.example.com` allows subdomains at any depth and denies `example.com` itself unless it is listed too. Listing `example.com` beside it only with a method or path rule is refused, since `sbx` cannot keep that rule next to the wildcard. Use `Egress.CLOSED` for no outbound access; it keeps the sandbox closed and needs none of the host checks below. A path `/v1/*` allows `/v1` and everything under it. `authority` rules are refused.

Global allow rules apply to every sandbox, so the backend reads them at acquire and denies each one for the new sandbox. That works only when the host's state lets the allowlist be exact, and acquire refuses with `SbxHostNotConfined` when it does not:

- a global allow that admits a requested host and more, such as `**.github.com` when `api.github.com` is requested, since denying it would deny the requested host too. A global allow that admits only requested hosts is left in place. The global `**` rule of `sbx policy init allow-all` is always refused;
- a stored service secret, global or for this sandbox, since `sbx` does not report which domains it is injected into. Remove it with `sbx secret rm`. The same goes for secrets `sbx` takes from the host environment;
- a custom secret whose target the allowlist reaches;
- active organization governance, under which this host's rules do not apply.

The host can change this state after acquire. So before every command, and at every warm acquire, the backend reads the rules, the secrets and the governance state again. If a new global allow admits a host beyond the allowlist, a service secret or a custom secret for an allowed host appears, governance becomes active, or any of the sandbox's own rules differs from what `sbx` reported when it opened, the command is refused and the sandbox retired; the next acquire replaces it with fresh rules. A new global allow whose hosts the allowlist already admits for every request is left in place. A process already running when the host changes keeps that access until it ends.

A sandbox keeps the allowlist it was created with. Acquiring its key with different `egress` or `egress_allow` raises `ValueError`; dispose it first.

## Ownership, cleanup and retention

`sbx` has no labels, so ownership is in the name: `name_prefix`, then digests of the conversation, the whole key and the kind. Two creates racing one name get one sandbox and a conflict.

`dispose` and `dispose_scope` run `sbx rm --force` on every name with the matching prefix, taken from `sbx ls` and from the workspace directories. Afterwards they delete only what was on the host before the removal, never a workspace made after it, since the name is then free for another process to create again. `sbx rm` accepts only a name, so disposing of one instance checks its id in `sbx ls` first, then removes by name: another process that replaces the instance in between loses the replacement. The directories are a second record because the daemon can lose its engine and then report no sandboxes at all. That failure surfaces as `SbxDaemonFault`, which names `sbx daemon restart`, and a disposal reports it as `unreachable` rather than as a sandbox that is gone.

Nothing expires on its own. A sandbox left behind by a crashed host keeps its `cpus` and `memory` until a disposal or `sbx rm` removes it. An idle sandbox stops after 30 seconds and keeps its files; the next command starts it again, in 0.5 to 1.2 seconds on the `ubuntu-24.04` runner.

## Verification

The live suite in `tests/test_sbx_e2e.py` runs the shared storage-base, `FILES_IN`, `FILES_OUT`, `FILES_DELETE`, `RECLAIM`, reach, `EXEC`, `EGRESS` and `EGRESS_METHODS` conformance suites against a real sandbox, checks by response content that the network is closed, and checks that wildcard and path rules admit what they name and nothing else. Set `MAF_SANDBOX_SBX_E2E=1`, and `MAF_SANDBOX_SBX_PATH` when `sbx` is not on `PATH`.

`tests/test_sbx_e2e_host.py` checks the refusals and faults that depend on host-wide state: SSH agent forwarding, a registered MCP server, a lapsed login, a daemon that has lost its engine (its `docker.sock` hidden), a global allow added after acquire, and a custom secret for an allowed host. Most of these tests change that state and put it back, so they also need `MAF_SANDBOX_SBX_E2E_HOST=1`. Set it only on a host no one else is using. The login test logs back in with `DOCKER_USERNAME` and `DOCKER_PAT`.

Tested with `sbx` v0.45.1 on Windows 11 and on GitHub's `ubuntu-24.04` runner. The [2026-10-01 Linux live run](https://github.com/sokolaidev/maf-extensions/actions/runs/36861017552) with v0.46.0 passed 18 backend tests and 7 host-state tests; the suite runs nightly on `ubuntu-24.04`. Not tested on macOS, tracked in [#1499](https://github.com/sokolaidev/maf-extensions/issues/1499). The [host-tools live run](https://github.com/sokolaidev/maf-extensions/actions/runs/37008434303) on the same platform/version passed 30 backend/CodeAct tests and 7 host-state tests. The recurring Linux suite covers idle and slow host tools, timeout, cancellation, sibling survival, forced stop, disposal and a CodeAct workload ([#1614](https://github.com/sokolaidev/maf-extensions/issues/1614)). Windows and macOS host-tool execution have not been measured. `sbx` is closed source and in early access, and its behaviour has changed between releases, so re-run the live suite on every `sbx` version you install.
