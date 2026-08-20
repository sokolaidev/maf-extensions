"""Refuse a commit that names the private origin this repository was scrubbed from.

The scrub rule (AGENTS.md) forbids naming the origin repository, its issue or PR numbers,
host paths, internal URLs or infrastructure identifiers in any artifact. CI can catch a leak
only after the push has made it public, so the only point at which the fix is free is before
the commit. This is that check, wired as a ``pre-commit`` hook over the staged files and a
``commit-msg`` hook over the message.

Two invocation forms, matching the two pre-commit hook stages that drive it:

    python scripts/check_no_origin_identifiers.py --staged <path>...
        The ``pre-commit`` stage. Each path is read from the index (the staged blob), so a
        leak that is fixed on disk but still in the index is the one that gets caught — the
        worktree is not what commits.

    python scripts/check_no_origin_identifiers.py --commit-msg <message-file>
        The ``commit-msg`` stage: scan the message file.

The committed patterns match only what can be matched *anonymously* **without a false
positive on this tree** — the internal DNS suffixes ``.internal`` and ``.intranet``. The
other shapes the scrub rule cares about are deliberately *not* committed patterns, for two
reasons. A generic one collides with public content: a bare "absolute Windows user path"
matches the legitimate ``C:/Users/…`` fixtures in the path-normalisation tests, and a generic
``PREFIX-123`` ticket shape matches the retail codes in the samples. And the rest are named
by definition: the origin repository, a specific internal host, the private registry (this
suite's public ``*.azurecr.io`` host is *not* it), and an origin ticket prefix. Those belong
in the untracked local list ``.no-origin-identifiers`` at the repository root: one literal
per line, ``#`` for a comment, matched case-insensitively as a substring. A committed list of
such names would publish the very secret it suppresses, and the list is gitignored, so it
never leaves the machine.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# A DNS label immediately before an internal suffix. The label requirement keeps this from
# matching a bare word in prose — a real internal host always carries a subdomain label.
# Only ``.internal`` and ``.intranet`` are committed: they are the unambiguous internal TLDs,
# with no public domain that could use them as a suffix. (``.corp`` is dropped on purpose —
# ``x.corp.com`` is a plausible public name the pattern would catch.)
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_INTERNAL_DNS = re.compile(rf"{_HOST_LABEL}\.(?:internal|intranet)\b", re.IGNORECASE)

#: The untracked, machine-local list of names only the owner of the origin knows.
LOCAL_LIST_NAME = ".no-origin-identifiers"

#: The repository root, resolved from this file. A module attribute (rather than a constant
# computed inside ``main``) so the tests can point it at a scratch repository.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The guard's own source and its test carry the rules' fixtures and examples by
# construction — a test that exercises ``.internal`` has to contain ``.internal`` — so they
# are exempt from the scan; everything else is judged. Named as pre-commit passes them,
# repo-relative with forward slashes, so the match never depends on the working directory.
_EXEMPT = frozenset(
    {
        "scripts/check_no_origin_identifiers.py",
        "tests/test_check_no_origin_identifiers.py",
    }
)


def _staged_blob(path: str) -> str:
    """The staged content of ``path`` as text, or empty for a binary blob."""
    result = subprocess.run(
        ["git", "show", f":{path}"], cwd=_REPO_ROOT, capture_output=True, check=False
    )
    if result.returncode != 0:
        return ""
    raw = result.stdout
    if b"\0" in raw:
        return ""
    return raw.decode("utf-8", errors="ignore")


def load_local_names(repo_root: Path) -> tuple[str, ...]:
    """The owner-only names from ``.no-origin-identifiers``, or none if it is absent."""
    path = repo_root / LOCAL_LIST_NAME
    if not path.is_file():
        return ()
    names = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return tuple(names)


def scan(text: str, local_names: tuple[str, ...] = ()) -> list[str]:
    """The reasons ``text`` may not be committed, or an empty list.

    Each finding names the rule that fired and the offending snippet, so the message says
    what to remove rather than only that something is wrong.
    """
    findings: list[str] = []
    for match in _INTERNAL_DNS.finditer(text):
        findings.append(
            f"internal hostname `{match.group(0)}` — name it generically; an internal host "
            f"is an infrastructure identifier the scrub rule forbids"
        )
    for name in local_names:
        if name.casefold() in text.casefold():
            findings.append("matches a name from the local origin list — remove it")
    return findings


def _exempt(path: str) -> bool:
    """Whether ``path`` is the guard's own source or test, exempt from the scan."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized in _EXEMPT


def main(argv: list[str]) -> int:
    """CLI entry. ``--staged <path>…`` scans the staged blobs, ``--commit-msg <file>`` scans a message."""
    paths: list[str] = []
    message_file: str | None = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--staged":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                paths.append(argv[i])
                i += 1
        elif arg == "--commit-msg":
            i += 1
            if i >= len(argv):
                break
            message_file = argv[i]
            i += 1
        else:
            print(f"unexpected argument: {arg}", file=sys.stderr)
            return 2

    repo_root = _REPO_ROOT
    local_names = load_local_names(repo_root)

    if not paths and message_file is None:
        print(
            f"usage: {argv[0]} --staged <path>… | --commit-msg <message-file>",
            file=sys.stderr,
        )
        return 2

    problems: list[str] = []
    for path in paths:
        if _exempt(path):
            continue
        for finding in scan(_staged_blob(path), local_names):
            problems.append(f"staged {path}: {finding}")
    if message_file is not None:
        message = Path(message_file).read_text(encoding="utf-8")
        for finding in scan(message, local_names):
            problems.append(f"commit message: {finding}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
