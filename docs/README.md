# Documentation

One directory per extension family, plus the repo-wide notes that belong to no family.

[`sandbox/`](sandbox/README.md) — the sandboxed-execution suite: a backend-neutral protocol that lets an agent's tool run its work in a sandbox, any sandbox, while the host decides which sandboxes qualify. **Start at its front door.** It assumes no prior knowledge of Microsoft Agent Framework, and its *Where to read next* is the deep map of everything beside it. 

[`compaction/`](compaction/README.md) — prompt-cache-aware compaction for Microsoft Agent Framework: strategies that keep the cached prefix intact while they shrink a conversation, and the benchmark they were measured with.

[`maintainers.md`](maintainers.md) — repo-wide release plumbing: trusted publishing, the release train, and the order the packages go out in.

[`security/`](security/README.md) — release-specific security evidence, artifact verification, image scan coverage, remaining gaps and development guidance.

[`release-compatibility.md`](release-compatibility.md) — what proves a core and its dependents still work together across a release, which pairings each gate covers, and the two windows that remain open.

[`AUTHORING.md`](AUTHORING.md) — how these documents are written, for maintainers and contributors adding or editing one.

A package's own `README.md` stays with the package under [`packages/`](../packages/), because it is that package's PyPI front page.
