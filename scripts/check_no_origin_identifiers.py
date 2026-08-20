"""Refuse a commit that names the private origin this repository was scrubbed from.

Wired as a pre-commit hook over the staged blobs and a commit-msg hook over the message.
The committed rules are the anonymous structural patterns; the names only the origin owner
knows are read from the untracked local list ``.no-origin-identifiers``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# A DNS label before the internal suffix — a real internal host always carries a subdomain
# label — and the match must end the internal name: no DNS character may follow the suffix,
# nor a dot plus a label, which would make the host a public continuation instead (a name
# that merely *contains* the suffix, as this file's own test fixtures do, is safe). A sentence
# or trailing root dot still ends the name. ``.corp`` stays out on purpose; a public name may
# end in it.
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_INTERNAL_DNS = re.compile(
    rf"{_HOST_LABEL}\.(?:internal|intranet)(?:-[A-Za-z0-9])?" r"(?![A-Za-z0-9-])(?!\.[A-Za-z0-9])",
    re.IGNORECASE,
)

#: The untracked, machine-local list of names only the owner of the origin knows.
LOCAL_LIST_NAME = ".no-origin-identifiers"

#: The repository root, resolved from this file. A module attribute (rather than a constant
# computed inside ``main``) so the tests can point it at a scratch repository.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# Every encoding an identifier can be committed in: the rules are ASCII, and none of these
# views produces an ASCII run when the blob is encoded in a *different* one, so scanning all
# of them cannot false-fire on a blob in the wrong encoding.
_DECODINGS = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def _staged_blob(path: str) -> str:
    """The staged content of ``path`` as text, decoded lossily in every known encoding."""
    result = subprocess.run(
        ["git", "show", f":{path}"], cwd=_REPO_ROOT, capture_output=True, check=False
    )
    if result.returncode != 0:
        return ""
    raw = result.stdout
    return "\n".join(raw.decode(encoding, errors="ignore") for encoding in _DECODINGS)


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
        # The path is part of the tree too: a leak in a file or directory name must be caught
        # even when the content is clean.
        for finding in scan(path, local_names):
            problems.append(f"staged path {path}: {finding}")
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
