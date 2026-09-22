> Exploration and live measurements for [#1203](https://github.com/sokolaidev/maf-extensions/issues/1203) and [#1338](https://github.com/sokolaidev/maf-extensions/issues/1338): WSLC input placement authority and the check/placement boundary. The first record below measured the root archive copy. The second records how writes and setup avoid it, and the last how setup is kept from acting as root where a swap could reach. The [backend contract](../backends/wslc.md#write-checkcopy-residual) states the result.

# WSLC write placement and parent swaps

Measured on 2026-09-13 with WSLC/WSL **2.9.4.0**, kernel **6.18.35.2-1**, Windows **10.0.26220.9223**, Python **3.13.12**, and maf-extensions at `190505961b0e19caf8b70b47f7a41246aa7c560b`. The non-root fixture is built from the checked-in [guest-owned Dockerfile](../../../packages/maf-sandbox-wslc/tests/fixtures/guest-owned/Dockerfile): Azure Linux core 3.0, uid/gid `10001:20001`, with a guest-owned working directory. The image used for measurement had ID prefix `68a4c6f59a37`. No Docker CLI measurement is substituted for a WSLC result.

## Deterministic interleaving

The [boundary tests](../../../packages/maf-sandbox-wslc/tests/test_wslc_write_window.py) run the backend's real classifier, tar builder and WSLC runner. Immediately before the stdin-to-container copy is submitted, a runner wrapper uses an exec **as the image user** to rename the checked parent and replace it with a link to `/protected`. This target is root-owned mode `0700`; a direct guest write fails with `Permission denied` before the measurement. Setup and result inspection use root, but the swap does not. Each case uses a separate network-isolated container, disposed in `finally`.

| Checked shape | Change immediately before extraction | WSLC result |
|---|---|---|
| Existing parent, write a file beneath it | Rename parent and link it to the protected directory | Copy succeeds; file lands under `/protected`, outside the working directory |
| Existing parent, create a missing child and file | Same swap | Copy succeeds; new guest-owned directory and file land under `/protected` |
| Missing parent included as a tar directory entry | Plant a link at that exact missing path | Copy succeeds; that entry replaces the link with a directory; this interleaving does not redirect the file |
| `prepare_work_dir`, existing parent and missing child/base | Rename parent and link it to the protected directory | Copy succeeds; intermediate directory lands root-owned under `/protected`, base lands with guest ownership |
| `prepare_work_dir`, missing parent included in archive | Plant a link at that exact missing path | Entry replaces the link; this interleaving does not redirect creation |

The protected directory remains `0:0:700`; escaped files are `10001:20001:644`, and the new base is `10001:20001:755`. These results establish **root-equivalent placement authority independently of tar ownership**. They do not establish host-filesystem escape: the protected target is inside the container. Replacement of a link at an explicit directory entry is not a closure proof: an existing prefix is still re-resolved, and the guest can mutate the filesystem while extraction runs.

Planting the link **before classification** is refused without submitting an input archive. Cancelling at the check/copy boundary likewise submits no archive and leaves the container usable. Offline tests also cover cancellation during classification and during the copy await, and an engine extraction error. Cancellation after submission cannot promise rollback or absence of partial files; killing the host CLI is not a transaction abort in the engine.

There is no unpaused statistical race gate here. All five shapes use an explicitly ordered swap, and all five also exercise pre-check refusal and pre-submission cancellation. These are characterization tests of the stated residual; a future closure should change their expected outcomes.

Reproduce from the repository root in PowerShell:

```powershell
wslc image build -t maf-sandbox-wslc-guest-owned:ci packages/maf-sandbox-wslc/tests/fixtures/guest-owned
$env:MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE = 'maf-sandbox-wslc-guest-owned:ci'
uv run pytest packages/maf-sandbox-wslc/tests/test_wslc_write_window.py -q
```

## Available mechanisms and their cost

The installed CLI help and upstream source were checked separately. Upstream `master` was [`eaa69e766cf375d96053207a4ba8858f54ea1536`](https://github.com/microsoft/WSL/tree/eaa69e766cf375d96053207a4ba8858f54ea1536); it is source evidence, not a live test of that revision. [`ContainerCpCommand::GetArguments`](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/wslc/commands/ContainerCpCommand.cpp) exposes no user or no-follow selector. [`WSLCContainerImpl::UploadArchive`](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/wslcsession/WSLCContainer.cpp) calls the internal runtime's `PutArchive` with the container ID and destination string. Its shared object lock does not span the backend's earlier checks or prevent guest filesystem mutations.

| Candidate | Evidence and cost | Decision |
|---|---|---|
| Trusted constrained upload / held resolution | No base-relative no-follow upload exposed by the inspected CLI or [COM interface](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/service/inc/wslc.idl) | Request upstream; unavailable to this backend today |
| Guest freeze while copying | No pause/unpause command in installed help or upstream commands, nor a freeze method or paused state in the [public SDK](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/WslcSDK/wslcsdk.h) | No supported operation to time or adopt; no thaw/recovery implementation is claimed |
| `container kill --signal SIGSTOP` / `SIGCONT` | Both returned 0, but a new guest exec between them returned `still-running`; SIGCONT and disposal completed | Process signalling does not freeze the container; not an alternative to a freezer |
| Stop/start | Ends and restarts workload processes, losing running exec state; the remaining classifier also requires guest exec | Changes lifecycle semantics, not an acceptable transparent file-operation guard |
| Transfer through guest exec | Can bound placement to the image user, but requires a trusted helper or extra utilities; the shared shell route needs `sh`, `base64`, `mkdir`, `mv` | Additional image contract and transfer cost; not adopted in #1203, adopted in #1338 with stdin instead of base64 |
| Keep engine tar and state the residual | No new image dependencies or extra engine round trips | Selected in #1203; replaced in #1338 |

Two exploratory passes measured input-copy subprocess durations of roughly **35–163 ms** and whole checked operations of **0.29–0.93 s**, including the deterministic swap exec. These small local samples are descriptive, not performance guarantees. No freeze timing exists because no supported freeze operation was found. No guest-helper benchmark was performed because that transport was not selected.

Even a future freeze command is insufficient by itself for the current algorithm: classifying an accepted non-directory copy source still executes guest `test`, which cannot be assumed to run under a real freeze. It also needs trusted metadata that can be read while frozen. The output-archive request [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) may supply type metadata but cannot alone hold resolution through upload. Bypassing WSLC to reach its internal runtime is not a supported WSLC API contract.

## Decision in #1203

Retain `FILES_IN` with a prominent, measured residual beside the capability declaration, including `prepare_work_dir` on cold acquire and warm repair. Host-side serialization alone cannot stop guest background processes. Non-root archive ownership is not a bound, and neither a second stat nor a guest shell check closes the interval. Workloads requiring confinement against concurrent guest mutation must use a backend with a supported closure or avoid this input plane.

The focused upstream request is for **constrained archive upload with held, no-follow resolution**. Filed upstream as [microsoft/WSL#41594](https://github.com/microsoft/WSL/issues/41594) (open), tracked locally by [#1203](https://github.com/sokolaidev/maf-extensions/issues/1203). A freeze-based alternative would require engine-authenticated metadata during freeze and the concurrency, exec, cancellation, failed-thaw and warm-recovery requirements recorded in [#1130](https://github.com/sokolaidev/maf-extensions/issues/1130). No such lifecycle implementation or live freezer validation is claimed by this record.

## Writes as the image user and held setup (#1338)

Measured on 2026-09-21 with WSLC/WSL **2.9.12.0**, kernel **6.18.40.1-1** and Windows **10.0.26220.9472**. microsoft/WSL#41594 was still open with no maintainer reply, and the installed CLI still had no pause command and no `cp` option beyond `--archive` and `--quiet`. The engine archive copy is no longer used for input.

**Writes run as the image user.** `write_file` sends the content on the stdin of one `container exec -i` without `--user`. That command creates missing parents with `mkdir -p`, writes a sibling named for the call, compares its size with `wc -c`, and renames it into place with `mv -f`. A 1 MiB write took 0.13 s and a 32 MiB write 0.31 s, both byte-identical by SHA-256. A plain exec took 0.11 s.

Killing the host `wslc` process closes stdin in the container. `cat` then exits 0 with the bytes that had arrived: 100,000 of them in the measurement, left in the staged file. So the size check is required — without it a cancelled write would publish a truncated file, and with it the command removes the sibling and exits by itself. That is what the **content** is protected by; it is not a reason to keep the container. The same measurement is why a stopped write discards it: the guest command outlives the host process, so an expired deadline, or stdout reaching the read cap, leaves work that a warm acquire would race. Short-input cleanup and container disposal answer two different halves.

**Setup runs as root inside held directories.** A non-root user cannot create `/maf-sandbox`, so setup stays root. One `/bin/sh` command enters the deepest existing directory with `cd -P`, compares `pwd -P` with the expected path, and creates each missing directory with `mkdir` relative to the directory it holds. It then enters and confirms each new directory the same way. `mkdir` without `-p` fails on an existing link, and `getcwd` reports where the held directory physically is. A link anywhere on the path makes the comparison fail. `chown` on `.` gives the base to the image user. `PATH` is pinned to `/usr/sbin:/usr/bin:/sbin:/bin`, so a directory the image user can write cannot supply a command that runs as root.

Both commands were run by hand on the guest-owned fixture (bash, coreutils, uid 10001) and on `python:3.13-alpine` (BusyBox 1.37.0 `ash`). They were also run under Debian's `dash` as a non-root user through the offline shell tests. All three handled short content, a directory at the target, a destination the user cannot write, an existing file, empty content, a swapped parent, a link planted at a missing directory, and names beginning with `-`.

The [boundary tests](../../../packages/maf-sandbox-wslc/tests/test_wslc_write_window.py) now place the swap immediately before the placement command. Setup and inspection use root; the swap uses the image user.

| Checked shape | Change immediately before placement | Result |
|---|---|---|
| Existing parent, write a file beneath it | Rename parent and link it to `/protected` | `PermissionError`; `/protected` stays empty |
| Existing parent, create a missing child and file | Same swap | `PermissionError`; `/protected` stays empty |
| Missing parent | Plant a link at that exact path | `PermissionError`; the link is left in place |
| Setup, existing parent and missing child/base | Rename parent and link it to `/protected` | Refused: the parent no longer resolves to itself |
| Setup, missing parent | Plant a link at that exact path | Refused: `mkdir` meets the link |
| Warm acquire recreating a missing base | Replace its parent with a link | Refused the same way; `/protected` stays empty |

The controls, with nothing swapped, land where asked. A write is `10001:20001:644`. A new intermediate directory from setup belongs to root and the base to `10001:20001`. The fixture's base is setgid, so a directory *created* beneath it takes its group and the bit — which is what the intermediate keeps, since setup never chowns it. The base does not keep the inherited group: the ownership step sets `uid:gid` explicitly, so its group becomes the image user's whatever the parent's was. Measured: `mkdir` under a setgid group-20001 parent gives `0:20001 2755`, and `chown 10001:10001` then gives `10001:10001 2755` — the group is overwritten and the setgid bit survives. The fixture cannot show that on its own, because its parent group and its image user's group are both 20001; the control with a differing group is in the live suite. A write with `working_directory="/etc"` raises `PermissionError` and leaves nothing. Root could have placed it before this change.

`exec --interactive` is declared in the upstream CLI source from [2.9.3](https://github.com/microsoft/WSL/blob/2.9.3/src/windows/wslc/commands/ContainerExecCommand.cpp) on, the backend's stated floor. It was run live only on 2.9.12.0.

**What remains.** A write is bounded, not atomic: a swap can still send it to another place the image user can write. Cancellation after the command starts is not a rollback: content that fully arrived still lands. A staged `.maf-<hex>.part` sibling remains if the command itself is interrupted. Root `test` in path checks still resolves through the image's `PATH`; setup's pinned `PATH` does not cover it.

## Review follow-ups (#1380)

Four areas re-examined after the first pass, measured on 2.9.12.0 with bash, BusyBox `ash` and Debian `dash`.

- **Inherited shell settings.** The setup loop's `mkdir` then `cd -P ${directory##*/}` uses a relative `cd`, so an inherited `CDPATH` diverts it: with `CDPATH=/decoy` and a same-named `/decoy/maf-sandbox`, `cd -P maf-sandbox` entered `/decoy/maf-sandbox` on both `ash` and `bash`. It failed safe — the `pwd -P` comparison refused and nothing landed in the decoy — but setup broke. Fixed by clearing `CDPATH` in the pinned environment of both root commands. `pwd -P` reports the physical directory, so the logical/physical comparison holds for a real base; an ancestor swapped to a link makes them differ and refuses.
- **Partial-setup recovery.** Setup was one command that created the base and chowned it. A failure or kill between the two left the base root-owned, and because `ensure_guest_work_dir` only creates a missing suffix, a later acquire found the base present and returned it — `write_file` then failed with `PermissionError`, an unusable base handed back. Reproduced directly. The chown is now a separate held command, `_ensure_base_owner`, run **only for a base the acquire created**: a first attempt to run it on every prepare was withdrawn because it chowned an existing base, and `work_dir=/etc` then handed `/etc` to the image user (measured). What covers the interruption instead is that a timed-out setup removes the container, so the half-prepared base goes with it; a host killed outright still leaves a root-owned base, and the first write says so.
- **Cancellation and blocked utilities.** A write blocked at a hung `mv`, then cancelled, raised `CancelledError`; the container stayed reusable and a `.maf-<hex>.part` sibling remained (documented). A write blocked at a hung `wc` raised `TimeoutError` at the command deadline. `write_file` did not discard the container on timeout, unlike `exec`; fixed to remove it, since killing the host process does not reliably reach the in-container command. The byte-count check means an interrupted or non-streaming write refuses rather than publishing a short file.
- **Minimum engine version.** `exec` with `--interactive`, `--user` and `--workdir` is present in the CLI source from the declared [2.9.3](https://github.com/microsoft/WSL/blob/2.9.3/src/windows/wslc/commands/ContainerExecCommand.cpp) minimum. Live evidence is 2.9.12.0 only. The floor is not a silent-correctness risk: a version whose `exec -i` did not stream stdin would fail the write's byte-count check, and a version whose `cd -P`/`pwd -P` differed would fail the setup comparison — both refuse rather than corrupt.

## Setup bounded by the reach rule (#1338)

Measured on 2026-09-22 with WSLC/WSL **2.9.12.0**, kernel **6.18.40.1-1** and Windows **10.0.26220.9492**. microsoft/WSL#41594 was still open with no maintainer reply, and 2.9.12 was still the latest release.

**The residual #1380 left.** The `pwd -P` comparison rules out a link, not a real directory. A guest that can write the directory holding a component can rename a different real directory into that name. Root then created inside the substitute and gave the base away. The live suite shows it: with the check below removed from a copy of the package, a root-owned directory the guest could move — it sat in the guest-owned base — was renamed into the base's parent, and setup created the base inside it without an error.

**The rule.** Root acts inside a directory only when that directory is root's and writable by nobody else. This is the predicate of `path_ancestors_are_host_owned`, and it is enough because a rename needs write permission on the directory that holds the name. The command walks from `/`, holding each directory, and checks the held one before it enters or creates anything inside it. The owner comes from `test -O`, a builtin of bash, Debian `dash` and BusyBox `ash` alike. The mode comes from `ls -ld` on the pinned `PATH`, and a group or other write bit fails the check, as does an `ls` that fails or prints no directory mode. POSIX ACL entries are capped by the mask, and `ls` shows the mask in the group bits, so an ACL granting write reads as group-writable. That last point was not measured, because no fixture image carries `setfacl`.

| Directory, guest-owned fixture | Mode, owner, group | Root may act inside |
|---|---|---|
| `/` | `drwxr-xr-x 0 0` | yes |
| `/maf-sandbox` | `drwxr-xr-x 0 0` | yes |
| `/maf-sandbox/work` | `drwxrws--- 10001 20001` | no |
| `/tmp` | `drwxrwxrwt 0 0` | no |

`python:3.13-alpine` answered the same for `/`, `/etc` and `/tmp`.

**Elsewhere, the image user creates the base.** At the first directory that fails, root creates nothing more and prints a marker. A second exec without `--user` then runs `mkdir -p` as the image user, whose permissions bound where a swap can send it. The chown went back into the creating command, still only for a base it created, so the separate ownership command and the window between the two are gone. Costs: the default base, under a missing or root-owned `/maf-sandbox`, takes one setup exec instead of two. A base left to the image user takes two: root's walk, then that user's `mkdir` (a plain exec took 0.11 s). Setup now also needs `ls` on the pinned `PATH`.

| Setup, 2.9.12.0 | Change before placement | Result |
|---|---|---|
| Base under the guest-owned base, missing child/base | Parent replaced by a link to `/protected` | `PermissionError` from the image user's `mkdir`; `/protected` stays empty |
| Same | Parent replaced by a root-owned real directory | `PermissionError`; the substitute stays empty |
| Warm acquire recreating a missing base | Parent replaced by a link, or by a root-owned real directory | `PermissionError`; nothing lands in either |
| Base under `/maf-sandbox`, root's alone | A root-installed `mkdir` wrapper swaps the new directory for a link | Refused: it no longer resolves to itself. Only root can make this swap here |

The controls, with nothing swapped: under the guest-owned base, the image user creates both directories, `10001:20001:2755`, keeping the setgid parent's group. Under `/maf-sandbox`, root's intermediate is `0:0:755` and the base `10001:20001:755`. Under a setgid, group-0, `2755` parent, the base is `10001:20001:2755`.

**Behaviour change.** A base under a directory root does not own, or under any group- or world-writable directory, is created by the image user. Where it cannot create it, for example in a root-owned directory beneath such a one, acquisition raises `PermissionError` where root used to create the base. The `/maf-sandbox/work` default changes only for an image that gives `/maf-sandbox` to its user, and that user can then create `work` itself.

**What remains.** Neither writes nor setup reach past the image user. A write is still bounded rather than atomic. The root command trusts the image's `/bin/sh`, whose builtin `test -O` answers the ownership check, and `mkdir`, `chown` and `ls` on the pinned `PATH`, as setup did before. An image whose user can write those can already run code as root through setup. #41594 would let setup act as root under guest-writable directories too, where this leaves the work to the image user.
