Automated by the release workflow when `maf-compaction` @VERSION@ was released. This updates `maf-cachebench`'s compaction dependency to admit the @MINOR@ line, moving the floor and ceiling together when needed.

Check that `maf-compaction` @VERSION@ is available on PyPI before merging this dependency update. Release creation dispatches publication separately, which may still be waiting for approval or have failed.

Merging this `fix:` PR makes release-please open or refresh the `maf-cachebench` Release PR. Release-please owns its version and changelog. Review compatibility before merging the update; it does not establish that cachebench works with a breaking compaction change. Patch releases within an already-adopted compaction line leave the range unchanged.
