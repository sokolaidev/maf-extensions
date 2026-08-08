<!--
Before anything else: your PR title is the changelog entry.

It becomes the commit subject when this is squashed, which is what release-please
reads — so it decides the next version AND is what a reader sees in the release
notes. Write it for someone deciding whether to upgrade, not as a summary of the
diff: "accept a list of arguments to exec, and quote them" rather than "update exec".

  feat:  a minor release      fix: / perf: / revert: / docs:  a patch
  refactor: / test: / build: / ci: / chore:  release nothing

Breaking something? Add a `BREAKING CHANGE: …` footer in the squash-commit box when
you merge — it becomes its own section, above everything else, in the release notes.

CONTRIBUTING.md has the rest: the type list, and what the boundary tests protect.
-->

## What changed, and why

<!-- Anything a reviewer should look at first, or any boundary you had to cross? -->
