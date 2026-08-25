"""The four functions a host offers a sandboxed program — three declared, one not.

The bodies are stubs, and a real implementation would change none of the declarations: the
three legs describe a function's relationship to the boundary, not its code.
"""

from __future__ import annotations

from maf_sandbox import Identity, SourceIntegrity, sandbox_tool


@sandbox_tool(source=None, sink=None, identity=None)
def semver_bump(current: str, level: str) -> str:
    """The next version after ``current`` at ``level``. Pure arithmetic on its arguments.

    All three legs are ``None`` and each is an answer: it brings nothing external in, nothing
    conversation-derived flows out through it, and it exercises no authority at all — it
    could not, since it only reads what it was handed.
    """
    major, minor, patch = (int(part) for part in current.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@sandbox_tool(source=SourceIntegrity.UNTRUSTED, sink=None, identity=Identity.APP)
def fetch_changelog(package: str) -> str:
    """Someone else's release notes, over the network, with the host's own credentials.

    ``UNTRUSTED`` because the text is whatever a third party published — the model will read
    it, and a model reading attacker-shaped text is the case the integrity leg exists for.
    ``Identity.APP`` because reaching the network uses the host process's authority, which is
    not the safe option but the honest one: it is every grant the deployed application holds.
    """
    return f"{package} 1.4.0\n- fixed a thing\n- broke another\n"


@sandbox_tool(source=None, sink="public", identity=Identity.USER)
def publish_release_note(text: str) -> str:
    """Post ``text`` publicly, as the person the agent is acting for.

    The one that changes the shape of the whole surface. ``sink="public"`` is a cap in this
    host's own confidentiality vocabulary — the library never orders or folds those, so the
    string stays opaque and is carried verbatim into the aggregate.

    ``Identity.USER`` is declarable and deliberately not servable: registering it raises the
    whole ``execute_code`` surface to approval-gated, and calling it as a host tool is refused
    with the prerequisites named. Declaring it honestly is what lets a router refuse it (see
    ``agent.py``'s fourth act); declaring it as ``APP`` to make the refusal go away would be
    the lie the leg exists to prevent.
    """
    return f"published {len(text)} characters"


def rerun_failed_jobs(workflow: str) -> str:
    """Re-run a CI workflow's failed jobs. **Deliberately not stamped.**

    Here to be refused. A registry built with ``require_declared=True`` turns this away at
    registration — the host's own configuration site, where the fix is one decorator away —
    rather than at the call, where the model would get a sanitized sentence and the host a
    tool it never classified.

    With the gate off it would register and fail safe instead: read as an ``UNTRUSTED``
    source and an ``APP`` identity, with ``has_undeclared`` raised so the degrade is visible
    without diffing the folds.
    """
    return f"re-ran {workflow}"
