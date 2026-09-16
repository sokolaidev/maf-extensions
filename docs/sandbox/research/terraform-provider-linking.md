# One image with every provider, linked and never copied

> Analysis, 2026-09-16, in [#1279](https://github.com/sokolaidev/maf-extensions/pull/1279). It follows [baking every latest AVM module](terraform-avm-catalog.md). Nothing here is implemented.

## Question

Can one image hold every provider the Azure Verified Modules catalog needs, and can a call use them without copying provider bytes anywhere?

## Answer

Yes, by storing providers unpacked in the image's filesystem mirror instead of as ZIPs. Terraform then links each provider into the call's data directory. Measured on Docker and in a live ACAS sandbox: zero bytes copied, one symlink per provider, every link pointing into the image mirror.

Three things come with it. The mirror grows from 562 MiB of ZIPs to 2.52 GiB unpacked, which is the whole cost of the change. A call still *reads* each provider it uses, because Terraform hashes the package for the lock file. And Terraform falls back to a recursive copy, silently, whenever it cannot create the symlink, so the launcher has to check and refuse rather than trust it.

## How Terraform installs from a local mirror

A filesystem mirror holds either ZIPs or unpacked directories at `HOST/NAMESPACE/TYPE/VERSION/TARGET/`. The unpacked form is what avoids copying, and the provider binary must keep its executable bit. Python's `zipfile` does not preserve it, and the failure is quiet: with a `0644` binary, `init` still succeeds and links the package, and only `validate` fails, with `Failed to load plugin schemas`. Measured on Docker.

In Terraform 1.16.2 `installFromLocalDir` ([source](https://github.com/hashicorp/terraform/blob/v1.16.2/internal/providercache/package_install.go#L148), `internal/providercache/package_install.go` line 148) verifies the lock hashes, deletes anything at the target path, and then tries `os.Symlink` with an absolute target. If that fails it creates the directory and does a recursive copy, and the install still reports success. Nothing in the output distinguishes the two.

Two consequences follow from the same file. Terraform cannot verify a `zh:` hash against a directory, because that scheme covers the original ZIP, so a lock holding only `zh:` hashes is treated as holding no hashes at all and anything passes. `h1:` hashes are verified normally. After the install, the installer always computes the package hash to record in the lock ([source](https://github.com/hashicorp/terraform/blob/v1.16.2/internal/providercache/installer.go), `internal/providercache/installer.go`), so every provider a call uses is read in full even when no bytes are written.

Two other routes were rejected. A plugin cache directory does not avoid copying: when a package is not already in the cache Terraform installs it *into* the cache and links from there, which needs a writable cache, and since 1.4 it only uses a cached entry when the lock file already records a matching checksum. `dev_overrides` skips installation entirely, but it also bypasses version constraints and the lock file, which would make validation claim less than it does today.

## Measurements

### Docker

Mirror of all 27 provider versions, mounted read-only from a named volume, one fresh container per root, no network, page cache dropped before each cold run. Terraform 1.16.2, 4 CPUs.

| Root | Providers | `init` cold | `init` warm | `init` with read-only lock | Bytes copied | Symlinks | Peak memory |
|---|---|---|---|---|---|---|---|
| typical: azurerm `~> 4.0`, azapi, modtm, random | 4 | 1.96 s | 0.50 s | 1.05 s | 0 | 4 | 877 MiB |
| legacy: azurerm `~> 3.116`, azuread `~> 2.47` | 2 | 0.43 s | 0.24 s | 0.44 s | 0 | 2 | 472 MiB |
| newest: azurerm `>= 5.0`, azuread, time | 3 | 0.40 s | 0.24 s | 0.41 s | 0 | 3 | 534 MiB |
| every provider in the catalog | 15 | 0.94 s | 0.65 s | 1.25 s | 0 | 15 | 1102 MiB |
| unsatisfiable: azurerm `~> 6.0` | none | 0.09 s | 0.02 s | 0.03 s | 0 | 0 | 143 MiB |

Every symlink pointed inside the mirror, and `validate` returned valid for all four working roots. Peak memory is the container cgroup peak, page cache included.

The same two providers installed from ZIPs, measured for the catalog analysis, take 2.1 s and copy 599 MiB into the call, with a 2.18 GB peak. Linking removes both.

Version selection out of one mirror holding six azurerm versions is Terraform's own, and it is correct: `~> 4.0` chose 4.81.0, `~> 3.116` chose 3.117.1, `>= 5.0` chose 5.5.0. A constraint the mirror cannot satisfy fails initialization and installs nothing, which is the refusal the offline design depends on.

Cold numbers are soft: dropping the page cache inside the Docker Desktop VM does not clear the host's own caching. The `typical` row at 1.96 s is the only genuine first touch of azurerm 4.81.0 and azapi in a fresh container, and is the most honest cold figure here.

### ACAS

One live sandbox on the default tier, booted from the prepared image of #1270, with the mirror unpacked into the guest first.

| Fact | Value |
|---|---|
| Sandbox | 1 vCPU, 2.17 GiB memory, 20 GB root disk with 296 MB used by the image |
| Reading the 40 MB azapi ZIP out of the image, first touch | 0.055 s, about 760 MB/s |
| `init` from the unpacked mirror | 0.71 s cold, 0.36 s warm, 0.72 s with a read-only lock |
| Bytes copied, symlinks | 0, one link into the mirror |
| Page cache | the guest may drop it, so the cold figures are real |

Linking therefore works on the ACAS filesystem, which was the open question: had symlink creation failed there, Terraform would have copied 599 MiB per call without saying so.

### Sizes

| Item | Size |
|---|---|
| 27 provider versions, unpacked | 2.52 GiB in 52 files |
| The same, gzipped as a layer would be | 549 MiB |
| The same as ZIPs, today's layout | 562 MiB |
| Estimated catalog image | about 2.9 GiB: base 331 MiB, modules 62 MiB, providers 2.52 GiB |
| Estimated ACAS disk image | about 3.4 GB, scaling by the 1.16 ratio measured on the current image (435 MiB image, 504 MB disk image) |

A 3.4 GB disk image leaves over 16 GB of the sandbox's 20 GB disk.

## What would change

1. **Preparation keeps fetching and verifying ZIPs.** The request policy, digests and provenance are unchanged. Unpacking happens in the image build.
2. **The receipt records the unpacked form too**: each file's digest and the package's `h1:` hash, with the build checking that the `h1:` of the unpacked tree matches the `h1:` derived from the verified ZIP. That keeps one chain from the reviewed digest to what the sandbox executes.
3. **`prepared.Dockerfile` unpacks in an earlier stage** so the ZIPs never land in a published layer, verifies every unpacked file against the receipt, and keeps the empty-mirror check on the base.
4. **The build restores the executable bit and proves it by validating.** An init-only probe passes a mirror whose binaries cannot execute, so the image build must run `validate` as well, or check the mode of every provider binary.
5. **The launcher verifies no copy happened.** After `init`, walk `TF_DATA_DIR/providers`: every provider entry must be a symlink into the image mirror, and no regular file may appear there. Anything else means the copy fallback fired, and the report should be INCOMPLETE. This is what turns "no copying" into a guarantee instead of an expectation.
6. **Tests and docs follow the layout.** The Docker suite's mirror assertions move from ZIP digests to unpacked digests, and the wrong-lock case must keep using an `h1:` hash, because a `zh:`-only lock is no longer verifiable against a directory.
7. **CI keeps building the small profile.** Building and pushing a 3 GiB image on every live run is heavy; the catalog image belongs on a schedule or in an operator's hands.

## Consequences and risks

- **`zh:`-only locks stop being checked.** Terraform says so in its own source. A supplied lock with `h1:` hashes is still verified, and the refusal measured in #1270 uses `h1:`. Worth saying plainly in the kind guide, because it is a real loss against the ZIP mirror.
- **Reading is not free.** A typical AVM root reads about 600 MB of provider bytes per call for hashing. At the ACAS read rate that is under a second, and it lands in page cache inside a 2 GiB sandbox.
- **The guest can still write into the mirror during its own call**, as today, because it runs as root and the sandbox is disposed afterwards. A read-only bind mount would need privileges the sandbox does not have.
- **Import and boot of a 3.4 GB disk image are unmeasured.** The current 504 MB image imports in 19 s; nothing here says the larger one scales linearly.

## Not measured

- ACAS import and boot for a 3 GiB image, and how a larger image affects acquire time.
- Cold reads on ACAS with no host-side caching at all.
- OpenTofu, which needs its own mirror and its own image.
- Any Terraform path other than `filesystem_mirror`, such as `-plugin-dir`.
- Provider versions beyond the 27 the catalog resolves. Every published version of these providers is roughly a hundred times larger and is not a candidate.
