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
"""

from __future__ import annotations

import asyncio
import posixpath

import pytest

from maf_sandbox import Capability, EntryKind, ExecResult, Sandbox, SandboxEntry
from maf_sandbox.conformance import (
    EXEC_PROBES,
    FILES_DELETE_PROBES,
    FILES_IN_PROBES,
    FILES_OUT_PROBES,
    ConformanceFailure,
    ConformancePaths,
    PosixGuestSubject,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    measure_files_delete_probes,
    run_exec_probes,
    run_files_delete_probes,
    run_files_in_probes,
    run_files_out_probes,
)
from maf_sandbox.testing import InProcessSandbox

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

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Removes whatever the link *pointed at*, which is the escape a delete probe hunts.

        The leak that matters for a removal is not reading a byte from outside — it is
        unlinking something outside, where nothing has to come back for the damage to be done.
        """
        guest = self._confined(path, working_directory)
        self._walk(guest, working_directory, include_self=False)
        resolved = self._follow_parents(guest)
        resolved = self.links.get(resolved, resolved)
        prefix = resolved.rstrip("/") + "/"
        under = [p for p in (*self.contents, *self.links) if p.startswith(prefix)]
        if under and not recursive:
            raise OSError(f"refusing to remove a non-empty directory without recursive: {path}")
        for stored in (*under, resolved):
            self.contents.pop(stored, None)
            self.links.pop(stored, None)

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


# ---------------------------------------------------------------------------
# The FILES_IN, EXEC and FILES_DELETE suites
# ---------------------------------------------------------------------------


class _SimulatedGuest:
    """A sandbox whose `exec` interprets the probes' commands against real stores.

    **A simulator, not a guest**: `exec` here is a Python reading of `test`, `cat`, `printf`,
    `pwd` and the one `sh -c` the quoting probe issues. What it proves is the probes' own
    behaviour — the discharging implementation passes, and each defect below fails exactly its
    probe — and nothing about any real shell. The live suites answer that, against engines and
    services; this one answers the suite itself, the same role `_Leaky` plays for FILES_OUT.

    Storage is the fake's shape (`contents`/`symlinks`/`directories`), so `write_file` and
    `remove` are the real `InProcessSandbox` methods reused via composition — the surface under
    test for those suites is the sandbox, and the shipped fake discharges it.
    """

    def __init__(self, *, quoting: bool = True, exit_codes: bool = True) -> None:
        self.contents: dict[str, bytes] = {}
        self.symlinks: dict[str, str] = {}
        self.directories: set[str] = set()
        self._quoting = quoting
        self._exit_codes = exit_codes

    async def write_file(self, path: str, content: str | bytes) -> None:
        self.contents[path] = content.encode("utf-8") if isinstance(content, str) else content

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        base = posixpath.normpath(working_directory)
        guest = posixpath.normpath(posixpath.join(base, path))
        if guest != base and not guest.startswith(base + "/"):
            raise ValueError(f"path {path!r} resolves outside working directory {base!r}")
        # The component walk, decided from the reported kind — a link standing in any parent
        # is the escape a real backend has to refuse.
        walked = ""
        for part in (part for part in posixpath.dirname(guest).split("/") if part):
            walked = f"{walked}/{part}"
            if walked in self.symlinks:
                raise ValueError(f"{walked!r} is a link rather than a real directory")
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
        self.contents.pop(guest, None)
        self.directories.discard(guest)
        for stored in under:
            self.contents.pop(stored, None)
            self.symlinks.pop(stored, None)
            self.directories.discard(stored)

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
            walked = ""
            for index, part in enumerate(parts):
                walked = f"{walked}/{part}"
                if walked in self.symlinks and index < len(parts) - 1:
                    resolved = self.symlinks[walked] + resolved[len(walked) :]
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
        # `sh -c 'printf ... | wc -l' probe '<hostile>'`: the quoting probe. A quoting backend
        # passes the hostile string through as $1; an unquoting one has already run it through
        # a shell, which this models by splitting on whitespace and evaluating nothing.
        if argv[0:1] == ["sh"] and argv[1:2] == ["-c"] and len(argv) == 5 and "wc -l" in argv[2]:
            words = (
                [argv[4]] if self._quoting else argv[4].replace("$(", " ").replace(")", " ").split()
            )
            return ExecResult(stdout=f"{len(words)}\n")
        if argv[0:1] == ["mkdir"]:
            # `-p` and plain alike: an empty directory is a real entry here, because the
            # simulator's remove refuses one without recursive only when it is recorded.
            self.directories.add(posixpath.normpath(posixpath.join(working_directory, argv[-1])))
            return ExecResult(stdout="")
        if argv[0:1] == ["rmdir"]:
            self.directories.discard(
                posixpath.normpath(posixpath.join(working_directory, argv[-1]))
            )
            return ExecResult(stdout="")
        if argv[0:1] == ["test"]:
            operand = self._resolve(posixpath.normpath(posixpath.join(working_directory, argv[-1])))
            if argv[1:2] == ["-f"]:
                hits = operand in self.contents
            else:  # -e
                hits = (
                    operand in self.contents
                    or operand in self.symlinks
                    or operand in self.directories
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
            async def write_file(self, path: str, content: str | bytes) -> None:
                del path, content  # the transport that drops every write

        failures = _sim_results(
            _SimSubject(sandbox=_Vanishing(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_in_probes,
        )
        assert set(failures.values()) != {None}

    def test_a_write_that_translates_bytes_fails_the_fidelity_probe(self):
        class _Translating(_SimulatedGuest):
            async def write_file(self, path: str, content: str | bytes) -> None:
                if isinstance(content, bytes):
                    # LF → CRLF: the text-mode translation a transport commits when nobody
                    # told it the payload is bytes. The probe's payload carries LF (in its
                    # ASCII run and its text head), so the mangling is one it can see.
                    content = content.replace(b"\n", b"\r\n")
                await super().write_file(path, content)

        failures = _sim_results(
            _SimSubject(sandbox=_Translating(), working_directory=_WORK, capabilities=_EVERYTHING),
            run_files_in_probes,
        )
        assert failures["bytes-survive-the-round-trip"] is not None

    def test_a_write_that_appends_fails_the_replacement_probe(self):
        class _Appending(_SimulatedGuest):
            async def write_file(self, path: str, content: str | bytes) -> None:
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
        for probes in (FILES_IN_PROBES, EXEC_PROBES, FILES_DELETE_PROBES):
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

        #452 named this as the one thing it could not verify from a workstation — how a
        service-side recursive delete treats links *inside* the tree — which is exactly why
        the probe exists: a backend can pass every other delete probe and still do it, because
        the only link the rest of the suite plants is the removal target itself.
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

    def test_a_backend_that_confines_only_the_non_recursive_path_is_caught(self):
        """`recursive` can select a different operation, so both values have to be asked.

        Docker moves from `rm -f` to `rm -rf`, and a service may move to a tree delete
        entirely. A backend confining one and escaping the other would pass a suite that asked
        once — which is what every confinement probe here used to do.
        """

        class _LeaksOnlyWhenRecursive(_SimulatedGuest):
            async def remove(self, path, *, working_directory, recursive=False):
                if not recursive:
                    await super().remove(
                        path, working_directory=working_directory, recursive=recursive
                    )
                    return
                # The recursive branch resolves the final component and deletes what it names,
                # with no confinement walk at all: the shape of a backend whose tree delete is
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


def test_the_assert_functions_return_the_results():
    """Each entry point returns its results so a caller can assert on what was skipped."""
    for probes, assert_ in (
        (FILES_IN_PROBES, assert_files_in_conformance),
        (EXEC_PROBES, assert_exec_conformance),
        (FILES_DELETE_PROBES, assert_files_delete_conformance),
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
