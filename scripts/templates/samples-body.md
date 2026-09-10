Automated by the release workflow when maf-sandbox @VERSION@ was released. It moves only the floors in `samples/*/agent.py` to the @MINOR@ core minor; nothing under `packages/` is part of this pull request.

**Wait for the dependent releases before merging this.** The samples resolve their backend and kind packages from PyPI. Until the dependents that should admit @VERSION@ have published, this pull request can ask for the new core beside packages whose published ceilings still exclude it.

**This is deliberately separate from the range pull request.** Putting the sample floors beside the package range made the suite unsatisfiable when the core reached PyPI before its dependents. The range pull request opens the packages' ceilings and releases those packages; this one follows with the documentation samples.

**If this pull request is already green, still check the release order.** A green run means the current index could satisfy the samples at check time, not that every intended dependent Release PR has published.
