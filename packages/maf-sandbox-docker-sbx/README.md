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

The backend checks two host-wide settings at every acquire and refuses with `SbxHostNotConfined` when either fails. It reads them and never writes them.

- **SSH agent forwarding must be off.** It is on by default, and it gives every sandbox a socket to the host's SSH agent.
- **No MCP server may be registered** (`sbx mcp ls`). The host's MCP gateway answers a sandbox even under a deny-all rule.

A lapsed login fails every `sbx` command until a person signs in again; the backend raises `SbxLoginRequired`. In CI, pipe an access token to `sbx login --username <user> --password-stdin`.

`sbx` collects telemetry by default. Set `SBX_NO_TELEMETRY=1` in the host's environment to opt out.

## Supported operations

| Setting | Behavior |
|---|---|
| Isolation | `MICROVM` |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM` |
| Guest OS | POSIX |
| Network | `CLOSED` |
| Isolation scope | `CONVERSATION` |
| Egress observation | No |

`RUN_CODE` is not declared, because any image is accepted and the runtime is the image's. `SNAPSHOT` is not declared. `HOST_TOOLS` is not declared yet: an idle sandbox stops 30 seconds after its last `sbx` session and kills every process, and the host-tool transport has not been measured against that.

`spec.image`, when set, is passed to `sbx create --template`. Without it the sandbox uses Docker's `shell` template, where commands run as `agent` (uid 1000) with passwordless `sudo`. An image without that user runs commands as root.

## Files: a workspace answered by the host

Each sandbox mounts one fresh, private host directory, `<workspace_root>/<sandbox name>/ws`, and nothing else. At create, the backend binds that mount again at the storage base's parent, as root. With the default storage base `/maf-sandbox/work`, that parent is `/maf-sandbox`. An idle stop drops the bind, and the next command binds it again before it runs.

Stats, reads, listings and writes act on the host side of the mount. No path check is answered inside the guest.

- On macOS and Linux each path component is opened relative to its parent with `O_NOFOLLOW`, so a link the guest creates between a check and an operation makes the operation fail rather than follow it.
- On Windows there are no descriptor-relative calls. The backend refuses reparse points, and relies on the guest being unable to create a link in its workspace, measured with `sbx` v0.45.1.
- Writes land in a temporary file and are renamed into place, so a write never goes through what stood at the destination.
- `remove` and `reclaim` both remove in the guest, at the guest's own authority. `remove` first runs the host-side path check, then `rm -f`, which refuses a directory swapped in after the check, or `rm -rf` when recursive. `reclaim` checks placement only, then runs `rm -rf`: a relative target must name a child of the working directory, and an absolute one may be anywhere strictly inside the workspace. A guest keeps seeing a name for seconds after a host-side delete, so a host-side removal would leave it acting on a file that is gone.
- Names the host would change, hide or merge are refused: a name that stats but is not listed verbatim, such as a case variant on a case-insensitive host. On Windows, reserved characters, reserved device names and a trailing dot or space are refused too.

File methods reach only paths under the storage base's parent. Any other absolute path is refused with `ValueError`. `work_dir` must have a parent other than `/`, and that parent must not already exist in the image.

`workspace_root` defaults to a per-user state directory: `%LOCALAPPDATA%\maf-sandbox-docker-sbx\workspaces`, `~/Library/Application Support/maf-sandbox-docker-sbx` or `$XDG_STATE_HOME/maf-sandbox-docker-sbx/workspaces`. It is never the temporary directory, which a host may clean.

## Commands

`exec` runs argv verbatim, with separate, byte-exact streams and the command's own exit code. Every command runs through a small `sh` wrapper. The image needs `sh`, `base64`, `setsid`, `mount`, `mkdir`, `cat`, `rm` and `sleep`, and acquire checks for all of them:

- argv is base64-encoded, because `sbx` refuses an empty argument;
- a nonce on stderr marks where the command's own stderr starts, so a missing sandbox is never read as a command that exited 1;
- the command runs in its own process group. When `timeout` expires, the backend kills that group with a second `sbx exec`, bounded by `exec_cleanup_timeout_seconds`, and then raises `TimeoutError`. Killing the `sbx` client alone would leave the command running. If the command has not recorded its group by the end of that allowance, it may still start, so the backend stops the sandbox instead. That kills every process in it and keeps the files; the next command starts it again.

A missing working directory exits 125 with the shell's message on stderr.

## Network access

Every sandbox is created with `--deny-network "**"`. A per-sandbox deny beats every global allow rule and every allow added later, so the sandbox has no network whatever the host's global policy says. A denied raw TCP connection still connects to Docker's proxy and then carries no data.

`ALLOWLIST` is not declared. Global allow rules apply to every sandbox, including running ones, so an exact allowlist would depend on host state that can change after acquire.

## Ownership, cleanup and retention

`sbx` has no labels, so ownership is in the name: `name_prefix`, then digests of the conversation, the whole key and the kind. Two creates racing one name get one sandbox and a conflict.

`dispose` and `dispose_scope` run `sbx rm --force` on every name with the matching prefix, taken from `sbx ls` and from the workspace directories. The directories are a second record because the daemon can lose its engine and then report no sandboxes at all. That failure surfaces as `SbxDaemonFault`, which names `sbx daemon restart`, and a disposal reports it as `unreachable` rather than as a sandbox that is gone.

Nothing expires on its own. A sandbox left behind by a crashed host keeps its `cpus` and `memory` until a disposal or `sbx rm` removes it. An idle sandbox stops after 30 seconds and keeps its files; the next command starts it again and binds the workspace in about 2 seconds.

## Verification

The live suite in `tests/test_sbx_e2e.py` runs the shared storage-base, `FILES_IN`, `FILES_OUT`, `FILES_DELETE`, `RECLAIM`, reach and `EXEC` conformance suites against a real sandbox, and checks by response content that the network is closed. Set `MAF_SANDBOX_SBX_E2E=1`, and `MAF_SANDBOX_SBX_PATH` when `sbx` is not on `PATH`.

Tested with `sbx` v0.45.1 on Windows 11. `sbx` is closed source and in early access, and its behaviour has changed between releases, so re-run the live suite on every `sbx` version you install.
