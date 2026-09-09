Automated by the release workflow when maf-sandbox @VERSION@ was released. **Check that it actually published before merging this.** The upload is dispatched separately and may still be waiting on the pypi environment, or have failed; a floor pointing at a version that never reached PyPI is unresolvable for every consumer.

It carries both ends of the range, because they are one string and two pull requests editing it each reverted the other's half (#195). The two ends are not the same kind of claim, so read them differently:

**The ceiling is uniform and is a claim about nobody's code.** It opens onto @VERSION@'s own line and stops there. Widening only enlarges an accepted set, so it cannot make a resolution fail, and what it buys is that these packages can be installed beside the core that just published (#175, #177). Nothing here needs your judgement.

**It deliberately does not reach the minor after @VERSION@.** Admitting a version and being tested against it are the same condition — `check_core_against_dependents.py` runs every published dependent whose ceiling admits the candidate — so a ceiling reaching ahead makes every breaking core wait on republishing the dependents it breaks. `docs/release-compatibility.md` has the argument.

**The floor moves with it, in every dependent a minor behind.** Together the two leave each dependent on one core minor: the suite is released as a set rather than carrying several core lines, which is the maintenance choice behind this. It is not evidence that a package's code needs @VERSION@ — decline a hunk for a package you want left on an older core, and be aware that its consumers then cannot install it beside a sibling that did move.

**Nothing under `samples/` is in this pull request**, deliberately: merged with the packages' hunk it takes the whole suite unsatisfiable whenever the core reached the index first (0.33.0, 0.34.0). The release workflow opens a separate `chore:` pull request for the samples; merge that one only after every dependent that should admit @VERSION@ has published.

**To decline a floor without losing its ceiling**, edit that line back to the floor it had and leave the new upper bound alone — the two now live in one hunk, so dropping the hunk wholesale would give up the widening too. Retitle it feat:/breaking if adopting the version is more than a patch for a dependent; the title says fix: so the bot does not choose the bump.

**Then merge it, and let the dependent releases it cuts publish.** The widening is only worth anything once it is on PyPI: that is what the next core release checks before it uploads. Its checks are held at "Approve and run", the same as a Release PR's; clearing them is what starts them.
