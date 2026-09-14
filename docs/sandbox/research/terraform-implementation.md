# Terraform/OpenTofu implementation verification

This records local verification for [#1246](https://github.com/sokolaidev/maf-extensions/issues/1246), on 2026-09-14, following the [CLI investigation](terraform-kind.md). Source baseline: `93c94d9c2a8bac82c9c72dc5bff812d2a3a1d372`. The host was Windows with CPython 3.12.14; the real adapter used Docker Desktop's Linux engine. Guest images pin Python 3.13.15, Terraform 1.16.2 or OpenTofu 1.12.6, and platform Linux amd64. The optional provider is the selected registry's pinned random 3.7.2 archive, with checksums in [the image guide](../../../images/terraform-sandbox/README.md).

| Evidence | Result |
|---|---|
| Deterministic workload tests | 60 passed: manifest/path/refusal, byte/count limits, provenance, hidden names, report parsing, engine selection, and cancellation ordering through core test sessions |
| Real Docker adapter matrix | 24 cases passed: 12 scenarios for each engine, each invoked twice, with daemon-observed absence after every call and unchanged store content |
| Linux launcher tests | Two image runs passed, each executing five subprocess/environment/lock tests |
| Checkout examples | Both engines passed local-module validation in their built-in images and random-provider validation in their mirrored-provider images; formatting also passed |
| Packaging | Wheel built from sdist, Twine checks passed, and isolated wheel installation/import smoke passed using the repository's local-core override |

The 12 adapter scenarios are local modules with an unset required variable, JSON configuration, undefined-variable diagnostics, invalid syntax, an unavailable local module, successful random-provider schema loading, a provider type error, formatting-only changes, wrong-engine images, cancellation, OpenTofu file precedence, and deadline expiry. Terraform refuses the `.tofu` manifest at admission; that scenario intentionally creates no container in Terraform mode. Every admitted repeated call has a different container identity. Disposal is observed through `docker ps --all`, not just the router's ledger. The launcher tests cover ambient environment removal, combined stdout/stderr overflow, a descendant retaining pipes after its parent exits, a shared deadline across commands, and supplied lock/source/state nonmutation.

The full unfiltered repository gate reported existing Windows timing failures. All five were reproduced using source archived from unchanged `origin/main`, with that archive's package sources on the Python path:

- `test_process_cleanup_steps_share_one_deadline_and_reserve_a_signal_attempt`, all three phases (`signal`, `descendants`, `after_cleanup`).
- `test_retained_candidates_are_batched_within_one_deadline_and_instance`.
- `test_completed_capacity_waiters_release_context_before_capacity_changes[timeout]` in the ACAS client pool tests.

The first four fail timing/deadline assertions, including floating-point rounding above a 0.1-second boundary. The ACAS case times out acquiring a client lease. The baseline reproduction was 5 failed and 62 passed. These files are unchanged by this implementation; the package registration failures from the initial gate were fixed separately.

The gate rerun completed with 8,951 passed, 334 skipped, and six deselected, followed by passing lint, formatting, all package strict type checks, root type checks, and documentation paths. The function-level selector also deselected the passing `before_cleanup` parameter; it was run separately and passed. The final 60-test workload run additionally covers the UTF-8 refusal added during review. Only the five reproduced baseline failures remain unresolved, and the default gate definition is unchanged. Markdown Python-block checks also passed.

Remote CI has not run for this local branch. The wheel smoke uses the repository's prepared core API from its locally built wheel; it is not evidence that the declared future core floor or this new package has been published. No sample claims an unpublished installable release. ACAS, WSLC, other guest architectures, and other providers have not been qualified by these measurements.
