# Docker Sandboxes

Docker Sandboxes runs one microVM with its own Linux kernel per sandbox, through Docker's `sbx` CLI, on Windows, macOS and Linux. It is the only backend that serves a local POSIX guest with a shell at the default `MICROVM` floor. Use the [package README](../../../packages/maf-sandbox-docker-sbx/README.md) for setup. The [research record](../research/docker-sandboxes-backend.md) holds the measurements this page rests on.

## Supported contract

| Setting | Value |
|---|---|
| Host | `sbx` installed and signed in; SSH agent forwarding off; no MCP server registered; an event loop that supports subprocesses |
| Isolation | `MICROVM` |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM` |
| Network | `CLOSED` |
| Guest OS | POSIX |
| Sharing | `CONVERSATION` |
| Transfer limits | `DEFAULT_SANDBOX_LIMITS` |
| Cleanup | Disposal or reclaim; no `SNAPSHOT` |

The isolation claim holds only while two host-wide settings hold, and the backend reads both at every acquire. SSH agent forwarding, on by default, gives each sandbox a socket to the host's agent. A registered MCP server is reachable through the host's MCP gateway even under a deny-all rule. The backend refuses with `SbxHostNotConfined` when either fails, and never changes host settings itself.

## The workspace file plane

Each sandbox mounts one fresh, private host directory and nothing else. At create, the backend binds that mount again at the storage base's parent, as root, so `/maf-sandbox/work` is a real directory in the guest. The parent must not already exist in the image. An auto-stop drops the bind. Every command checks a marker the host keeps at the workspace root, and runs nothing while it is missing: the backend binds the mount again and retries once.

Stats, reads, listings and writes act on the host side of the mount, so no path check is answered by the guest. On macOS and Linux each component is opened relative to its parent with `O_NOFOLLOW`, which resolves and acts as one operation. A link the guest creates between a check and an operation makes the operation fail rather than follow the link. On Windows the backend refuses reparse points and acts by path. That rests on the guest being unable to create a link in its workspace: measured on `sbx` v0.45.1, `ln -s` created nothing in 13 attempts. A write lands in a temporary file that is renamed into place, so it never writes through what stood at the destination. Files the host creates are the host user's alone; the mount presents the host user to the guest as the sandbox's own user, so the guest can change them.

Names the host would change, hide or merge are refused. That covers a name that stats but is not listed verbatim, such as a case variant on a case-insensitive host. On Windows it also covers reserved characters, reserved device names, and a trailing dot or space.

Both remove in the guest, at the guest's own authority, but they check different things first. `remove` runs the host-side path check, the same no-follow walk the other file methods use, then `rm -f` for `remove(recursive=False)`, which refuses a directory swapped in after the check, or `rm -rf` when recursive. `reclaim` checks placement: a relative target must name a child of the working directory, and every target must lie strictly inside the workspace. It then runs the same host-side check as `remove` on the target, which refuses a case variant or other alias the host would merge, and a link on the path, before `rm -rf`. The guest's lookups fold case as the host's do, so without that check `reclaim("upper")` would remove `Upper`. A guest that has looked a name up can keep seeing it for several seconds after a host-side delete, which broke the `FILES_DELETE` suite. The guest's authority reaches only its own VM and this workspace, so a swapped component cannot redirect a removal to anything the guest could not delete itself.

## Commands

`sbx exec` passes argv verbatim with separate, byte-exact streams and faithful exit codes. It refuses an empty argument, and killing the client leaves the guest process running. So every command runs through a wrapper:

- argv is base64-encoded;
- a nonce on stderr marks where the command's own stderr begins, and its absence means the wrapper never ran;
- the command runs under `setsid`, and an expired `timeout` kills its process group with a second `sbx exec`, bounded by `exec_cleanup_timeout_seconds`. A command that has not recorded its group by then may still start, so the backend stops the sandbox instead, which kills every process and keeps the files (5.6 s measured).

The image needs `sh`, `base64`, `setsid`, `mount`, `mkdir`, `cat`, `rm` and `sleep`. Acquire checks for all of them.

## Ownership and disposal

`sbx` has no labels. A sandbox's name is a prefix plus digests of the conversation, the whole key and the kind, and its workspace directory has the same name. Disposal runs `sbx rm --force` on every matching name from the listing and from the workspace directories. When the daemon loses its engine, it lists no sandboxes, and the directories are what still find them. A daemon reporting "backend unavailable" raises `SbxDaemonFault`, and a disposal reports it as `unreachable`.

## Unsupported operations

| Operation | Reason |
|---|---|
| `run_code` | Any image is accepted, so the runtime is the image's. |
| `reset` | A delete and recreate from a saved template would meet the contract, at the cost of a fresh create. Not built. |
| `HOST_TOOLS` | An idle sandbox stops 30 seconds after its last session and kills every process. The host-tool transport is not measured against that. |
| `ALLOWLIST` | Global allow rules apply to running sandboxes, so an exact allowlist depends on host state that can change after acquire. |
| `CALL` scope | Not declared; a create costs about 4 seconds on a warm host. |

## Status

| Area | State | Reference |
|---|---|---|
| Backend at the `MICROVM` floor, `CLOSED` only | Implemented; live suite green on Windows with `sbx` v0.45.1, and run on `ubuntu-24.04` by `sbx-live.yml` after merge | [#1412](https://github.com/sokolaidev/maf-extensions/issues/1412) (open) |
| macOS and Linux link behaviour in the workspace | Not measured; the first `sbx-live.yml` run after merge records Linux, and nothing covers macOS | [research record](../research/docker-sandboxes-backend.md) |
| `ALLOWLIST`, `HOST_TOOLS`, `SNAPSHOT` | Not implemented | untracked |
