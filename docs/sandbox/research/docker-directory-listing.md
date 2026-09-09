# Directory listing through Docker's archive stream

> Investigation for [#353](https://github.com/sokolaidev/maf-extensions/issues/353): whether the engine archive can serve `FILES_LIST`, and what discovery would cost. The decision is to keep the capability withheld because a complete listing transfers the subtree. The decided content lives in [the Docker backend](../backends/docker.md#files_list-is-withheld-because-a-listing-transfers-the-subtree) and [capabilities](../capabilities.md).

Docker can expose directory entries without asking the guest to enumerate them. The reason to decline `FILES_LIST` is the cost of its recursive archive, not the absence of an engine mechanism. A ceiling could bound that cost, but it would turn even a small one-level listing into a refusal whenever an unrelated descendant exceeded the budget.

## Method and scope

Measured on 2026-09-09 with Docker client and Engine 29.7.2, API 1.55, a Windows client and Docker Desktop's Linux engine. [The measurement script](../../../scripts/measure_docker_listing.py) creates a disposable container from a local Python 3 image, writes known fixtures, stops the container, and reads `docker cp <container>:<directory> -` using the host's streaming tar parser. Guest Python constructs the fixture only; no guest program supplies the observations. The container has no network, host mounts or extra capabilities and is removed in `finally`. Allow about 1.4 GiB of temporary container storage for the fixtures.

```bash
uv run python scripts/measure_docker_listing.py --image python:3.13-slim --repeats 3
```

The image must already exist locally. Each result records actual CLI stdout bytes through EOF, archive member counts, the first and last headers with offsets, and elapsed time including process startup. Bodies are consumed without extraction or retention. The byte-ceiling probe is a measurement of refusal, not a proposed `Sandbox.list_dir` implementation: it does not implement confinement, the production metadata rules, or the shared conformance suite.

## Headers are usable, but children are interleaved with descendants

For each ordinary directory, the first header represented the source directory itself, followed by its contents. The observed order was lexical and depth-first. In the nested fixture the sequence was `nested`, `nested/a-subtree`, `nested/a-subtree/large`, then `nested/z-last`. The final immediate child's header began at byte 167,773,696, after the nested file's 160 MiB body. Filtering returned names to one level does not avoid reading that body.

This order is an observation, not an API guarantee to build an early-stop rule on. A directory header contains neither its child count nor an offset to the next sibling. The stream has to finish to establish that no more immediate children remain; a known fixture count cannot become a completion rule for an unknown directory. Even a flat directory's final file body must be passed to find the archive terminator.

## Cost follows bytes as well as entry count

The table reports three complete copies per fixture, with medians and ranges in seconds. Files held ordinary repeated bytes, without hard links or sparse encodings. No compression option was used.

| Fixture | Immediate children | File content bytes in subtree | Archive bytes | Median seconds | Range seconds |
|---|---:|---:|---:|---:|---:|
| Empty directory | 0 | 0 | 1,536 | 0.265 | 0.210–0.303 |
| Ten one-byte files | 10 | 10 | 11,776 | 0.262 | 0.194–0.269 |
| One thousand one-byte files | 1,000 | 1,000 | 1,025,536 | 0.738 | 0.575–0.909 |
| Ten 1 MiB files | 10 | 10,485,760 | 10,492,416 | 0.288 | 0.259–0.422 |
| Ten 16 MiB files | 10 | 167,772,160 | 167,778,816 | 1.145 | 0.997–1.315 |
| Ten 100 MiB files | 10 | 1,048,576,000 | 1,048,582,656 | 7.517 | 6.699–12.545 |
| One subdirectory holding 160 MiB, plus a one-byte sibling | 2 | 167,772,161 | 167,775,744 | 1.367 | 1.325–1.415 |

For these short-name fixtures, the exact byte count was `512 * members + sum(ceil(file_size / 512) * 512) + 1024`, counting the source directory as a member. Extended metadata can add headers. The counts show the unavoidable body transfer for ordinary files over this stream; the timings describe this machine, not a throughput promise. A remote engine would carry the archive over that connection too.

## Stopping the reader is not completing a listing

Killing and reaping the CLI after reading a 65,536-byte prefix of the 1,000 MiB fixture took 0.188, 0.128 and 0.129 seconds. Those prefixes omit nine immediate children. The next copy of the same container's empty directory took 6.295, 4.816 and 13.264 seconds, compared with the ordinary 0.210–0.303 seconds above. Cleanup drains the killed CLI's remaining pipe output; the prefix count is not a measurement of every byte the daemon produced.

Early cancellation saves the reader from processing the archive to completion, but these observations do not establish prompt daemon cancellation or a bound on daemon-side read-ahead. They do show that returning quickly from a killed CLI does not promise that the next backend call is cheap. No engine-side I/O counters were collected.

## Symlinks and the source-path distinction

The directory fixture held a file link, a directory link and a dangling link, all pointing outside the listed directory. Each arrived as tar typeflag `2`, size zero, with its link target intact. Neither the external file nor the directory's contents appeared. Passing `-L` while copying that ordinary directory produced the same four members and 3,072 bytes: it did not recursively follow internal links. Copying a source link without `-L` yielded just that link's header and archive terminator.

The Windows client's `-L` copies of both relative and absolute source-directory links failed, asking the daemon for `/listing/listing/links` instead of `/listing/links`. This run therefore does not claim a successful source-link-following measurement. The [CLI implementation's `copyFromContainer`](https://github.com/docker/cli/blob/v29.7.2/cli/command/container/cp.go) resolves the requested source when `followLink` is set and then makes the archive request; it does not set a recursive link-follow option. The [copy command documentation](https://docs.docker.com/reference/cli/docker/container/cp/) likewise describes `-L` in terms of the source path.

Archiving internal symlinks as link entries is necessary, but does not prove confinement. Any future implementation must refuse a linked directory and linked ancestors, confine every returned name, and run the four shared `FILES_LIST` probes against a real engine. This investigation leaves the declaration unchanged and does not claim those probes passed.

## The HTTP API has the same cost

The [Engine API 1.55 specification](https://docs.docker.com/reference/api/engine/version/v1.55.yaml) defines `GET /containers/{id}/archive` as a tar response and `HEAD` as metadata for one named path in `X-Docker-Container-Path-Stat`. GET has no headers-only, depth, pagination or entry-count parameter. Switching from the CLI to HTTP does not provide a one-level index; HEAD cannot discover names the caller does not know. This is a contract and source review, not a separate live HTTP benchmark.

The [Moby archive handler](https://github.com/moby/moby/blob/v28.3.3/daemon/archive_unix.go) constructs a streaming tarballer. The [archive implementation](https://github.com/moby/go-archive/blob/v0.1.0/archive.go) walks directories recursively, reads link targets with `Lstat`/`Readlink`, and writes regular file content after its header. Those source versions explain the mechanism; their ordering and cancellation details are not a guarantee for every compatible engine.

## Decision and alternatives

Keep `FILES_LIST` withheld. The backend can cheaply stat a named file's metadata while declining to transfer all descendants merely to discover names. An unbounded listing would make the work directory's unrelated contents control the cost of every discovery call. A ceiling would be honest only if excess bytes, entries, metadata or elapsed time raised before any listing was returned. The script's 1 MiB ceiling against the nested fixture does exactly that: it raises `archive byte ceiling exceeded; listing is incomplete` instead of returning the directory seen before the missing sibling. No production ceiling is being selected.

Reject `docker exec ... ls` or `find`: the guest controls those executables and what they report. The protocol's description of listing as the least trusted enumeration concerns the returned names as data; it does not authorize the guest to author the observation. Parsing a guest-produced tar has the same problem.

[#352](https://github.com/sokolaidev/maf-extensions/issues/352) is already closed. Its historical claim that Docker had no enumeration primitive was too strong, but the transport's current portability constraint remains: this backend offers `FILES_OUT` and withholds `FILES_LIST`. Discovering request files by name avoids repeatedly copying a growing request/response subtree. The capability split and literal output paths therefore remain justified without treating archive-based discovery as impossible.
