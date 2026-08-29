"""Refuse a `## Status` row whose tracker has gone stale.

    python scripts/check_doc_trackers.py

`tests/test_docs_structure.py` holds every row to pinning *something*, which is what stops a
document drifting silently. Nothing holds it to pinning something still **open**, so a row goes
stale on the day its issue closes and no check anywhere fails — which is how six rows across
five documents came to describe work that had already shipped.

Two findings, and they are different mistakes:

- An **annotation** that disagrees with what it labels. The convention writes `(open)`,
  `(closed)` or `(merged)` after a reference, and that is a claim about this repository which
  stops being true without anything touching the file.
- A row still **outstanding** — `open`, `partial`, `parked` — whose every reference has closed.
  That decision is now tracked by nothing, which is the state the convention exists to prevent.

A run that belongs to a pull request judges the state at *merge* rather than the state today:
the request's closing keywords promise what it closes, a promised number counts as `CLOSED`,
and the row a merge is about to invalidate can be flipped in the request that earns the flip.
A run with no request to its name — the one on `main` — judges live state, which is what
catches a promise that never landed.

A reference to another repository is reported as unchecked rather than guessed at: upstream
issues close on somebody else's schedule and this check has no standing to read them.

Asking needs a GitHub token — `GITHUB_TOKEN`, `GH_TOKEN`, or whatever `gh auth token` answers.
Without one, or without a network, it says so and exits 0. A documentation check that failed
when GitHub was unreachable would put `poe gate` behind somebody else's uptime, and the rows it
guards are prose rather than anything that ships.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_API = "https://api.github.com/graphql"
_TIMEOUT_SECONDS = 20.0

#: How many references one query asks about. GraphQL costs a node per alias, and the whole tree
#: is under a hundred today — the chunking is here so a growing tree never silently truncates.
_BATCH = 50

#: A row is outstanding when its state *opens* with one of these. Matching the whole cell would
#: miss `open — the mechanism refuses a link`, and matching anywhere would catch
#: `shipped ... the umbrella's remaining parts are open`, which pins its successors and is fine.
_OUTSTANDING = re.compile(r"^(open|partial|parked)\b", re.IGNORECASE)

#: A reference into some GitHub repository, as the tracking cells write them.
_REFERENCE = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<number>\d+)"
)

#: The annotation, if the cell wrote one straight after the link. `(both open)` and `(all
#: merged)` label a group; attaching one to the reference it follows checks fewer references
#: than it labels, which is the safe direction to be wrong in.
_ANNOTATION = re.compile(r"\A\)?\s*\((?:both |all |either )?(open|closed|merged)\)", re.IGNORECASE)

#: GitHub's closing keywords, followed by the reference they close. `#N` means this repository;
#: `owner/repo#N` and the `issues/` URL name one in full, and a reference into another repository
#: promises nothing here. Only `/issues/` closes — a keyword aimed at a pull request closes
#: nothing when the request merges.
_CLOSING = re.compile(
    r"\b(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\s+"
    r"(?:"
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<url_number>\d+)"
    r"|(?:(?P<ref>[\w.-]+/[\w.-]+))?#(?P<number>\d+)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reference:
    """One tracker named by a row, and what the row claimed about it."""

    owner: str
    repo: str
    number: int
    #: `OPEN`, `CLOSED`, `MERGED` — or ``None`` where the row annotated nothing.
    claimed: str | None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class Row:
    """One Status-table row, located well enough to be fixed from the report alone."""

    path: str
    line: int
    decision: str
    state: str
    tracking: str


def cells(row: str) -> list[str]:
    """One table row's cells, stripped, without the leading and trailing pipe."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def is_separator(row: str) -> bool:
    """Whether this is the `|---|` line rather than a decision."""
    return all(re.fullmatch(r":?-+:?", cell) for cell in cells(row)) if cells(row) else False


def status_rows(text: str, path: str) -> list[Row]:
    """Every decision row under this document's `## Status` heading.

    The header row and the separator are dropped; a row with fewer than three cells is not a
    decision and is left to `test_docs_structure.py`, which owns the table's shape.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("## Status"))
    except StopIteration:
        return []
    rows = []
    for offset, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("#"):
            break
        if not line.startswith("|") or is_separator(line):
            continue
        parts = cells(line)
        if len(parts) < 3 or parts[0] == "Decision":
            continue
        rows.append(
            Row(path=path, line=offset, decision=parts[0], state=parts[1], tracking=parts[2])
        )
    return rows


def references(cell: str) -> list[Reference]:
    """The trackers a cell names, each with the state the cell claimed for it."""
    found = []
    for match in _REFERENCE.finditer(cell):
        annotation = _ANNOTATION.match(cell[match.end() :])
        found.append(
            Reference(
                owner=match["owner"],
                repo=match["repo"],
                number=int(match["number"]),
                claimed=annotation[1].upper() if annotation else None,
            )
        )
    return found


def promised_numbers(body: str, slug: str) -> frozenset[int]:
    """The numbers this request's closing keywords promise to close — this repository's only.

    A keyword without a reference promises nothing, and a reference into another repository
    stays out, the same way `ask` refuses to read another repository's numbers.
    """
    promised: set[int] = set()
    for match in _CLOSING.finditer(body):
        if match["url_number"] is not None:
            if f"{match['owner']}/{match['repo']}" == slug:
                promised.add(int(match["url_number"]))
        elif match["ref"] is None or match["ref"] == slug:
            promised.add(int(match["number"]))
    return frozenset(promised)


def is_outstanding(state: str) -> bool:
    """Whether the row says the work is still to do."""
    return _OUTSTANDING.match(state) is not None


def state_at_merge(state: str | None, number: int, promised: frozenset[int]) -> str | None:
    """The state a merge leaves behind: a number this request promises to close is `CLOSED`.

    Only `OPEN` changes — a tracker already closed or merged keeps what it is, and a number this
    repository does not have stays missing, so no promise can invent one.
    """
    return "CLOSED" if number in promised and state == "OPEN" else state


def findings(
    rows: list[Row],
    states: dict[int, str | None],
    slug: str,
    promised: frozenset[int] = frozenset(),
) -> list[str]:
    """Every stale annotation and every outstanding row nothing tracks, newest problem first.

    ``states`` maps a reference number to `OPEN`/`CLOSED`/`MERGED`, or to ``None`` for a number
    this repository does not have. A number missing from the mapping was never asked about —
    another repository's — and is not judged here.

    ``promised`` is the numbers the current request's closing keywords close, counted as `CLOSED`
    so a row is judged against the merge it belongs to rather than the moment the run happens.
    Leave it empty and the comparison is live state, exactly as the run without a request sees it.
    """
    problems = []
    for row in rows:
        mine = [ref for ref in references(row.tracking) if ref.slug == slug]
        for ref in mine:
            live = states.get(ref.number, "")
            if live is None:
                problems.append(f"{row.path}:{row.line}: #{ref.number} does not exist in {slug}")
                continue
            at_merge = state_at_merge(live, ref.number, promised)
            if not ref.claimed or not at_merge or ref.claimed == at_merge:
                continue
            if at_merge != live:
                problems.append(
                    f"{row.path}:{row.line}: names #{ref.number} as ({ref.claimed.lower()}), "
                    f"and this PR closes it — {row.decision[:60]}"
                )
            else:
                problems.append(
                    f"{row.path}:{row.line}: names #{ref.number} as ({ref.claimed.lower()}), "
                    f"and it is {live.lower()} — {row.decision[:60]}"
                )
        if not is_outstanding(row.state) or not mine:
            continue
        tracked = [states.get(ref.number) for ref in mine]
        at_merge = [
            state_at_merge(state, ref.number, promised) for state, ref in zip(tracked, mine)
        ]
        if not all(state in ("CLOSED", "MERGED") for state in at_merge):
            continue
        closed = ", ".join(f"#{ref.number}" for ref in mine)
        if at_merge != tracked:
            problems.append(
                f"{row.path}:{row.line}: says {row.state.split(' ')[0]!r} and nothing will "
                f"track it once this PR merges ({closed}) — {row.decision[:60]}"
            )
        else:
            problems.append(
                f"{row.path}:{row.line}: says {row.state.split(' ')[0]!r} and every tracker it "
                f"names has closed ({closed}) — {row.decision[:60]}"
            )
    return problems


def documents(root: Path) -> list[Path]:
    """Tracked markdown under `docs/`, which is where Status tables live."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "docs"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / name for name in listed.split("\0") if name.endswith(".md")]


def slug_from_url(url: str) -> str:
    """`owner/name` out of a remote URL, in either the https or the ssh spelling."""
    match = re.search(r"[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$", url.strip())
    if match is None:
        raise ValueError(f"cannot read an owner/name out of {url!r}")
    return f"{match['owner']}/{match['repo']}"


def repo_slug(root: Path) -> str:
    """`owner/name` for `origin`, so a fork checks its own references rather than upstream's."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return slug_from_url(url)


def token() -> str | None:
    """A GitHub token from the environment, or whatever `gh` is already signed in as."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        answered = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return answered.stdout.strip() or None


def query(slug: str, numbers: list[int]) -> str:
    """One GraphQL document asking for every number's state at once.

    `issueOrPullRequest` rather than `issue`, because a tracking cell names merged pull requests
    as often as it names issues and asking the wrong one back an error instead of a state.
    """
    owner, name = slug.split("/")
    fields = " ".join(
        f"n{number}: issueOrPullRequest(number: {number}) "
        "{ ... on Issue { state } ... on PullRequest { state } }"
        for number in numbers
    )
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'


def _post(document: str, auth: str) -> dict:
    """One GraphQL document answered, or the refusal the transport raised."""
    request = urllib.request.Request(
        _API,
        data=json.dumps({"query": document}).encode(),
        headers={"Authorization": f"bearer {auth}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def ask(slug: str, numbers: list[int], auth: str) -> dict[int, str | None]:
    """The live state of each number, or ``None`` for one this repository does not have."""
    states: dict[int, str | None] = {}
    for start in range(0, len(numbers), _BATCH):
        batch = numbers[start : start + _BATCH]
        body = _post(query(slug, batch), auth)
        repository = (body.get("data") or {}).get("repository") or {}
        for number in batch:
            answered = repository.get(f"n{number}")
            states[number] = answered.get("state") if answered else None
    return states


def pull_request_number() -> int | None:
    """The number the run names for its pull request, when it names one; `push` has no request."""
    raw = os.environ.get("PR_NUMBER", "")
    return int(raw) if raw.isdigit() else None


def pull_request_body(slug: str, number: int, auth: str) -> str | None:
    """One request's body, read live so the run sees edits its trigger did not.

    ``None`` for a request with no body or one that is gone — a missing request is a fact about
    the repository rather than a refusal, and the check falls back to live state.
    """
    owner, name = slug.split("/")
    document = (
        f'query {{ repository(owner: "{owner}", name: "{name}") '
        f"{{ pullRequest(number: {number}) {{ body }} }} }}"
    )
    body = _post(document, auth)
    repository = (body.get("data") or {}).get("repository") or {}
    answered = repository.get("pullRequest") or {}
    return answered.get("body")


def branch_pull_request_body(root: Path) -> str | None:
    """The body of the request for the current branch, or ``None`` when there is none.

    `gh pr view` reads the request from the branch, which is the only place a local run can find
    one — and why this stays optional: before the request exists there is nothing to promise,
    and the check runs exactly as it did before.
    """
    try:
        answered = subprocess.run(
            ["gh", "pr", "view", "--json", "body"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if answered.returncode != 0:
        return None
    try:
        return (json.loads(answered.stdout) or {}).get("body")
    except json.JSONDecodeError:
        return None


def repo_root() -> Path:
    """The repository root, from this file's own location."""
    return Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    """CLI entry: report every stale tracker, and exit 1 if there is one."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    root = repo_root()
    slug = repo_slug(root)
    rows = [
        row
        for path in documents(root)
        for row in status_rows(path.read_text("utf-8"), path.relative_to(root).as_posix())
    ]
    wanted = sorted(
        {ref.number for row in rows for ref in references(row.tracking) if ref.slug == slug}
    )
    if not wanted:
        print("no tracker references to check")
        return 0

    auth = token()
    if auth is None:
        print(f"skipped: no GitHub token, so the {len(wanted)} tracker(s) named were not read")
        return 0
    try:
        states = ask(slug, wanted, auth)
        number = pull_request_number()
        body = (
            pull_request_body(slug, number, auth)
            if number is not None
            else branch_pull_request_body(root)
        )
        promised: frozenset[int] = frozenset()
        if body:
            promised = promised_numbers(body, slug)
    except urllib.error.HTTPError as refused:
        # Asked and turned away — a rejected token or a spent rate limit. Not the same as being
        # offline and not skippable: in CI the token is always there, so a silent pass here would
        # be the check reporting green on the one failure it cannot see past.
        print(f"GitHub refused the query: {refused.code} {refused.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as unreachable:
        print(f"skipped: could not reach GitHub ({unreachable})")
        return 0

    problems = findings(rows, states, slug, promised)
    unchecked = {ref.slug for row in rows for ref in references(row.tracking) if ref.slug != slug}
    if not problems:
        print(
            f"every tracker named in a Status row still says what the row says ({len(wanted)} checked)"
        )
        scored = ", ".join(f"#{n}" for n in sorted(promised & set(wanted)))
        if scored:
            print(f"promised by this request, scored as closed: {scored}")
        if unchecked:
            print(f"not checked, another repository's: {', '.join(sorted(unchecked))}")
        return 0
    for problem in sorted(problems):
        print(problem, file=sys.stderr)
    print(
        f"\n{len(problems)} stale tracker(s). A row that pins a closed issue reads as tracked "
        "and is not, which is the drift the Status convention exists to prevent.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
