"""The conformance suite, held to the two things a conformance suite has to be.

It must **pass** against an implementation that discharges the duty — otherwise it is a
tripwire nobody can clear — and it must **fail**, naming the right probe, against one that does
not. The second half is the one that matters: a suite written against the same misreading it is
meant to catch passes everything (#142).

So there are two specimens here. `InProcessSandbox` is the real fake, which refuses; `_Leaky`
is written for this file and genuinely resolves through a link, the way a real engine and a
real data plane do, with each of the two duties on a switch.
"""

from __future__ import annotations

import asyncio
import posixpath

import pytest

from maf_sandbox import Capability, EntryKind, ExecResult, Sandbox, SandboxEntry
from maf_sandbox.conformance import (
    FILES_OUT_PROBES,
    ConformanceFailure,
    ConformancePaths,
    PosixGuestSubject,
    assert_files_out_conformance,
    run_files_out_probes,
)
from maf_sandbox.testing import InProcessSandbox

_WORK = "/maf-sandbox/work"
_BOTH = frozenset({Capability.FILES_OUT, Capability.FILES_LIST})


class _FakeSubject:
    """Plants straight into `InProcessSandbox`'s stores — its `exec` is scripted, not real."""

    def __init__(self, sandbox: InProcessSandbox, capabilities: frozenset[Capability]) -> None:
        self.sandbox = sandbox
        self.working_directory = _WORK
        self.capabilities = capabilities

    async def plant_file(self, path: str, content: bytes) -> None:
        await self.sandbox.write_file(path, content)

    async def plant_symlink(self, path: str, target: str) -> None:
        del target  # this fake refuses links rather than following them, so it stores no target
        self.sandbox.symlinks.add(path)


class _Leaky:
    """A sandbox that really does resolve through a link, with each duty on a switch.

    The shipped fake refuses, which is correct behaviour and the wrong specimen for testing a
    suite: probes have to fail against something that leaks. This one is the escape modelled —
    a link component is replaced by its target before anything is looked up, exactly as an
    engine resolving daemon-side or a data plane resolving service-side does.

    ``walks`` turns the component walk off. ``names_links`` makes it answer
    :data:`~maf_sandbox.EntryKind.OTHER` for a link instead of
    :data:`~maf_sandbox.EntryKind.SYMLINK` — the backend that cannot tell one from a fifo, which
    still refuses every path attacked here and cannot say why.
    """

    def __init__(self, *, walks: bool = True, names_links: bool = True) -> None:
        self.contents: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        self._walks = walks
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

    def _walk(self, guest: str, working_directory: str, *, include_self: bool = False) -> None:
        """The component walk, deciding from the *reported* kind rather than private knowledge.

        Which is the point of `names_links`: a backend that can only report `OTHER` has no way
        to name the escape, so this walk cannot either, and the probes see the difference.
        """
        del working_directory
        if not self._walks:
            return
        deepest = guest if include_self else posixpath.dirname(guest)
        walked = ""
        for part in (part for part in deepest.split("/") if part):
            walked = f"{walked}/{part}"
            found = self._classify(walked)
            if found is None:
                return
            kind, _ = found
            if kind is EntryKind.SYMLINK:
                raise ValueError(f"{walked!r} is a link rather than a real directory")
            if kind is not EntryKind.DIRECTORY:
                raise NotADirectoryError(f"{walked!r} is not a directory")

    async def write_file(self, path: str, content: str | bytes) -> None:
        self.contents[path] = content.encode("utf-8") if isinstance(content, str) else content

    async def exec(self, command, *, working_directory: str, timeout: float) -> ExecResult:
        del working_directory, timeout
        # The only command the subject issues: `ln -sfn <target> <path>`.
        match list(command):
            case ["ln", "-sfn", target, path]:
                self.links[path] = target
                return ExecResult(stdout="")
        return ExecResult(stdout="", stderr="unsupported", exit_code=1)

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        guest = self._confined(path, working_directory)
        self._walk(guest, working_directory)
        found = self._classify(self._follow_parents(guest))
        if found is None:
            return None
        kind, size_bytes = found
        return SandboxEntry(path=posixpath.normpath(path), kind=kind, size_bytes=size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        del max_bytes
        guest = self._confined(path, working_directory)
        self._walk(guest, working_directory)
        resolved = self._follow_parents(guest)
        if resolved in self.links:
            raise OSError(f"{path!r} is not a regular file and is refused")
        if resolved not in self.contents:
            raise FileNotFoundError(f"no such file: {path!r}")
        return self.contents[resolved]

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        guest = self._confined(path, working_directory)
        self._walk(guest, working_directory, include_self=True)
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
        """`InProcessSandbox` runs the shared walk, so it answers the same probes a backend does."""
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

    def test_a_backend_that_skips_the_walk_fails_exactly_the_walk_probes(self):
        failures = {name for name, why in _results(_leaky_subject(walks=False)).items() if why}
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
        failure = _results(_leaky_subject(walks=False))["read-through-a-linked-parent"]
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
        """A walk that starts *at* the work dir passes the work-dir probe and fails this one.

        The two are separate probes because they are separate mistakes, and the specimen here
        makes only the second: it classifies the work dir it was handed and nothing above it.
        """

        class _WalksFromTheWorkDir(_Leaky):
            def _walk(self, guest, working_directory, *, include_self=False):
                del guest, include_self
                found = self._classify(posixpath.normpath(working_directory))
                if found is not None and found[0] is EntryKind.SYMLINK:
                    raise ValueError("work dir is a link rather than a real directory")

        subject = _LeakySubject(
            sandbox=_WalksFromTheWorkDir(),
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

    def test_every_probe_runs_even_after_one_fails(self):
        """A backend fixing one refusal at a time learns nothing from a suite that stops early."""
        results = asyncio.run(run_files_out_probes(_leaky_subject(walks=False)))
        assert len(results) == len(FILES_OUT_PROBES)
        assert len([r for r in results if r.failure]) > 1

    def test_the_failure_names_every_probe_and_why_each_is_in_the_suite(self):
        with pytest.raises(ConformanceFailure) as raised:
            asyncio.run(assert_files_out_conformance(_leaky_subject(walks=False)))
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
