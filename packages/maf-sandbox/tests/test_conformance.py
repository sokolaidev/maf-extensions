"""The conformance suite, held to the two things a conformance suite has to be.

It must **pass** against an implementation that discharges the duty — otherwise it is a
tripwire nobody can clear — and it must **fail**, naming the right probe, against one that does
not. The second half is the one that matters: a suite written against the same misreading it is
meant to catch passes everything (#142).

So there are two specimens here. `InProcessSandbox` is the real fake, which refuses; `_Leaky`
is written for this file and genuinely resolves through a link, the way a real engine and a
real data plane do, with each of the two duties on a switch.

For the FILES_IN, EXEC and FILES_DELETE suites there is a third specimen, `_SimulatedGuest` —
its class docstring carries what it is and what it is not.

The reclaim suite adds a fourth, `_RuntimeOnlyGuest`: the fake with `exec` and `write_file`
refused outright, which is the shell-less backend the subject's seams exist for (#639).
"""

from __future__ import annotations

import asyncio
import dataclasses
import posixpath
import re

import pytest

from maf_sandbox import (
    Capability,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    Sandbox,
    SandboxEntry,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import (
    EXEC_PROBES,
    FILES_DELETE_PROBES,
    FILES_IN_PROBES,
    FILES_OUT_PROBES,
    REACH_PROBES,
    RECLAIM_PROBES,
    ConformanceFailure,
    ConformancePaths,
    PosixGuestSubject,
    assert_call_scope_conformance,
    assert_egress_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reach_conformance,
    assert_reclaim_conformance,
    measure_files_delete_probes,
    run_call_scope_probes,
    run_exec_probes,
    run_files_delete_probes,
    run_files_in_probes,
    run_files_out_probes,
    run_reach_probes,
    run_reclaim_probes,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)

_WORK = "/maf-sandbox/work"
_BOTH = frozenset({Capability.FILES_OUT, Capability.FILES_LIST})
_EVERYTHING = frozenset(
    {
        Capability.EXEC,
        Capability.FILES_IN,
        Capability.FILES_OUT,
        Capability.FILES_LIST,
        Capability.FILES_DELETE,
    }
)


class _FakeSubject:
    """Plants straight into `InProcessSandbox`'s stores — its `exec` is scripted, not real."""

    def __init__(self, sandbox: InProcessSandbox, capabilities: frozenset[Capability]) -> None:
        self.sandbox = sandbox
        self.working_directory = _WORK
        self.capabilities = capabilities

    async def plant_file(self, path: str, content: bytes) -> None:
        await self.sandbox.write_file(
            path, content, working_directory=posixpath.dirname(path) or "/"
        )

    async def plant_symlink(self, path: str, target: str) -> None:
        del target  # this fake refuses links rather than following them, so it stores no target
        self.sandbox.symlinks.add(path)

    async def exists(self, path: str) -> bool:
        """Through `exec`, as `PosixGuestSubject` does — and this fake's `exec` is scripted."""
        result = await self.sandbox.exec(
            ["test", "-e", path], working_directory=self.working_directory, timeout=60
        )
        return result.exit_code == 0

    async def plant_directory_the_guest_owns(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to make {path!r}")

    async def the_guest_could_have_made(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to ask about {path!r}")

    async def plant_directory_the_guest_cannot_write_into(self, path: str) -> bool:
        """The same: a mode means nothing to a store that keeps bytes and names."""
        raise NotImplementedError(f"no guest here to close {path!r} against")


class _Leaky:
    """A sandbox that really does resolve through a link, with each duty on a switch.

    The shipped fake refuses, which is correct behaviour and the wrong specimen for testing a
    suite: probes have to fail against something that leaks. This one is the escape modelled —
    a link component is replaced by its target before anything is looked up, exactly as an
    engine resolving daemon-side or a data plane resolving service-side does.

    ``checks`` turns the filesystem path check off. ``names_links`` makes it answer
    :data:`~maf_sandbox.EntryKind.OTHER` for a link instead of
    :data:`~maf_sandbox.EntryKind.SYMLINK` — the backend that cannot tell one from a fifo, which
    still refuses every path attacked here and cannot say why.
    """

    def __init__(self, *, checks: bool = True, names_links: bool = True) -> None:
        self.contents: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        self._checks = checks
        self._names_links = names_links

    # -- the guest's own filesystem ------------------------------------------------------

    def _link_kind(self) -> EntryKind:
        return EntryKind.SYMLINK if self._names_links else EntryKind.OTHER

    def _has_children(self, guest: str) -> bool:
        prefix = guest.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in (*self.contents, *self.links))

    def _classify(self, guest: str) -> tuple[EntryKind, int | None] | None:
        """`lstat`, with no following at all — what a link *is*."""
        if guest in self.links:
            return self._link_kind(), None
        if guest in self.contents:
            return EntryKind.FILE, len(self.contents[guest])
        if self._has_children(guest):
            return EntryKind.DIRECTORY, None
        return None

    def _follow_parents(self, guest: str) -> str:
        """Resolve every component *above* the last one. This is the leak, on purpose."""
        parts = [part for part in guest.split("/") if part]
        resolved = ""
        for part in parts[:-1]:
            resolved = f"{resolved}/{part}"
            resolved = self.links.get(resolved, resolved)
        return f"{resolved}/{parts[-1]}" if parts else "/"

    # -- the protocol --------------------------------------------------------------------

    def _confined(self, path: str, working_directory: str) -> str:
        base = posixpath.normpath(working_directory)
        guest = posixpath.normpath(posixpath.join(base, path))
        if guest != base and not guest.startswith(base + "/"):
            raise ValueError(f"path {path!r} resolves outside working directory {base!r}")
        return guest

    def _check_ancestors(
        self, guest: str, working_directory: str, *, include_self: bool = False
    ) -> None:
        """The filesystem path check, deciding from the *reported* kind rather than private knowledge.

        Which is the point of `names_links`: a backend that can only report `OTHER` has no way
        to name the escape, so this check cannot either, and the probes see the difference.
        """
        del working_directory
        if not self._checks:
            return
        deepest = guest if include_self else posixpath.dirname(guest)
        so_far = ""
        for part in (part for part in deepest.split("/") if part):
            so_far = f"{so_far}/{part}"
            found = self._classify(so_far)
            if found is None:
                return
            kind, _ = found
            if kind is EntryKind.SYMLINK:
                raise ValueError(f"{so_far!r} is a link rather than a real directory")
            if kind is not EntryKind.DIRECTORY:
                raise NotADirectoryError(f"{so_far!r} is not a directory")

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        del working_directory
        self.contents[path] = content.encode("utf-8") if isinstance(content, str) else content

    async def exec(self, command, *, working_directory: str, timeout: float) -> ExecResult:
        del working_directory, timeout
        # The only command the subject issues: `ln -sfn <target> <path>`.
        match list(command):
            case ["ln", "-sfn", target, path]:
                self.links[path] = target
                return ExecResult(stdout="")
        return ExecResult(stdout="", stderr="unsupported", exit_code=1)

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Refused: this specimen models a POSIX guest reached through `exec`, and the suites
        that use it plant links with a shell. Present so it is still a `Sandbox`."""
        del code, timeout
        raise NotImplementedError("this specimen declares no RUN_CODE")

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        guest = self._confined(path, working_directory)
        self._check_ancestors(guest, working_directory)
        found = self._classify(self._follow_parents(guest))
        if found is None:
            return None
        kind, size_bytes = found
        return SandboxEntry(path=posixpath.normpath(path), kind=kind, size_bytes=size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        del max_bytes
        guest = self._confined(path, working_directory)
        self._check_ancestors(guest, working_directory)
        resolved = self._follow_parents(guest)
        if resolved in self.links:
            raise OSError(f"{path!r} is not a regular file and is refused")
        if resolved not in self.contents:
            raise FileNotFoundError(f"no such file: {path!r}")
        return self.contents[resolved]

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Removes whatever the link *pointed at*, which is the escape a delete probe hunts.

        The leak that matters for a removal is not reading a byte from outside — it is
        unlinking something outside, where nothing has to come back for the damage to be done.
        """
        guest = self._confined(path, working_directory)
        self._check_ancestors(guest, working_directory, include_self=False)
        resolved = self._follow_parents(guest)
        resolved = self.links.get(resolved, resolved)
        prefix = resolved.rstrip("/") + "/"
        under = [p for p in (*self.contents, *self.links) if p.startswith(prefix)]
        if under and not recursive:
            raise OSError(f"refusing to remove a non-empty directory without recursive: {path}")
        for stored in (*under, resolved):
            self.contents.pop(stored, None)
            self.links.pop(stored, None)

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """A plain recursive removal. This specimen's leak is the pull surface, not this one."""
        del working_directory, timeout
        prefix = directory.rstrip("/") + "/"
        for stored in [
            p for p in (*self.contents, *self.links) if p == directory or p.startswith(prefix)
        ]:
            self.contents.pop(stored, None)
            self.links.pop(stored, None)

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        guest = self._confined(path, working_directory)
        self._check_ancestors(guest, working_directory, include_self=True)
        base = posixpath.normpath(working_directory)
        resolved = self.links.get(self._follow_parents(guest), self._follow_parents(guest))
        prefix = resolved.rstrip("/") + "/"
        names = {
            p[len(prefix) :].split("/")[0]
            for p in (*self.contents, *self.links)
            if p.startswith(prefix)
        }
        entries: list[SandboxEntry] = []
        for name in sorted(names):
            found = self._classify(prefix + name)
            if found is None:
                continue
            kind, size_bytes = found
            rel = posixpath.relpath(posixpath.join(guest, name), base)
            entries.append(SandboxEntry(path=rel, kind=kind, size_bytes=size_bytes))
        return tuple(entries)


class _LeakySubject(PosixGuestSubject):
    """`_Leaky` plants through `exec`, exactly as a real Linux guest does."""


def _results(subject) -> dict[str, str | None]:
    """Every probe's outcome as `{name: failure or None}`, skips excluded."""
    results = asyncio.run(run_files_out_probes(subject))
    return {r.probe.name: r.failure for r in results if r.skipped is None}


def _leaky_subject(**kwargs) -> _LeakySubject:
    sandbox = _Leaky(**kwargs)
    return _LeakySubject(
        sandbox=sandbox, working_directory=_WORK, capabilities=_BOTH, exec_timeout=5
    )


class TestTheSuitePassesAnImplementationThatDischargesTheDuty:
    def test_the_shipped_fake_answers_every_probe(self):
        """`InProcessSandbox` runs the shared check, so it answers the same probes a backend does."""
        subject = _FakeSubject(InProcessSandbox(), _BOTH)
        assert _results(subject) == dict.fromkeys([p.name for p in FILES_OUT_PROBES], None)

    def test_a_second_independent_implementation_answers_them_too(self):
        """One passing specimen could be a suite shaped around it. Two, written apart, is not."""
        assert set(_results(_leaky_subject()).values()) == {None}

    def test_assert_returns_the_results_so_a_caller_can_check_what_was_skipped(self):
        results = asyncio.run(assert_files_out_conformance(_leaky_subject()))
        assert [r.probe.name for r in results] == [p.name for p in FILES_OUT_PROBES]
        assert all(r.passed for r in results)


class TestTheSuiteFailsAnImplementationThatDoesNot:
    """The half that decides whether any of this is worth shipping."""

    def test_a_backend_that_skips_the_check_fails_exactly_the_check_probes(self):
        failures = {name for name, why in _results(_leaky_subject(checks=False)).items() if why}
        assert failures == {
            "stat-through-a-linked-parent",
            "read-through-a-linked-parent",
            "a-linked-working-directory",
            "a-linked-ancestor-of-the-working-directory",
            "a-plain-parent-is-not-an-escape",
            "listing-a-linked-directory",
            "listing-through-a-linked-parent",
            "listing-under-a-linked-ancestor",
        }

    def test_the_read_probe_says_the_bytes_came_back(self):
        """Not merely 'no exception': the specimen returns `/maf-sandbox/work-outside/secret.txt`."""
        failure = _results(_leaky_subject(checks=False))["read-through-a-linked-parent"]
        assert failure is not None and "returned instead of raising" in failure

    def test_a_backend_that_cannot_name_a_link_is_safe_but_fails_the_naming_probes(self):
        """It refuses every path attacked here — as a non-directory — and cannot say why.

        Which is the whole argument for `EntryKind.SYMLINK`: `OTHER` refuses correctly and
        reports an escape as an `ENOTDIR`, so nothing above the backend can tell an attack from
        a guest tripping over its own fifo.
        """
        failures = _results(_leaky_subject(names_links=False))
        assert failures["a-link-is-named-a-link"] is not None
        assert "not 'symlink'" in failures["a-link-is-named-a-link"]
        for probe in ("stat-through-a-linked-parent", "read-through-a-linked-parent"):
            assert failures[probe] is not None
            assert "raised NotADirectoryError" in failures[probe]
        # Still refused, though: nothing here leaked, and `a-link-is-never-read` is unaffected.
        assert failures["a-link-is-never-read"] is None
        assert failures["a-legitimate-read-still-works"] is None

    def test_a_positive_control_that_never_landed_fails_first(self):
        """A subject whose planting silently does nothing must not pass by refusing everything."""

        class _PlantsNothing(_FakeSubject):
            async def plant_file(self, path: str, content: bytes) -> None:
                del path, content

            async def plant_symlink(self, path: str, target: str) -> None:
                del path, target

        failure = _results(_PlantsNothing(InProcessSandbox(), _BOTH))
        assert failure["a-legitimate-read-still-works"] is not None

    def test_the_probe_below_a_linked_ancestor_is_the_one_that_reaches_the_acas_case(self):
        """A check that starts *at* the work dir passes the work-dir probe and fails this one.

        The two are separate probes because they are separate mistakes, and the specimen here
        makes only the second: it classifies the work dir it was handed and nothing above it.
        """

        class _ChecksFromTheWorkDir(_Leaky):
            def _check_ancestors(self, guest, working_directory, *, include_self=False):
                del guest, include_self
                found = self._classify(posixpath.normpath(working_directory))
                if found is not None and found[0] is EntryKind.SYMLINK:
                    raise ValueError("work dir is a link rather than a real directory")

        subject = _LeakySubject(
            sandbox=_ChecksFromTheWorkDir(),
            working_directory=_WORK,
            capabilities=_BOTH,
            exec_timeout=5,
        )
        failures = _results(subject)
        assert failures["a-linked-working-directory"] is None
        assert failures["a-linked-ancestor-of-the-working-directory"] is not None


class TestWhatTheRunnerReports:
    def test_a_probe_that_raises_something_other_than_an_assertion_still_reports(self):
        """One backend error must fail its own probe, not take the run down with it.

        A positive control calls the sandbox directly, so a backend answering with its own
        `RuntimeError` would otherwise escape the runner and report none of the refusals that
        did work.
        """

        class _StatExplodes(_Leaky):
            async def stat_file(self, path: str, *, working_directory: str):
                raise RuntimeError("the provider fell over")

        subject = _LeakySubject(
            sandbox=_StatExplodes(), working_directory=_WORK, capabilities=_BOTH, exec_timeout=5
        )
        results = asyncio.run(run_files_out_probes(subject))
        assert len(results) == len(FILES_OUT_PROBES)
        by_name = {r.probe.name: r.failure for r in results}
        assert (
            "raised RuntimeError: the provider fell over"
            in by_name["a-legitimate-read-still-works"]
        )
        # The listing probes never touch `stat_file`, so they still answer for themselves.
        assert by_name["listing-a-linked-directory"] is None

    def test_a_capability_the_backend_never_claimed_is_skipped_not_failed(self):
        subject = _FakeSubject(InProcessSandbox(), frozenset({Capability.FILES_OUT}))
        results = asyncio.run(run_files_out_probes(subject))
        skipped = {r.probe.name for r in results if r.skipped is not None}
        assert skipped == {
            "listing-a-linked-directory",
            "listing-through-a-linked-parent",
            "listing-under-a-linked-ancestor",
            "a-listing-names-its-links",
        }
        assert all(r.failure is None for r in results)
        assert all("files_list" in (r.skipped or "") for r in results if r.skipped)

    def test_a_subject_without_files_out_is_refused_rather_than_skipped_into_success(self):
        """Skipping one capability is the feature; skipping all of them is a green run of nothing.

        The distinction matters wherever a *call* to the suite is taken as evidence — a subject
        built with the wrong capabilities, or with `DEFAULT_CAPABILITIES`, would otherwise plant
        the layout, attack none of it, and return a full set of skips as success.
        """
        subject = _FakeSubject(InProcessSandbox(), frozenset({Capability.FILES_LIST}))
        with pytest.raises(ValueError, match="declares no FILES_OUT"):
            asyncio.run(run_files_out_probes(subject))
        with pytest.raises(ValueError, match="declares no FILES_OUT"):
            asyncio.run(assert_files_out_conformance(subject))

    def test_every_probe_runs_even_after_one_fails(self):
        """A backend fixing one refusal at a time learns nothing from a suite that stops early."""
        results = asyncio.run(run_files_out_probes(_leaky_subject(checks=False)))
        assert len(results) == len(FILES_OUT_PROBES)
        assert len([r for r in results if r.failure]) > 1

    def test_the_failure_names_every_probe_and_why_each_is_in_the_suite(self):
        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(assert_files_out_conformance(_leaky_subject(checks=False)))
        message = str(raised.value)
        assert "stat-through-a-linked-parent" in message
        assert "read-through-a-linked-parent" in message
        assert "why it is in the suite" in message
        assert len(raised.value.failures) == 8

    def test_a_failure_reads_as_a_failed_assertion_without_a_test_framework(self):
        """This module ships in the wheel, so it imports no test framework to raise from.

        The stdlib-only half is pinned where every protocol module's is —
        `TestZeroDependencies` in `test_sandbox_router.py`, which now covers `conformance`.
        What is left here is the consequence: the failure still has to *report* as a failed
        assertion rather than as an error.
        """
        assert issubclass(ConformanceFailure, AssertionError)

    def test_every_probe_says_why_it_is_in_the_suite(self):
        """A probe whose point is not written down is a probe someone deletes when it fails."""
        assert all(len(probe.why) > 40 for probe in FILES_OUT_PROBES)
        assert len({probe.name for probe in FILES_OUT_PROBES}) == len(FILES_OUT_PROBES)

    def test_no_probe_explains_itself_in_the_vocabulary_the_repository_retired(self):
        """`ConformanceFailure` prints `why` verbatim, so a stale word is what a user reads.

        "walk" named this check until it collided with the directory traversal in `maf.py`,
        and "lexical test" named the file name check. These `why` strings are the only copies
        a consumer of the wheel ever sees, so they are the ones worth holding.
        """
        retired = re.compile(r"\bwalk(s|ed|ing)?\b|\blexical\b", re.IGNORECASE)
        offenders = [
            probe.name
            for probes in (
                FILES_OUT_PROBES,
                FILES_IN_PROBES,
                EXEC_PROBES,
                FILES_DELETE_PROBES,
                RECLAIM_PROBES,
            )
            for probe in probes
            if retired.search(probe.why)
        ]
        assert offenders == []


class TestTheLayout:
    def test_outside_is_a_sibling_that_shares_a_prefix_with_the_working_directory(self):
        """`/maf-sandbox/work-outside` is outside `/maf-sandbox/work` and starts with it — a prefix check without the
        separator passes it, which is the bug this layout is shaped to catch."""
        paths = ConformancePaths.under("/maf-sandbox/work")
        assert paths.outside.startswith(paths.work)
        assert not paths.outside.startswith(paths.work + "/")

    def test_a_trailing_slash_on_the_working_directory_does_not_change_the_layout(self):
        assert ConformancePaths.under("/maf-sandbox/work/") == ConformancePaths.under(
            "/maf-sandbox/work"
        )

    def test_the_specimens_really_are_sandboxes(self):
        assert isinstance(_Leaky(), Sandbox)
        assert isinstance(InProcessSandbox(), Sandbox)


# ---------------------------------------------------------------------------
# The FILES_IN, EXEC and FILES_DELETE suites
# ---------------------------------------------------------------------------


class _SimulatedGuest:
    """A sandbox whose `exec` interprets the probes' commands against real stores.

    **A simulator, not a guest**: `exec` here is a Python reading of `test`, `cat`, `printf`,
    `pwd` and the two `sh -c` lines the quoting and stream probes issue. What it proves is the
    probes' own behaviour — the discharging implementation passes, and each defect below fails
    exactly its probe — and nothing about any real shell. The live suites answer that, against
    engines and services; this one answers the suite itself, the same role `_Leaky` plays for
    FILES_OUT.

    Storage is the fake's shape (`contents`/`symlinks`/`directories`), so `write_file` and
    `remove` are the real `InProcessSandbox` methods reused via composition — the surface under
    test for those suites is the sandbox, and the shipped fake discharges it.
    """

    def __init__(
        self,
        *,
        quoting: bool = True,
        exit_codes: bool = True,
        streams: str = "separate",
        guest_is_root: bool = False,
        writes_as_the_host: bool = False,
        removes_as_the_host: bool = False,
    ) -> None:
        self.contents: dict[str, bytes] = {}
        self.symlinks: dict[str, str] = {}
        self.directories: set[str] = set()
        #: What the guest program cannot write — a mode it closed, or a path the host owns.
        self.beyond_the_guest: set[str] = set()
        self._guest_is_root = guest_is_root
        self._writes_as_the_host = writes_as_the_host
        self._removes_as_the_host = removes_as_the_host
        self._quoting = quoting
        self._exit_codes = exit_codes
        #: How this specimen answers the stream probe: `separate` keeps the two apart,
        #: `merged` folds and declares it, `folded` folds and stays quiet, `mislabelled`
        #: declares the ownership while leaving the program's stderr on `stderr`, and
        #: `echoing` declares it while copying the program's *stdout* there instead.
        self._streams = streams

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        if "\\" in path:
            raise ValueError("backslash is not a valid separator")
        guest = posixpath.normpath(posixpath.join(working_directory, path))
        base = posixpath.normpath(working_directory)
        if guest != base and not guest.startswith(base + "/"):
            raise ValueError(f"path {path!r} resolves outside working directory {base!r}")
        if guest == posixpath.normpath(working_directory):
            raise ValueError("refusing to write the working directory")
        so_far = ""
        for part in (part for part in posixpath.dirname(guest).split("/") if part):
            so_far = f"{so_far}/{part}"
            if so_far in self.symlinks:
                raise ValueError(f"{so_far!r} is a link")
            if so_far in self.contents:
                raise NotADirectoryError(f"{so_far!r} is not a directory")
        if guest in self.symlinks:
            raise ValueError(f"{guest!r} is a link")
        if self._writes_as_the_host:
            # Only what this write *creates* becomes the host's: a directory the guest already
            # made stays the guest's, which is the shape the reach probes plant.
            self.beyond_the_guest.add(guest)
            so_far = base
            for part in (p for p in posixpath.dirname(guest)[len(base) :].split("/") if p):
                so_far = f"{so_far}/{part}"
                if not self._is_there(so_far):
                    self.beyond_the_guest.add(so_far)
        self.contents[guest] = content.encode("utf-8") if isinstance(content, str) else content

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        base = posixpath.normpath(working_directory)
        guest = posixpath.normpath(posixpath.join(base, path))
        if guest != base and not guest.startswith(base + "/"):
            raise ValueError(f"path {path!r} resolves outside working directory {base!r}")
        # The filesystem path check, decided from the reported kind — a link standing in any parent
        # is the escape a real backend has to refuse.
        so_far = ""
        for part in (part for part in posixpath.dirname(guest).split("/") if part):
            so_far = f"{so_far}/{part}"
            if so_far in self.symlinks:
                raise ValueError(f"{so_far!r} is a link rather than a real directory")
        if guest in self.symlinks:
            # A link named here is the thing being removed; removing it never follows it.
            self.symlinks.pop(guest)
            return
        if guest == base:
            raise ValueError(f"refusing to remove the working directory itself: {path}")
        prefix = guest.rstrip("/") + "/"
        under = [stored for stored in (*self.contents, *self.symlinks) if stored.startswith(prefix)]
        if (under or guest in self.directories) and not recursive:
            raise OSError(f"refusing to remove a directory without recursive: {path}")
        if under and not self._removes_as_the_host and not self._the_guest_can_write(guest):
            raise OSError(f"cannot empty {guest!r}: the guest program cannot write into it")
        self.contents.pop(guest, None)
        self.directories.discard(guest)
        for stored in under:
            self.contents.pop(stored, None)
            self.symlinks.pop(stored, None)
            self.directories.discard(stored)

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """The tree goes and nothing else does. ``working_directory`` is not read."""
        del working_directory, timeout
        guest = posixpath.normpath(directory)
        prefix = guest.rstrip("/") + "/"
        for stored in [p for p in self._everything() if p == guest or p.startswith(prefix)]:
            self.contents.pop(stored, None)
            self.symlinks.pop(stored, None)
            self.directories.discard(stored)

    def _everything(self) -> tuple[str, ...]:
        """Every path this simulator holds, whatever kind it is."""
        return (*self.contents, *self.symlinks, *self.directories)

    def _the_guest_can_write(self, path: str) -> bool:
        """Root's authority is not reduced by a mode; every other principal's is."""
        return self._guest_is_root or path not in self.beyond_the_guest

    def _is_there(self, path: str) -> bool:
        """Anything stored at ``path``, the directory its children imply included."""
        prefix = path.rstrip("/") + "/"
        return path in self.directories or any(
            held == path or held.startswith(prefix) for held in self._everything()
        )

    def _resolve(self, guest: str) -> str:
        """Where a `cat`/`test` actually lands: the guest's own resolution, links followed.

        `exec` is the guest's move, so it follows links exactly as a shell would — that is the
        premise the FILES_DELETE link probe hangs on, and refusing here instead would make the
        simulator disagree with every guest it stands in for.
        """
        resolved = guest
        changed = True
        while changed:
            changed = False
            parts = [part for part in resolved.split("/") if part]
            so_far = ""
            for index, part in enumerate(parts):
                so_far = f"{so_far}/{part}"
                if so_far in self.symlinks and index < len(parts) - 1:
                    resolved = self.symlinks[so_far] + resolved[len(so_far) :]
                    changed = True
                    break
        return resolved

    async def exec(self, command, *, working_directory: str, timeout: float) -> ExecResult:
        argv = [command] if isinstance(command, str) else list(command)
        # `ln -sfn target path`, which PosixGuestSubject plants links with.
        if argv[0:1] == ["ln"] and argv[1:2] == ["-sfn"] and len(argv) == 4:
            self.symlinks[argv[3]] = argv[2]
            return ExecResult(stdout="")
        if argv[0:1] == ["sleep"]:
            # The simulator obeys the protocol's timeout rule the way a guest does not have
            # to: the point is the *backend's* duty, and this specimen discharges it. It
            # sleeps only a whisker past the bound so the suite stays fast.
            await asyncio.sleep(min(float(argv[1]), timeout + 0.05))
            if float(argv[1]) > timeout:
                raise TimeoutError
            return ExecResult(stdout="")
        # `sh -c 'printf %s "$1"' probe '<hostile>'`: the quoting probe, which prints the
        # argument itself. A quoting backend passes it through as $1. An unquoting one has
        # already handed the whole thing to a shell, so the substitution ran while the command
        # line was built and $1 is only the first word of what came back — modelled here by
        # evaluating `$(echo X)` to X and taking the first word.
        # `sh -c 'printf %s OUT; printf %s ERR >&2'`: the stream probe, and the only command
        # here that writes to both. Each answer below is one a backend can give.
        if argv[0:1] == ["sh"] and argv[1:2] == ["-c"] and len(argv) == 3 and ">&2" in argv[2]:
            out, err = re.findall(r"printf %s (\S+)", argv[2])
            if self._streams == "folded":
                return ExecResult(stdout=out + err)
            if self._streams == "merged":
                return ExecResult(stdout=out + err, producer_owns_stderr=True)
            if self._streams == "mislabelled":
                return ExecResult(stdout=out, stderr=err, producer_owns_stderr=True)
            if self._streams == "echoing":
                return ExecResult(stdout=out + err, stderr=out, producer_owns_stderr=True)
            return ExecResult(stdout=out, stderr=err)
        if argv[0:1] == ["sh"] and argv[1:2] == ["-c"] and len(argv) == 5 and "printf" in argv[2]:
            if self._quoting:
                return ExecResult(stdout=argv[4])
            substituted = re.sub(r"\$\(echo ([^)]*)\)", r"\1", argv[4])
            first = substituted.split()[0] if substituted.split() else ""
            return ExecResult(stdout=first)
        if argv[0:1] == ["mkdir"]:
            # `-p` and plain alike: an empty directory is a real entry here, because the
            # simulator's remove refuses one without recursive only when it is recorded.
            operand = posixpath.normpath(posixpath.join(working_directory, argv[-1]))
            if not self._the_guest_can_write(posixpath.dirname(operand)):
                return ExecResult(stdout="", stderr="permission denied", exit_code=1)
            self.directories.add(operand)
            return ExecResult(stdout="")
        if argv[0:1] == ["rmdir"]:
            self.directories.discard(
                posixpath.normpath(posixpath.join(working_directory, argv[-1]))
            )
            return ExecResult(stdout="")
        if argv[0:1] == ["chmod"]:
            # `chmod 500 <dir>`: the guest closing a directory against itself. It records the
            # mode whoever owns the directory, because what a mode *means* is `test -w`'s to
            # answer and root's answer does not change.
            self.beyond_the_guest.add(
                posixpath.normpath(posixpath.join(working_directory, argv[-1]))
            )
            return ExecResult(stdout="")
        if argv[0:1] == ["test"]:
            operand = self._resolve(posixpath.normpath(posixpath.join(working_directory, argv[-1])))
            if argv[1:2] == ["-f"]:
                hits = operand in self.contents
            elif argv[1:2] == ["-w"]:
                hits = self._the_guest_can_write(operand)
            else:  # -e
                hits = (
                    operand in self.contents
                    or operand in self.symlinks
                    or operand in self.directories
                    # A directory a write implied, which a guest's `mkdir -p` made real. Without
                    # it a probe asking whether a removed directory is gone answers "gone" for a
                    # backend that removed nothing at all.
                    or any(p.startswith(operand + "/") for p in self._everything())
                )
            return ExecResult(stdout="", exit_code=0 if hits else 1)
        if argv[0:1] == ["cat"]:
            operand = self._resolve(posixpath.normpath(posixpath.join(working_directory, argv[-1])))
            if operand not in self.contents:
                return ExecResult(stdout="", stderr="no such file", exit_code=1)
            content = self.contents[operand]
            return ExecResult(
                stdout=content.decode("utf-8", errors="surrogateescape"),
            )
        if argv[0:1] == ["printf"]:
            return ExecResult(stdout=argv[1])
        if argv[0:1] == ["pwd"]:
            return ExecResult(stdout=posixpath.normpath(working_directory))
        if isinstance(command, str) and command.startswith("exit "):
            code = int(command.split()[1])
            return ExecResult(stdout="", exit_code=code if self._exit_codes else 1)
        return ExecResult(stdout="", stderr=f"unsupported: {argv[0]!r}", exit_code=127)


class _SimSubject(PosixGuestSubject):
    """`_SimulatedGuest` planted the way a Linux guest is: `write_file` and `ln`."""


def _sim_subject(**kwargs) -> _SimSubject:
    sandbox = _SimulatedGuest(**kwargs)
    return _SimSubject(
        sandbox=sandbox, working_directory=_WORK, capabilities=_EVERYTHING, exec_timeout=5
    )


def _sim_results(subject: _SimSubject, run) -> dict[str, str | None]:
    results = asyncio.run(run(subject))
    return {r.probe.name: r.failure for r in results if r.skipped is None}


class TestFilesInConformance:
    def test_the_simulator_answers_every_probe(self):
        assert _sim_results(_sim_subject(), run_files_in_probes) == dict.fromkeys(
            [p.name for p in FILES_IN_PROBES], None
        )

    def test_a_write_that_lands_nowhere_fails_the_positive_control(self):
        class _Vanishing(_SimulatedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                del path, content, working_directory  # the transport that drops every write

        failures = _sim_results(
            _SimSubject(sandbox=_Vanishing(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_in_probes,
        )
        assert set(failures.values()) != {None}

    def test_a_write_that_translates_bytes_fails_the_fidelity_probe(self):
        class _Translating(_SimulatedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                if isinstance(content, bytes):
                    # LF → CRLF: the text-mode translation a transport commits when nobody
                    # told it the payload is bytes. The probe's payload carries LF (in its
                    # ASCII run and its text head), so the mangling is one it can see.
                    content = content.replace(b"\n", b"\r\n")
                await super().write_file(path, content, working_directory=working_directory)

        failures = _sim_results(
            _SimSubject(sandbox=_Translating(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_in_probes,
        )
        assert failures["bytes-survive-the-round-trip"] is not None

    def test_a_write_that_appends_fails_the_replacement_probe(self):
        class _Appending(_SimulatedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                path = posixpath.normpath(posixpath.join(working_directory, path))
                blob = content.encode("utf-8") if isinstance(content, str) else content
                self.contents[path] = self.contents.get(path, b"") + blob

        failures = _sim_results(
            _SimSubject(sandbox=_Appending(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_in_probes,
        )
        assert failures["a-second-write-replaces"] is not None
        assert failures["a-write-lands-and-reads-back"] is None

    def test_a_subject_without_files_in_is_refused(self):
        with pytest.raises(ValueError, match="declares no FILES_IN"):
            asyncio.run(run_files_in_probes(_FakeSubject(InProcessSandbox(), _BOTH)))


class TestExecConformance:
    def test_the_simulator_answers_every_probe(self):
        assert _sim_results(_sim_subject(), run_exec_probes) == dict.fromkeys(
            [p.name for p in EXEC_PROBES], None
        )

    def test_a_backend_that_normalises_exit_codes_fails_the_fidelity_probe(self):
        failures = _sim_results(_sim_subject(exit_codes=False), run_exec_probes)
        assert failures["exit-code-fidelity"] is not None
        assert failures["an-argv-sequence-runs"] is None

    def test_a_backend_that_joins_argv_unquoted_fails_the_quoting_probe(self):
        failures = _sim_results(_sim_subject(quoting=False), run_exec_probes)
        assert failures["argv-is-quoted"] is not None
        assert failures["an-argv-sequence-runs"] is None

    def test_a_backend_that_folds_stderr_into_stdout_quietly_fails_the_stream_probe(self):
        failures = _sim_results(_sim_subject(streams="folded"), run_exec_probes)
        assert failures["streams-stay-separate"] is not None
        assert failures["an-argv-sequence-runs"] is None

    def test_a_merge_the_result_declares_is_conformant(self):
        """The transport merges and says so, so the probe has to admit that answer.

        A suite that failed it would hold a fourth backend to a rule core's own launcher
        breaks — and the field exists precisely so the honest merge is expressible.
        """
        assert _sim_results(_sim_subject(streams="merged"), run_exec_probes) == dict.fromkeys(
            [p.name for p in EXEC_PROBES], None
        )

    @pytest.mark.parametrize("streams", ["mislabelled", "echoing"])
    def test_a_declared_ownership_still_owes_an_stderr_with_none_of_the_programs_words(
        self, streams
    ):
        """Setting the flag over any of the guest's words is the worse failure.

        A caller reading `producer_owns_stderr` treats that field as the producer's, so a kind
        withholding guest text surfaces the guest's own words whole. Either marker there
        breaks it: `mislabelled` leaves the program's stderr, `echoing` copies its stdout.
        """
        failures = _sim_results(_sim_subject(streams=streams), run_exec_probes)
        assert failures["streams-stay-separate"] is not None

    def test_a_timeout_that_returns_fails_the_last_probe(self):
        class _NeverTimesOut(_SimulatedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
                if isinstance(command, list) and command[0:1] == ["sleep"]:
                    return ExecResult(stdout="")  # the overrun that quietly returns
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        failures = _sim_results(
            _SimSubject(
                sandbox=_NeverTimesOut(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_exec_probes,
        )
        assert failures["a-timeout-raises-timeout-error"] is not None

    def test_the_timeout_probe_is_last(self):
        """Two backends discard the sandbox on timeout, so nothing may run after this probe."""
        assert EXEC_PROBES[-1].name == "a-timeout-raises-timeout-error"

    def test_every_probe_says_why_it_is_in_the_suite(self):
        for probes in (FILES_IN_PROBES, EXEC_PROBES, FILES_DELETE_PROBES, REACH_PROBES):
            assert all(len(probe.why) > 40 for probe in probes)
            assert len({probe.name for probe in probes}) == len(probes)


class TestFilesDeleteConformance:
    def test_the_simulator_answers_every_probe(self):
        assert _sim_results(_sim_subject(), run_files_delete_probes) == dict.fromkeys(
            [p.name for p in FILES_DELETE_PROBES], None
        )

    def test_a_removal_that_does_nothing_fails_the_positive_control(self):
        class _Inert(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                del path, working_directory, recursive

        failures = _sim_results(
            _SimSubject(sandbox=_Inert(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_delete_probes,
        )
        assert failures["a-removal-removes"] is not None
        assert failures["recursive-removes-the-tree"] is not None

    def test_a_destructive_refusal_fails_the_intact_assertions(self):
        """Delete first, raise after: a refusal that performed its own damage.

        Each refusal probe asserts the survivor still stands, so a backend that did the damage
        and then raised the documented error fails the probe it would otherwise have passed.
        """

        class _Destructive(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                # Do the damage plainly: everything from the resolved guest down, target
                # included when a link is on the path.
                resolved = self._resolve(guest)
                resolved = self.symlinks.get(resolved, resolved)
                prefix = resolved.rstrip("/") + "/"
                for stored in list(self.contents):
                    if stored == resolved or stored.startswith(prefix):
                        self.contents.pop(stored, None)
                for stored in list(self.symlinks):
                    if stored == resolved or stored.startswith(prefix):
                        self.symlinks.pop(stored)
                for stored in list(self.directories):
                    if stored == resolved or stored.startswith(prefix):
                        self.directories.discard(stored)
                # Then raise what the protocol documents, as though nothing happened.
                if guest == base:
                    raise ValueError(f"refusing to remove the working directory itself: {path}")
                if guest != resolved or path.startswith(".."):
                    raise ValueError(f"path {path!r} resolves outside working directory")
                raise OSError(f"could not remove {path}")

        failures = _sim_results(
            _SimSubject(sandbox=_Destructive(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_delete_probes,
        )
        assert failures["the-working-directory-is-refused"] is not None
        assert failures["a-path-outside-is-refused"] is not None
        assert failures["a-path-through-a-linked-parent-is-refused"] is not None
        assert failures["a-directory-needs-recursive"] is not None
        assert failures["an-empty-directory-needs-recursive"] is not None

    def test_an_overbroad_removal_fails_the_sentinel_assertions(self):
        """rm -rf the working directory whole: the tree probe's own sentinel catches it."""

        class _ScorchedEarth(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                if recursive:
                    for stored in (*self.contents, *self.symlinks):
                        if stored.startswith(base + "/"):
                            self.contents.pop(stored, None)
                            self.symlinks.pop(stored, None)
                    return
                await super().remove(path, working_directory=working_directory, recursive=recursive)

        failures = _sim_results(
            _SimSubject(
                sandbox=_ScorchedEarth(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_files_delete_probes,
        )
        # The tree probe's bystander check is what catches it — the sentinel lives in the same
        # probe, so a recursive removal scoped to nothing at all fails it directly.
        assert failures["recursive-removes-the-tree"] is not None
        assert "beside the tree" in failures["recursive-removes-the-tree"]

    def test_an_instant_timeout_is_not_the_bound_expiring(self):
        """A backend raising TimeoutError for its own ceiling passes the type check and fails."""

        class _OwnCeiling(_SimulatedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
                if isinstance(command, list) and command[0:1] == ["sleep"]:
                    raise TimeoutError  # immediately, without any wait
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        failures = _sim_results(
            _SimSubject(sandbox=_OwnCeiling(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_exec_probes,
        )
        assert failures["a-timeout-raises-timeout-error"] is not None
        assert "under half the bound" in failures["a-timeout-raises-timeout-error"]

    def test_a_delayed_timeout_ignoring_the_bound_fails_the_probe(self):
        """A backend that sleeps its own ceiling before raising passes the lower bound only."""

        class _IgnoresTheBound(_SimulatedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
                if isinstance(command, list) and command[0:1] == ["sleep"]:
                    await asyncio.sleep(5)  # well past the 1s the caller allowed
                    raise TimeoutError
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        failures = _sim_results(
            _SimSubject(
                sandbox=_IgnoresTheBound(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_exec_probes,
        )
        assert failures["a-timeout-raises-timeout-error"] is not None
        assert "ignored the caller's timeout" in failures["a-timeout-raises-timeout-error"]

    def test_a_non_idempotent_removal_fails_the_missing_path_probe(self):
        """Succeeds on never-seen paths, raises on the repeat — the finally-breaker."""

        class _RepeatRaises(_SimulatedGuest):
            def __init__(self):
                super().__init__()
                self._removed: set[str] = set()

            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                if guest in self._removed:
                    raise FileNotFoundError(path)
                self._removed.add(guest)
                await super().remove(path, working_directory=working_directory, recursive=recursive)

        failures = _sim_results(
            _SimSubject(sandbox=_RepeatRaises(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_delete_probes,
        )
        assert failures["a-missing-path-is-success"] is not None

    def test_an_empty_directory_carved_out_fails_the_empty_probe(self):
        """The quiet rmdir: non-empty refused, empty deleted without recursive."""

        class _CarvesOutEmpty(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                if guest in self.directories:
                    self.directories.discard(guest)
                    return  # an empty directory goes without the word
                await super().remove(path, working_directory=working_directory, recursive=recursive)

        failures = _sim_results(
            _SimSubject(
                sandbox=_CarvesOutEmpty(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_files_delete_probes,
        )
        assert failures["an-empty-directory-needs-recursive"] is not None
        assert failures["a-directory-needs-recursive"] is None

    def test_a_removal_that_follows_links_fails_the_link_probe(self):
        class _Following(_SimulatedGuest):
            def _resolve_all(self, guest: str) -> str:
                """Resolve the final component too — the leak: remove deletes the target."""
                resolved = self._resolve(guest)
                return self.symlinks.get(resolved, resolved)

            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                if guest not in self.symlinks:
                    await super().remove(
                        path, working_directory=working_directory, recursive=recursive
                    )
                    return
                target = self._resolve_all(guest)
                self.symlinks.pop(guest, None)
                self.contents.pop(target, None)  # the escape: the target goes instead

        failures = _sim_results(
            _SimSubject(sandbox=_Following(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_delete_probes,
        )
        assert failures["a-link-is-removed-never-followed"] is not None

    def test_a_recursive_removal_that_follows_interior_links_fails_the_interior_probe(self):
        """The escape one level down: the tree goes, and the target outside goes with it.

        The only link the rest of the suite plants is the removal target itself, so a
        backend that resolves links *under* a tree it is deleting passes every other delete
        probe.
        """

        class _FollowingInteriorLinks(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                if not recursive:
                    await super().remove(
                        path, working_directory=working_directory, recursive=recursive
                    )
                    return
                # The recursive delete that resolves every link under the tree: the
                # service-side behaviour the probe hunts, where an interior link's target is
                # deleted from outside the working directory.
                prefix = guest.rstrip("/") + "/"
                for stored in sorted(
                    s for s in (*self.contents, *self.symlinks) if s.startswith(prefix)
                ):
                    resolved = stored
                    while resolved in self.symlinks:
                        resolved = self.symlinks[resolved]
                    self.contents.pop(stored, None)
                    self.symlinks.pop(stored, None)
                    self.directories.discard(stored)
                    self.contents.pop(resolved, None)  # the escape: the target goes too
                # The named path itself goes, correctly. This specimen's only defect is what a
                # recursive delete does to links *inside* the tree, so everything else about it
                # has to conform — including taking a link named directly rather than leaving it.
                self.contents.pop(guest, None)
                self.symlinks.pop(guest, None)
                self.directories.discard(guest)

        failures = _sim_results(
            _SimSubject(
                sandbox=_FollowingInteriorLinks(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_files_delete_probes,
        )
        assert failures["a-link-inside-a-recursive-removal-is-unlinked-not-followed"] is not None
        assert failures["a-link-is-removed-never-followed"] is None
        assert failures["recursive-removes-the-tree"] is None

    def test_the_probes_hold_a_subject_rooted_anywhere(self):
        """`working_directory` is the subject's to choose, so no probe may spell it.

        `ConformancePaths.outside` is derived from it — `/workspace` plants
        `/workspace-outside` — and a probe addressing `../work-outside/...` would attack a path
        nobody planted and fail a conforming backend.
        """
        failures = _sim_results(
            _SimSubject(
                sandbox=_SimulatedGuest(),
                working_directory="/workspace",
                capabilities=_EVERYTHING,
            ),
            run_files_delete_probes,
        )
        assert not [name for name, failure in failures.items() if failure is not None], failures

    def test_a_backend_that_confines_only_the_non_recursive_path_is_caught(self):
        """`recursive` can select a different operation, so both values have to be asked.

        Docker moves from `rm -f` to `rm -rf`, and a service may move to a tree delete
        entirely, so a backend can confine one path and escape the other.
        """

        class _LeaksOnlyWhenRecursive(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                if not recursive:
                    await super().remove(
                        path, working_directory=working_directory, recursive=recursive
                    )
                    return
                # The recursive branch resolves the final component and deletes what it names,
                # with no confinement check at all: the shape of a backend whose tree delete is
                # a different code path from its file delete.
                guest = posixpath.normpath(posixpath.join(working_directory, path))
                resolved = guest
                while resolved in self.symlinks:
                    resolved = self.symlinks[resolved]
                self.contents.pop(resolved, None)
                self.symlinks.pop(guest, None)
                self.directories.discard(resolved)

        failures = _sim_results(
            _SimSubject(
                sandbox=_LeaksOnlyWhenRecursive(),
                working_directory=_WORK,
                capabilities=_EVERYTHING,
            ),
            run_files_delete_probes,
        )
        assert failures["a-link-is-removed-never-followed"] is not None, (
            "a recursive removal that followed the link went unnoticed"
        )

    def test_a_removal_that_raises_on_missing_fails_the_idempotence_probe(self):
        class _Strict(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                guest = posixpath.normpath(posixpath.join(working_directory, path))
                prefix = guest.rstrip("/") + "/"
                present = (
                    guest in self.contents
                    or guest in self.symlinks
                    or guest in self.directories
                    or any(stored.startswith(prefix) for stored in (*self.contents, *self.symlinks))
                )
                if not present:
                    raise FileNotFoundError(path)
                await super().remove(path, working_directory=working_directory, recursive=recursive)

        failures = _sim_results(
            _SimSubject(sandbox=_Strict(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_delete_probes,
        )
        assert failures["a-missing-path-is-success"] is not None
        assert failures["a-removal-removes"] is None

    def test_a_subject_without_files_delete_is_refused(self):
        subject = _SimSubject(
            sandbox=_SimulatedGuest(),
            working_directory=_WORK,
            capabilities=_EVERYTHING - {Capability.FILES_DELETE},
        )
        with pytest.raises(ValueError, match="declares no FILES_DELETE"):
            asyncio.run(run_files_delete_probes(subject))

    def test_measurement_runs_the_probes_without_the_gate(self):
        """The undeclared subject the gated suites refuse is the one measurement exists for.

        A capability may only be declared once its mechanism passes the probes, so the probes
        have to be runnable against an undeclared mechanism or the gate can never open (#450:
        the ACAS withholding would otherwise be permanent — nothing could ever measure it).
        """
        subject = _SimSubject(
            sandbox=_SimulatedGuest(),
            working_directory=_WORK,
            capabilities=_EVERYTHING - {Capability.FILES_DELETE},
        )
        results = asyncio.run(measure_files_delete_probes(subject))
        assert [r.probe.name for r in results] == [p.name for p in FILES_DELETE_PROBES]
        assert all(r.skipped is None for r in results)
        assert all(r.failure is None for r in results)

    def test_measurement_reports_each_findings_name(self):
        """A failing mechanism is a finding, not a broken promise — and it stays a list.

        The ACAS refusal of a link reads as one failed probe here, with what it raised, which
        is the artefact #435 and #438 argue over: whether an unreclaimed run is transient
        (guest refusing a removal — exceptional path, callback) or structural (backend cannot
        delete — capability gap, router refuses up front).
        """

        class _RefusesLinks(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                base = posixpath.normpath(working_directory)
                guest = posixpath.normpath(posixpath.join(base, path))
                for stored in self.symlinks:
                    if stored == guest or stored.startswith(guest.rstrip("/") + "/"):
                        raise ValueError(
                            f"refusing to remove {path!r}: it is a link, and whether this "
                            "service unlinks one or follows it on a delete is unverified"
                        )
                await super().remove(path, working_directory=working_directory, recursive=recursive)

        subject = _SimSubject(
            sandbox=_RefusesLinks(),
            working_directory=_WORK,
            capabilities=_EVERYTHING - {Capability.FILES_DELETE},
        )
        results = asyncio.run(measure_files_delete_probes(subject))
        by_name = {r.probe.name: r.failure for r in results}
        assert by_name["a-link-is-removed-never-followed"] is not None
        assert "ValueError" in by_name["a-link-is-removed-never-followed"]
        assert by_name["a-removal-removes"] is None
        assert by_name["recursive-removes-the-tree"] is None

    def test_the_shipped_fake_answers_the_delete_probes_too(self):
        """The fake every kind's tests run against discharges the delete contract as well."""
        subject = _FakeSubject(
            InProcessSandbox(),
            frozenset({Capability.FILES_OUT, Capability.FILES_LIST, Capability.FILES_DELETE}),
        )
        # The fake's `exec` is scripted, so only the remove-driven probes can run against it;
        # the exec-verified ones need a guest. Those it cannot answer, it must not pretend to.
        with pytest.raises(ConformanceFailure):
            asyncio.run(assert_files_delete_conformance(subject))


class _RuntimeOnlyGuest(InProcessSandbox):
    """The shipped fake with no shell and no push surface: `exec` and `write_file` refuse.

    The runtime-only shape #382 and #425 name, where the guest is reached through `run_code` or
    a store API alone. `reclaim` stays the fake's real one, because `reclaim` is mandatory
    whatever else a backend declines to serve.
    """

    async def exec(self, command, *, working_directory, timeout):
        del command, working_directory, timeout
        raise NotImplementedError("this specimen declares no EXEC")

    async def write_file(self, path, content, *, working_directory):
        del path, content, working_directory
        raise NotImplementedError("this specimen serves no push surface")


class _RuntimeOnlySubject:
    """Plants and sees through the guest's own store, never through `exec` or `write_file`.

    What a shell-less backend's subject does with `run_code` or a store API; the fake's own
    dicts stand in for that native mechanism here.
    """

    def __init__(self, sandbox: InProcessSandbox) -> None:
        self.sandbox = sandbox
        self.working_directory = _WORK
        self.capabilities = frozenset()

    async def plant_file(self, path: str, content: bytes) -> None:
        self.sandbox.contents[path] = content

    async def plant_symlink(self, path: str, target: str) -> None:
        del target  # the fake stores no target, and `exists` answers for the name either way
        self.sandbox.symlinks.add(path)

    async def exists(self, path: str) -> bool:
        # A directory nobody created explicitly is the one its children imply, which is how the
        # fake itself reads its stores.
        prefix = path.rstrip("/") + "/"
        stored = (*self.sandbox.contents, *self.sandbox.symlinks, *self.sandbox.directories)
        return any(held == path or held.startswith(prefix) for held in stored)

    async def plant_directory_the_guest_owns(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to make {path!r}")

    async def the_guest_could_have_made(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to ask about {path!r}")

    async def plant_directory_the_guest_cannot_write_into(self, path: str) -> bool:
        """The same: a mode means nothing to a store that keeps bytes and names."""
        raise NotImplementedError(f"no guest here to close {path!r} against")


class TestReclaimConformance:
    """No gate here, so the negatives are backends that answer without doing what they promise."""

    def test_the_simulator_answers_every_probe(self):
        assert _sim_results(_sim_subject(), run_reclaim_probes) == dict.fromkeys(
            [p.name for p in RECLAIM_PROBES], None
        )

    def _failures(self, sandbox: _SimulatedGuest) -> dict[str, str | None]:
        return _sim_results(
            _SimSubject(sandbox=sandbox, working_directory=_WORK, capabilities=_EVERYTHING),
            run_reclaim_probes,
        )

    def test_a_reclaim_that_removes_nothing_fails_the_positive_control(self):
        """Answering and doing nothing: every other probe here asks what did *not* happen."""

        class _Inert(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                del directory, working_directory, timeout

        failures = self._failures(_Inert())
        assert failures["a-created-directory-is-gone"] is not None
        assert failures["nested-content-goes-with-it"] is not None
        assert failures["a-missing-directory-is-success"] is None

    def test_a_reclaim_that_takes_only_the_top_level_fails_the_tree_probe(self):
        """A call's directory holds a tree, and the flat removal empties one level of it."""

        class _OnlyTheTopLevel(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                del working_directory, timeout
                prefix = posixpath.normpath(directory).rstrip("/") + "/"
                for stored in [
                    p
                    for p in self._everything()
                    if p.startswith(prefix) and "/" not in p[len(prefix) :]
                ]:
                    self.contents.pop(stored, None)
                    self.symlinks.pop(stored, None)
                    self.directories.discard(stored)

        failures = self._failures(_OnlyTheTopLevel())
        assert failures["nested-content-goes-with-it"] is not None
        assert failures["a-created-directory-is-gone"] is None

    def test_a_reclaim_that_clears_the_working_directory_fails_the_bystander_check(self):
        """The tree probe cannot see this one: everything it planted was meant to go."""

        class _TakesTheWorkDirWhole(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                del directory, timeout
                prefix = posixpath.normpath(working_directory).rstrip("/") + "/"
                for stored in [p for p in self._everything() if p.startswith(prefix)]:
                    self.contents.pop(stored, None)
                    self.symlinks.pop(stored, None)
                    self.directories.discard(stored)

        failures = self._failures(_TakesTheWorkDirWhole())
        assert failures["a-created-directory-is-gone"] is not None
        assert "a bystander file" in failures["a-created-directory-is-gone"]
        assert failures["nested-content-goes-with-it"] is None

    def test_a_reclaim_that_follows_an_interior_link_fails_the_link_probe(self):
        """The guest chooses what is inside the directory; a followed link deletes outside it."""

        class _FollowsInteriorLinks(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                del working_directory, timeout
                guest = posixpath.normpath(directory)
                prefix = guest.rstrip("/") + "/"
                for stored in sorted(p for p in self._everything() if p.startswith(prefix)):
                    resolved = stored
                    while resolved in self.symlinks:
                        resolved = self.symlinks[resolved]
                    self.contents.pop(stored, None)
                    self.symlinks.pop(stored, None)
                    self.directories.discard(stored)
                    self.contents.pop(resolved, None)  # the escape: the target goes too
                self.contents.pop(guest, None)
                self.symlinks.pop(guest, None)
                self.directories.discard(guest)

        failures = self._failures(_FollowsInteriorLinks())
        assert [name for name, failure in failures.items() if failure is not None] == [
            "a-link-inside-is-unlinked-not-followed"
        ]

    def test_a_reclaim_that_raises_on_a_missing_directory_fails_the_finally_probe(self):
        """Succeeds on a directory it can see, raises on the repeat — the finally-breaker."""

        class _StrictAboutMissing(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                guest = posixpath.normpath(directory)
                prefix = guest.rstrip("/") + "/"
                if not any(p == guest or p.startswith(prefix) for p in self._everything()):
                    raise FileNotFoundError(directory)
                await super().reclaim(
                    directory, working_directory=working_directory, timeout=timeout
                )

        failures = self._failures(_StrictAboutMissing())
        assert failures["a-missing-directory-is-success"] is not None
        assert failures["a-created-directory-is-gone"] is None
        assert failures["nested-content-goes-with-it"] is None

    def test_a_reclaim_that_moves_to_the_working_directory_first_fails_the_absent_probe(self):
        """`cd` then remove: it reports a leak over a directory that was never there."""

        class _MovesThereFirst(_SimulatedGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                base = posixpath.normpath(working_directory)
                prefix = base.rstrip("/") + "/"
                if not any(p == base or p.startswith(prefix) for p in self._everything()):
                    raise FileNotFoundError(f"cd: {working_directory}: no such directory")
                await super().reclaim(
                    directory, working_directory=working_directory, timeout=timeout
                )

        failures = self._failures(_MovesThereFirst())
        assert failures["an-absent-working-directory-still-succeeds"] is not None
        assert [name for name, failure in failures.items() if failure is not None] == [
            "an-absent-working-directory-still-succeeds"
        ]

    def test_a_guest_whose_test_binary_is_missing_fails_the_absence_only_probe(self):
        """Read 127 as "absent" and every probe asking only what *went* is vacuously green."""

        class _NoTestBinary(_SimulatedGuest):
            async def exec(self, command, *, working_directory, timeout):
                if list(command)[0:1] == ["test"]:
                    return ExecResult(stdout="", stderr="test: not found", exit_code=127)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        failures = self._failures(_NoTestBinary())
        assert failures["nested-content-goes-with-it"] is not None
        assert "exited 127" in failures["nested-content-goes-with-it"]

    def test_a_backend_with_no_exec_and_no_write_file_answers_every_probe(self):
        """The suite is mandatory, so a runtime-only backend has to be able to sit it (#639)."""
        results = asyncio.run(assert_reclaim_conformance(_RuntimeOnlySubject(_RuntimeOnlyGuest())))
        assert [r.probe.name for r in results] == [p.name for p in RECLAIM_PROBES]
        assert all(r.passed for r in results)

    def test_the_runtime_only_specimen_really_refuses_both_surfaces(self):
        """Otherwise the green run above could be a specimen that quietly kept a shell."""
        sandbox = _RuntimeOnlyGuest()
        with pytest.raises(NotImplementedError):
            asyncio.run(sandbox.exec(["test", "-e", "."], working_directory=_WORK, timeout=1))
        with pytest.raises(NotImplementedError):
            asyncio.run(sandbox.write_file("note.txt", b"", working_directory=_WORK))

    def test_a_runtime_only_backend_that_reclaims_nothing_fails_the_positive_control(self):
        """Verifying through the seam is not verifying less."""

        class _Inert(_RuntimeOnlyGuest):
            async def reclaim(self, directory, *, working_directory, timeout):
                del directory, working_directory, timeout

        results = asyncio.run(run_reclaim_probes(_RuntimeOnlySubject(_Inert())))
        failures = {r.probe.name: r.failure for r in results}
        assert failures["a-created-directory-is-gone"] is not None
        assert "still there" in failures["a-created-directory-is-gone"]

    def test_every_probe_says_why_it_is_in_the_suite(self):
        assert all(len(probe.why) > 40 for probe in RECLAIM_PROBES)
        assert len({probe.name for probe in RECLAIM_PROBES}) == len(RECLAIM_PROBES)

    def test_the_shipped_fake_answers_only_the_probes_that_need_no_guest(self):
        """Its `exec` is scripted, so a probe that verifies through a command must not pass."""
        results = asyncio.run(run_reclaim_probes(_FakeSubject(InProcessSandbox(), _EVERYTHING)))
        assert {r.probe.name for r in results if r.failure} == {
            "a-created-directory-is-gone",
            "nested-content-goes-with-it",
            "a-link-inside-is-unlinked-not-followed",
        }
        with pytest.raises(ConformanceFailure):
            asyncio.run(assert_reclaim_conformance(_FakeSubject(InProcessSandbox(), _EVERYTHING)))


class _ScriptedTest(InProcessSandbox):
    """The fake with `test` scripted: exit 0 for the flags named here, 1 for the rest.

    Scripted rather than simulated because no specimen in this file tells `-e` and `-L` apart —
    `_SimulatedGuest` reads both the same way, and a dangling link is exactly where a real
    `test` does not: `-e` is false for it and `-L` true.
    """

    def __init__(self, *true_flags: str, otherwise: int = 1) -> None:
        super().__init__()
        self.true_flags = frozenset(true_flags)
        self.otherwise = otherwise
        self.asked: list[str] = []

    async def exec(self, command, *, working_directory, timeout):
        del working_directory, timeout
        argv = list(command)
        self.asked.append(argv[1])
        code = 0 if argv[1] in self.true_flags else self.otherwise
        return ExecResult(stdout="", stderr="" if code < 2 else "test: not found", exit_code=code)


class TestPosixGuestSubjectSees:
    """`exists` asks `test -e`, then `test -L`. The second call is the whole no-follow claim."""

    def _seeing(
        self, *true_flags: str, otherwise: int = 1
    ) -> tuple[PosixGuestSubject, _ScriptedTest]:
        sandbox = _ScriptedTest(*true_flags, otherwise=otherwise)
        subject = PosixGuestSubject(
            sandbox=sandbox, working_directory=_WORK, capabilities=frozenset()
        )
        return subject, sandbox

    def test_a_path_the_first_flag_answers_for_is_there_and_costs_one_call(self):
        subject, sandbox = self._seeing("-e")
        assert asyncio.run(subject.exists(f"{_WORK}/note.txt")) is True
        assert sandbox.asked == ["-e"]

    def test_a_dangling_link_is_there_though_only_the_second_flag_answers(self):
        """No probe reaches this: the reclaim link points at a target planted beside it."""
        subject, sandbox = self._seeing("-L")
        assert asyncio.run(subject.exists(f"{_WORK}/dangling")) is True
        assert sandbox.asked == ["-e", "-L"]

    def test_a_path_neither_flag_answers_for_is_absent(self):
        subject, sandbox = self._seeing()
        assert asyncio.run(subject.exists(f"{_WORK}/gone")) is False
        assert sandbox.asked == ["-e", "-L"]

    def test_a_test_that_could_not_run_raises_rather_than_answering_absent(self):
        """127 is a missing binary, and reading it as "absent" is a probe that verified nothing."""
        subject, sandbox = self._seeing(otherwise=127)
        with pytest.raises(RuntimeError, match="could not see whether"):
            asyncio.run(subject.exists(f"{_WORK}/note.txt"))
        # Raised on the first flag: a `test` that cannot run will not run for `-L` either.
        assert sandbox.asked == ["-e"]


def test_assert_reclaim_answers_a_conforming_subject():
    """Called by name: the coverage test reads this module's AST."""
    results = asyncio.run(assert_reclaim_conformance(_sim_subject()))
    assert all(r.passed for r in results)


def test_assert_reclaim_names_the_probe_a_subject_failed():
    class _Inert(_SimulatedGuest):
        async def reclaim(self, directory, *, working_directory, timeout):
            del directory, working_directory, timeout

    subject = _SimSubject(sandbox=_Inert(), working_directory=_WORK, capabilities=_EVERYTHING)
    with pytest.raises(ConformanceFailure, match="a-created-directory-is-gone"):
        asyncio.run(assert_reclaim_conformance(subject))


def test_the_assert_functions_return_the_results():
    """Each entry point returns its results so a caller can assert on what was skipped."""
    for probes, assert_ in (
        (FILES_IN_PROBES, assert_files_in_conformance),
        (EXEC_PROBES, assert_exec_conformance),
        (FILES_DELETE_PROBES, assert_files_delete_conformance),
        (RECLAIM_PROBES, assert_reclaim_conformance),
    ):
        results = asyncio.run(assert_(_sim_subject()))
        assert [r.probe.name for r in results] == [p.name for p in probes]
        assert all(r.passed for r in results)


def test_assert_files_in_answers_a_conforming_subject():
    """Called by name — the coverage test reads this module's AST, and an aliased callee is
    invisible to it, so the package that ships the assert exercises it spelled out."""
    results = asyncio.run(assert_files_in_conformance(_sim_subject()))
    assert all(r.passed for r in results)


def test_assert_exec_answers_a_conforming_subject():
    results = asyncio.run(assert_exec_conformance(_sim_subject()))
    assert all(r.passed for r in results)


class _CurlSandbox:
    """A sandbox whose only method is a scripted `exec` answering the egress probe's curl.

    Keyed by URL substring to `(exit_code, http_code)`, so a test can play docker's shape (a
    refused connection: non-zero exit, `000`) or ACAS's (an L7 proxy deny: exit 0, `403`) without
    a network. Anything unmatched is a refused connection.
    """

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self._replies = replies

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        del path, content, working_directory  # the probes' `.probe-cwd` marker; nothing stored

    async def exec(self, command, *, working_directory: str, timeout: float) -> ExecResult:
        del working_directory, timeout
        script = list(command)[-1]  # ["sh", "-c", "curl ... <url>"]
        for url, (rc, code) in self._replies.items():
            if url in script:
                return ExecResult(stdout=code, exit_code=rc)
        return ExecResult(stdout="000", exit_code=7)


class _EgressSubject:
    """What the egress probes read: a scripted sandbox, a work dir, EXEC — and the planting seam.

    Planting is the shared one every suite runs first, so it is here even though no egress probe
    attacks a layout.
    """

    def __init__(self, sandbox: _CurlSandbox) -> None:
        self.sandbox = sandbox
        self.working_directory = _WORK
        self.capabilities = frozenset({Capability.EXEC})

    async def plant_file(self, path: str, content: bytes) -> None:
        await self.sandbox.write_file(path, content, working_directory=self.working_directory)


class _StoreSubject(_FakeSubject):
    """`_FakeSubject` whose `exists` reads the store rather than the scripted `exec`.

    The shipped fake answers every command with success, so an `exec`-borne `exists` is true
    everywhere. Right for the suites that verify through the store, wrong for the one probe that
    asks whether a name is there at all.
    """

    async def exists(self, path: str) -> bool:
        entry = await self.sandbox.stat_file(path, working_directory=posixpath.dirname(path) or "/")
        return entry is not None


class _VanishingSubject(_StoreSubject):
    """A subject whose plant lands nowhere — the harness failure the positive controls exist for."""

    async def plant_file(self, path: str, content: bytes) -> None:
        del path, content


class TestCallScopeConformance:
    """The package that ships the suite answers it too, through two real acquires.

    Two keys differing only in `call_id`, served by the fake in its per-key mode: the shape a
    backend's own test builds.
    """

    _SPEC = SandboxSpec(kind="test", isolation_scope=IsolationScope.CALL, work_dir=_WORK)
    _KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent-1", call_id="one")

    def _router(self) -> SandboxRouter:
        backend = InProcessSandboxBackend(
            sandbox_per_key=True,
            declarations=dataclasses.replace(
                FAKE_BACKEND_DECLARATIONS,
                capabilities=_EVERYTHING,
                isolation_scopes=frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL}),
            ),
        )
        return SandboxRouter([backend], min_isolation=Isolation.NONE)

    def _served(self, capabilities: frozenset[Capability] = _EVERYTHING):
        """The first subject, a factory for the second, and the seam that deletes the first."""
        router = self._router()

        async def first() -> _StoreSubject:
            return _StoreSubject(await router.acquire(self._KEY, self._SPEC), capabilities)

        async def acquire_another() -> _StoreSubject:
            second = await router.acquire(dataclasses.replace(self._KEY, call_id="two"), self._SPEC)
            return _StoreSubject(second, capabilities)

        async def dispose_this_call() -> None:
            if not await router.dispose_call(self._KEY, timeout=5.0):
                raise AssertionError("the delete did not land, so the probe would prove nothing")

        async def dispose_the_other() -> None:
            await router.dispose_call(dataclasses.replace(self._KEY, call_id="two"), timeout=5.0)

        return first, acquire_another, dispose_this_call, dispose_the_other

    def test_two_sandboxes_answer_every_probe(self):
        first, acquire_another, dispose_this_call, dispose_the_other = self._served()

        async def run():
            return await assert_call_scope_conformance(
                await first(), acquire_another, dispose_this_call, dispose_the_other
            )

        results = asyncio.run(run())
        assert [result.failure for result in results] == [None] * 5
        assert [result.skipped for result in results] == [None] * 5

    def test_one_filesystem_behind_two_keys_fails_the_separation_probes(self):
        """The specimen the suite exists for: the sharing a declaration cannot be trusted about."""
        shared = InProcessSandbox()

        async def acquire_another() -> _StoreSubject:
            return _StoreSubject(shared, _EVERYTHING)

        async def dispose_this_call() -> None:
            shared.contents.clear()

        async def dispose_the_other() -> None:
            """Nothing to release: this specimen's sandboxes are objects the test holds."""

        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(
                assert_call_scope_conformance(
                    _StoreSubject(shared, _EVERYTHING),
                    acquire_another,
                    dispose_this_call,
                    dispose_the_other,
                )
            )
        assert len(raised.value.failures) == 5
        assert "CALL_SCOPE" in str(raised.value)

    def test_a_sandbox_seeded_from_the_conversations_is_refused(self):
        """Two filesystems, and the second opens holding the first's files.

        A warm start that copies the conversation's sandbox satisfies every probe that writes
        after both exist, which is why one of them plants before the second is acquired.
        """
        first = InProcessSandbox()

        async def acquire_another() -> _StoreSubject:
            seeded = InProcessSandbox()
            seeded.contents.update(first.contents)
            return _StoreSubject(seeded, _EVERYTHING)

        async def dispose_this_call() -> None:
            first.contents.clear()

        async def dispose_the_other() -> None:
            """Nothing to release: this specimen's sandboxes are objects the test holds."""

        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(
                assert_call_scope_conformance(
                    _StoreSubject(first, _EVERYTHING),
                    acquire_another,
                    dispose_this_call,
                    dispose_the_other,
                )
            )
        assert [failure.probe.name for failure in raised.value.failures] == [
            "arrives-without-the-other-calls-data"
        ]

    def test_a_disposal_that_sweeps_the_conversation_is_refused(self):
        """Acquire folds the call; dispose still reaches by scope, thread and agent.

        Every probe that runs before a delete passes, and ending one call destroys the sandbox
        of another still running beside it — which is what the shipped backends' `dispose` would
        do if they declared the scope without changing it.
        """
        first = InProcessSandbox()
        second = InProcessSandbox()

        async def acquire_another() -> _StoreSubject:
            return _StoreSubject(second, _EVERYTHING)

        async def dispose_this_call() -> None:
            first.contents.clear()
            second.contents.clear()  # the sweep: everything for this scope, thread and agent

        async def dispose_the_other() -> None:
            """Nothing to release: this specimen's sandboxes are objects the test holds."""

        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(
                assert_call_scope_conformance(
                    _StoreSubject(first, _EVERYTHING),
                    acquire_another,
                    dispose_this_call,
                    dispose_the_other,
                )
            )
        assert [failure.probe.name for failure in raised.value.failures] == [
            "disposing-this-call-leaves-the-other"
        ]

    def test_a_plant_that_lands_nowhere_fails_rather_than_passes(self):
        """The positive control: a subject whose writes vanish must not read as separation."""
        _, acquire_another, dispose_this_call, dispose_the_other = self._served()

        async def run():
            return await assert_call_scope_conformance(
                _VanishingSubject(InProcessSandbox(), _EVERYTHING),
                acquire_another,
                dispose_this_call,
                dispose_the_other,
            )

        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(run())
        assert "attacked nothing" in str(raised.value)

    def test_no_capability_gates_the_run(self):
        """A backend owes these probes whatever else it declares.

        `plant_file` and `exists` are the subject's own seams, as they are for the mandatory
        reclaim suite, so gating on `FILES_IN` would lock a valid call-scoped backend out of the
        suite it owes for declaring the scope. The two probes that read a sandbox back still skip.
        """
        first, acquire_another, dispose_this_call, dispose_the_other = self._served(
            frozenset({Capability.EXEC})
        )

        async def run():
            return await run_call_scope_probes(
                await first(), acquire_another, dispose_this_call, dispose_the_other
            )

        results = asyncio.run(run())
        assert [result.failure for result in results] == [None] * 5
        assert [result.skipped is None for result in results] == [True, True, False, False, True]

    def test_the_second_sandbox_is_released_when_its_acquire_raises(self):
        """A create that raises part-way has still made the thing the teardown exists for.

        The acquire is the suite's own, so a caller never learns a sandbox was made; leaving it
        outside the `finally` is a live, billable one waiting on a scope purge.
        """
        released: list[str] = []

        async def acquire_another() -> _StoreSubject:
            raise RuntimeError("the provider created it and then failed")

        async def dispose_this_call() -> None:
            released.append("this call")

        async def dispose_the_other() -> None:
            released.append("the other")

        with pytest.raises(RuntimeError, match="created it and then failed"):
            asyncio.run(
                run_call_scope_probes(
                    _StoreSubject(InProcessSandbox(), _EVERYTHING),
                    acquire_another,
                    dispose_this_call,
                    dispose_the_other,
                )
            )
        assert released == ["the other"]

    def test_the_second_sandbox_is_released_when_the_run_is_cancelled(self):
        released: list[str] = []

        async def acquire_another() -> _StoreSubject:
            raise asyncio.CancelledError

        async def dispose_this_call() -> None:
            released.append("this call")

        async def dispose_the_other() -> None:
            released.append("the other")

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_call_scope_probes(
                    _StoreSubject(InProcessSandbox(), _EVERYTHING),
                    acquire_another,
                    dispose_this_call,
                    dispose_the_other,
                )
            )
        assert released == ["the other"]

    def test_two_roots_are_refused_rather_than_passing_vacuously(self):
        first, acquire_another, dispose_this_call, dispose_the_other = self._served()

        async def run():
            async def elsewhere() -> _StoreSubject:
                second = await acquire_another()
                second.working_directory = f"{_WORK}-elsewhere"
                return second

            return await run_call_scope_probes(
                await first(), elsewhere, dispose_this_call, dispose_the_other
            )

        with pytest.raises(ValueError, match="rooted at"):
            asyncio.run(run())


class TestReachConformance:
    """The reach rule as probes: what a write left behind, and what a removal took."""

    def test_the_simulator_answers_every_probe(self):
        assert _sim_results(_sim_subject(), run_reach_probes) == dict.fromkeys(
            [p.name for p in REACH_PROBES], None
        )

    def test_a_write_at_the_hosts_authority_fails_the_write_probe(self):
        failures = _sim_results(_sim_subject(writes_as_the_host=True), run_reach_probes)
        assert failures["a-write-leaves-nothing-beyond-the-guest"] is not None

    def test_a_removal_at_the_hosts_authority_fails_the_removal_probe(self):
        failures = _sim_results(_sim_subject(removes_as_the_host=True), run_reach_probes)
        assert failures["a-removal-stays-at-the-guests-authority"] is not None

    def test_one_file_plane_at_the_hosts_authority_fails_both(self):
        """The shape a backend has when its data plane is the host's and there is no other."""
        failures = _sim_results(
            _sim_subject(writes_as_the_host=True, removes_as_the_host=True), run_reach_probes
        )
        assert all(failure is not None for failure in failures.values())

    def test_a_root_guest_passes_with_nothing_to_distinguish(self):
        """Where the guest is root the two authorities are one, so the rule binds nothing.

        The same specimen that fails both probes above passes both here, which is the whole of
        why these probes are sharp on one image and vacuous on another.
        """
        assert _sim_results(
            _sim_subject(guest_is_root=True, writes_as_the_host=True, removes_as_the_host=True),
            run_reach_probes,
        ) == dict.fromkeys([p.name for p in REACH_PROBES], None)

    def test_a_working_directory_the_guest_cannot_write_stops_both_probes(self):
        """Nothing on the path is the guest's to replace, so the rule binds nothing here.

        The probes stop rather than fail: a backend acting as the host over a path no guest
        can swap is exactly the case the rule permits.
        """
        sandbox = _SimulatedGuest(writes_as_the_host=True, removes_as_the_host=True)
        sandbox.beyond_the_guest.add(_WORK)
        subject = _SimSubject(
            sandbox=sandbox, working_directory=_WORK, capabilities=_EVERYTHING, exec_timeout=5
        )
        assert _sim_results(subject, run_reach_probes) == dict.fromkeys(
            [p.name for p in REACH_PROBES], None
        )

    def test_a_removal_that_times_out_does_not_pass_the_probe(self):
        """`TimeoutError` is an `OSError`, and the survivor stands either way.

        A call that never finished read nothing about which principal ran, so accepting it as
        the guest-authority refusal reports the strongest pass this suite has on a backend that
        did nothing at all.
        """

        class _TimesOut(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                del path, working_directory, recursive
                raise TimeoutError("the service never answered")

        failures = _sim_results(
            _SimSubject(sandbox=_TimesOut(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_reach_probes,
        )
        assert failures["a-removal-stays-at-the-guests-authority"] is not None
        assert "TimeoutError" in failures["a-removal-stays-at-the-guests-authority"]

    @pytest.mark.parametrize("missing", ["mkdir", "chmod"])
    def test_a_guest_missing_a_utility_raises_rather_than_passing(self, missing):
        """127 is the harness failing, not the guest refusing, and the two look alike here.

        Read as a refusal, a missing `mkdir` says the guest may write nowhere and a missing
        `chmod` says it writes everywhere — opposite readings that stop the same probes, so the
        suite would report success having attacked nothing.
        """

        class _WithoutTheUtility(_SimulatedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
                argv = [command] if isinstance(command, str) else list(command)
                if argv[0:1] == [missing]:
                    return ExecResult(stdout="", stderr="not found", exit_code=127)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        failures = _sim_results(
            _SimSubject(
                sandbox=_WithoutTheUtility(), working_directory=_WORK, capabilities=_EVERYTHING
            ),
            run_reach_probes,
        )
        assert [f for f in failures.values() if f is not None], (
            f"a guest without `{missing}` passed every reach probe"
        )
        assert all("exited 127" in f for f in failures.values() if f is not None)

    def test_a_backend_declaring_neither_file_capability_skips_both(self):
        """Gated per probe rather than per suite, so no capability refuses the run itself."""
        subject = _SimSubject(
            sandbox=_SimulatedGuest(),
            working_directory=_WORK,
            capabilities=frozenset({Capability.EXEC}),
        )
        results = asyncio.run(run_reach_probes(subject))
        assert [result.probe.name for result in results if result.skipped] == [
            probe.name for probe in REACH_PROBES
        ]

    def test_the_assert_entry_point_names_the_suite(self):
        with pytest.raises(ConformanceFailure, match="REACH conformance probes failed"):
            asyncio.run(assert_reach_conformance(_sim_subject(writes_as_the_host=True)))


class TestEgressConformance:
    """The enforcement outcome an `Egress.ALLOWLIST` backend must share, both deny shapes (#402)."""

    _ALLOWED = "https://mcr.example/v2/"
    _DENIED = "https://pypi.example/simple/"

    def _run(self, *, allowed: tuple[int, str], denied: tuple[int, str]):
        subject = _EgressSubject(_CurlSandbox({self._ALLOWED: allowed, self._DENIED: denied}))
        return asyncio.run(
            assert_egress_conformance(subject, allowed_url=self._ALLOWED, denied_url=self._DENIED)
        )

    def test_l3_deny_passes(self):
        """docker's shape: allowed answers 2xx, denied is a refused connection (`000`)."""
        results = self._run(allowed=(0, "200"), denied=(7, "000"))
        assert all(r.passed for r in results)

    def test_l7_proxy_deny_passes(self):
        """ACAS's shape: allowed answers 2xx, denied is an L7 proxy deny (`403`)."""
        results = self._run(allowed=(0, "200"), denied=(0, "403"))
        assert all(r.passed for r in results)

    def test_a_reachable_denied_host_fails(self):
        with pytest.raises(ConformanceFailure, match="a-denied-host-is-refused"):
            self._run(allowed=(0, "200"), denied=(0, "200"))

    def test_an_unreachable_allowed_host_fails(self):
        with pytest.raises(ConformanceFailure, match="an-allowed-host-is-reachable"):
            self._run(allowed=(7, "000"), denied=(7, "000"))
