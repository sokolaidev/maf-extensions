"""Files over the shell and the plane's refusals, answered in one vocabulary.

The shell road is pinned by the commands it issues against the in-process fake, and by what
it makes of the fake's scripted answers: the shapes that matter are the ones a guest's words
decide, since nothing else tells an unreachable file from an absent one.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from maf_sandbox import (
    EntryKind,
    ExecResult,
    FileRefusal,
    SandboxEntry,
    SandboxExecOutputLimitExceeded,
    SandboxFileRefused,
    SandboxShellTransferFailed,
    SandboxShellTransferUnfinished,
    SandboxTransferCapExceeded,
    entry_refusal,
    file_refusal,
    read_file_over_exec,
    shell_refusal,
    write_file_over_exec,
)
from maf_sandbox.file_transfer import SHELL_CHUNK_BYTES, SHELL_UTILITIES
from maf_sandbox.testing import InProcessSandbox

WORK = "/maf-sandbox/work"


class Answering(InProcessSandbox):
    """The fake with a scripted exit code and stderr for every command."""

    def __init__(self, *, exit_code: int = 0, stderr: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._exit_code = exit_code
        self._stderr = stderr

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        return ExecResult(stdout=result.stdout, stderr=self._stderr, exit_code=self._exit_code)


def _commands(fake: InProcessSandbox) -> list[str]:
    return [command for command, _, _ in fake.commands]


class TestTheVocabulary:
    @pytest.mark.parametrize(
        ("error", "refusal"),
        [
            (PermissionError("no search permission"), FileRefusal.PERMISSION_DENIED),
            (IsADirectoryError("a directory"), FileRefusal.IS_DIRECTORY),
            (FileNotFoundError("gone"), FileRefusal.NOT_FOUND),
            (NotADirectoryError("a parent is a file"), FileRefusal.INVALID_PATH),
            (ValueError("through a link"), FileRefusal.INVALID_PATH),
            (OSError("not a regular file"), FileRefusal.INVALID_PATH),
            (SandboxTransferCapExceeded("over the cap"), None),
            (TimeoutError("late"), None),
            (ConnectionResetError("the transport"), None),
            (RuntimeError("the transport"), None),
        ],
    )
    def test_the_plane_s_exceptions_name_their_refusal(self, error, refusal):
        assert file_refusal(error) is refusal

    def test_a_stat_names_its_refusal(self):
        assert entry_refusal(None) is FileRefusal.NOT_FOUND
        directory = SandboxEntry(path="d", kind=EntryKind.DIRECTORY, size_bytes=None)
        link = SandboxEntry(path="l", kind=EntryKind.SYMLINK, size_bytes=None)
        regular = SandboxEntry(path="f", kind=EntryKind.FILE, size_bytes=3)
        assert entry_refusal(directory) is FileRefusal.IS_DIRECTORY
        assert entry_refusal(link) is FileRefusal.INVALID_PATH
        assert entry_refusal(regular) is None

    @pytest.mark.parametrize(
        ("stderr", "refusal"),
        [
            ("sh: can't create /x: Permission denied", FileRefusal.PERMISSION_DENIED),
            ("sh: /x: Is a directory", FileRefusal.IS_DIRECTORY),
            ("sh: /f/x: Not a directory", FileRefusal.INVALID_PATH),
            ("sh: can't open /x: No such file or directory", FileRefusal.NOT_FOUND),
            ("sh: can't open /x: no such file", FileRefusal.NOT_FOUND),
            ("sh: base64: not found", None),
            ("mv: cannot move: Input/output error", None),
            # The path is the model's text: only the line's end is the shell's.
            (
                "sh: can't open '/tmp/Permission denied': No such file or directory",
                FileRefusal.NOT_FOUND,
            ),
            (
                "mkdir: can't create '/No such file or directory': Permission denied",
                FileRefusal.PERMISSION_DENIED,
            ),
            ("sh: /tmp/Is a directory: not found", None),
            # The last line is the diagnostic that ended the command.
            (
                "mkdir: created directory 'Permission denied'\nmv: cannot stat: No such file or directory",
                FileRefusal.NOT_FOUND,
            ),
        ],
    )
    def test_the_shell_s_words_name_their_refusal(self, stderr, refusal):
        assert shell_refusal(stderr) is refusal

    @pytest.mark.parametrize("path", ["/tmp/a\nPermission denied\nb", "/tmp/a\rb", "/tmp/a\0b"])
    def test_a_path_no_command_can_carry_is_refused_before_any_command(self, path):
        """A line break would let the path write a line of its own into a diagnostic, and a
        NUL byte cannot reach a shell at all; neither is an unclean sandbox."""
        fake = InProcessSandbox()
        with pytest.raises(SandboxFileRefused) as on_write:
            asyncio.run(write_file_over_exec(fake, path, b"1", working_directory=WORK, timeout=5))
        with pytest.raises(SandboxFileRefused) as on_read:
            asyncio.run(
                read_file_over_exec(fake, path, working_directory=WORK, timeout=5, max_bytes=8)
            )
        assert on_write.value.refusal is on_read.value.refusal is FileRefusal.INVALID_PATH
        assert fake.commands == []


class TestTheWrite:
    def test_lands_whole_through_a_staged_sibling(self):
        fake = InProcessSandbox()
        content = bytes(range(256)) * 400  # past one chunk

        asyncio.run(
            write_file_over_exec(
                fake, "/notes/todo.bin", content, working_directory=WORK, timeout=5
            )
        )

        commands = _commands(fake)
        assert all(c.startswith("export LC_ALL=C; ") for c in commands)
        commands = [c.removeprefix("export LC_ALL=C; ") for c in commands]
        refuse = "if [ -d /notes/todo.bin ]; then echo 'Is a directory' >&2; exit 1; fi"
        staged = commands[0].removeprefix(f"mkdir -p -- /notes && {refuse} && : > ")
        # A sibling in the target's directory, of a fixed length: the leaf may already be as
        # long as a name can be.
        assert staged.startswith("/notes/.maf-") and staged.endswith(".part")
        assert len(staged) == len("/notes/.maf-") + 32 + len(".part")
        chunks = [c.removeprefix("printf %s ").split(" | ")[0] for c in commands[1:-1]]
        assert len(chunks) == -(-len(content) // SHELL_CHUNK_BYTES)
        assert all(len(chunk) <= 4 * (SHELL_CHUNK_BYTES // 3) for chunk in chunks)
        assert all(c.endswith(f"base64 -d >> {staged}") for c in commands[1:-1])
        assert base64.b64decode("".join(chunks)) == content
        # The directory check again, in the command that moves: `mv` would otherwise move
        # the sibling inside a directory that appeared in between.
        assert commands[-1] == f"{refuse} && mv -f -- {staged} /notes/todo.bin"
        assert {directory for _, directory, _ in fake.commands} == {WORK}

    def test_a_second_write_over_the_path_stages_under_its_own_name(self):
        fake = InProcessSandbox()
        asyncio.run(write_file_over_exec(fake, "/tmp/f", b"1", working_directory=WORK, timeout=5))
        first = _commands(fake)
        asyncio.run(write_file_over_exec(fake, "/tmp/f", b"2", working_directory=WORK, timeout=5))
        second = _commands(fake)[len(first) :]
        assert second[0] != first[0]
        assert second[-1] != first[-1] and second[-1].endswith(".part /tmp/f")

    def test_a_relative_path_is_written_where_the_working_directory_is(self):
        fake = InProcessSandbox()
        asyncio.run(write_file_over_exec(fake, "note.txt", b"1", working_directory=WORK, timeout=5))
        first = _commands(fake)[0]
        assert first.startswith("export LC_ALL=C; mkdir -p -- . && if [ -d note.txt ]")
        assert "&& : > .maf-" in first  # the sibling is relative too

    def test_a_path_that_begins_with_a_dash_is_an_operand_not_an_option(self):
        fake = InProcessSandbox()
        asyncio.run(
            write_file_over_exec(fake, "-dir/-file", b"1", working_directory=WORK, timeout=5)
        )
        first, _, last = _commands(fake)
        assert first.startswith("export LC_ALL=C; mkdir -p -- -dir && if [ -d -dir/-file ]")
        assert "&& mv -f -- -dir/.maf-" in last and last.endswith(".part -dir/-file")

    def test_a_write_refused_part_way_takes_its_sibling_back(self):
        class Full(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                result = await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )
                if "base64 -d" in command:
                    return ExecResult(
                        stdout="", stderr="sh: can't open: Permission denied", exit_code=1
                    )
                return result

        fake = Full()
        with pytest.raises(SandboxFileRefused) as refused:
            asyncio.run(
                write_file_over_exec(fake, "/tmp/f", b"1", working_directory=WORK, timeout=5)
            )
        assert refused.value.refusal is FileRefusal.PERMISSION_DENIED
        first, _, last = _commands(fake)
        staged = first.split(" : > ")[1]
        assert last == f"export LC_ALL=C; rm -f -- {staged}"

    def test_a_sibling_that_cannot_be_taken_back_is_unfinished(self):
        class Stuck(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                result = await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )
                if "base64 -d" in command or "rm -f" in command:
                    return ExecResult(stdout="", stderr="disk on fire", exit_code=1)
                return result

        with pytest.raises(SandboxShellTransferUnfinished, match="could not be removed"):
            asyncio.run(
                write_file_over_exec(Stuck(), "/tmp/f", b"1", working_directory=WORK, timeout=5)
            )

    def test_what_the_shell_refuses_is_named(self):
        fake = Answering(exit_code=1, stderr="sh: can't create /etc/x: Permission denied")
        with pytest.raises(SandboxFileRefused) as refused:
            asyncio.run(
                write_file_over_exec(fake, "/etc/x", b"1", working_directory=WORK, timeout=5)
            )
        assert refused.value.refusal is FileRefusal.PERMISSION_DENIED
        assert "Permission denied" in refused.value.detail
        assert len(fake.commands) == 1  # nothing after the refusal

    def test_a_failure_the_words_do_not_name_is_not_a_refusal(self):
        fake = Answering(exit_code=127, stderr="sh: base64: not found")
        with pytest.raises(SandboxShellTransferFailed, match="base64: not found"):
            asyncio.run(
                write_file_over_exec(fake, "/tmp/f", b"1", working_directory=WORK, timeout=5)
            )

    @pytest.mark.parametrize(
        "raised", [TimeoutError("late"), SandboxExecOutputLimitExceeded("loud"), OSError("gone")]
    )
    def test_a_command_whose_end_is_unknown_is_unfinished(self, raised):
        fake = InProcessSandbox(raises=raised)
        with pytest.raises(SandboxShellTransferUnfinished) as unfinished:
            asyncio.run(
                write_file_over_exec(fake, "/tmp/f", b"1", working_directory=WORK, timeout=5)
            )
        assert unfinished.value.__cause__ is raised


class TestTheRead:
    def test_reads_what_the_probe_measured(self):
        encoded = base64.b64encode(b"# history\n").decode()
        fake = InProcessSandbox(
            outputs={"wc -c": "10\n", "base64 <": encoded[:8] + "\n" + encoded[8:]}
        )

        content = asyncio.run(
            read_file_over_exec(
                fake, "/conversation_history/s.md", working_directory=WORK, timeout=5, max_bytes=64
            )
        )

        assert content == b"# history\n"
        probe, read = _commands(fake)
        assert probe.startswith(
            "export LC_ALL=C; if [ ! -e /conversation_history/s.md ]; then ( : < "
        )
        assert read == "export LC_ALL=C; base64 < /conversation_history/s.md"

    @pytest.mark.parametrize(
        ("answer", "refusal"),
        [
            ("missing", FileRefusal.NOT_FOUND),
            (
                "sh: 1: cannot open /tmp/x: No such file or directory\nmissing",
                FileRefusal.NOT_FOUND,
            ),
            ("sh: can't open /tmp/x: no such file\nmissing", FileRefusal.NOT_FOUND),
            ("sh: can't open '/tmp/x': Permission denied\nmissing", FileRefusal.PERMISSION_DENIED),
            ("sh: line 1: /tmp/x: Not a directory\nmissing", FileRefusal.INVALID_PATH),
            ("directory", FileRefusal.IS_DIRECTORY),
            ("other", FileRefusal.INVALID_PATH),
            ("unreadable", FileRefusal.PERMISSION_DENIED),
        ],
    )
    def test_the_probe_s_answer_names_the_refusal(self, answer, refusal):
        """`test -e` is false behind an unsearchable ancestor as for an absent file; the open's
        own words, in the shell's phrasing (busybox, dash, bash), tell the two apart."""
        fake = InProcessSandbox(outputs={"wc -c": f"{answer}\n"})
        with pytest.raises(SandboxFileRefused) as refused:
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert refused.value.refusal is refusal
        assert len(fake.commands) == 1

    def test_a_file_over_the_cap_is_refused_before_the_read(self):
        fake = InProcessSandbox(outputs={"wc -c": "65\n"})
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert len(fake.commands) == 1

    def test_a_file_that_grew_is_refused_after_the_read(self):
        encoded = base64.b64encode(b"x" * 65).decode()
        fake = InProcessSandbox(outputs={"wc -c": "64\n", "base64 <": encoded})
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )

    def test_the_read_runs_under_a_budget_sized_for_base64(self):
        class Loud(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                if isinstance(command, str) and command.endswith("base64 < /tmp/x"):
                    assert max_output_bytes == 64 * 2 + 4096
                    raise SandboxExecOutputLimitExceeded("the file outgrew its cap")
                return await super().exec_bounded(
                    command,
                    working_directory=working_directory,
                    timeout=timeout,
                    max_output_bytes=max_output_bytes,
                )

        fake = Loud(outputs={"wc -c": "64\n"})
        with pytest.raises(SandboxShellTransferUnfinished) as unfinished:
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert unfinished.value.over_cap is True

    def test_a_probe_whose_open_fails_for_words_it_does_not_know_is_a_failure(self):
        fake = InProcessSandbox(
            outputs={"wc -c": "sh: can't open /tmp/x: Input/output error\nmissing\n"}
        )
        with pytest.raises(SandboxShellTransferFailed, match="Input/output error"):
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )

    def test_a_probe_that_overflows_is_unfinished_but_not_over_the_cap(self):
        fake = InProcessSandbox(outputs={"wc -c": "x" * 5000})
        with pytest.raises(SandboxShellTransferUnfinished) as unfinished:
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert unfinished.value.over_cap is False

    @pytest.mark.parametrize(
        ("marker", "stderr", "refusal"),
        [
            ("wc -c", "sh: can't open /tmp/x: Permission denied", FileRefusal.PERMISSION_DENIED),
            ("base64 <", "sh: can't open /tmp/x: No such file or directory", FileRefusal.NOT_FOUND),
            ("base64 <", "base64: /tmp/x: Is a directory", FileRefusal.IS_DIRECTORY),
        ],
    )
    def test_a_path_that_changed_under_the_probe_or_the_read_is_still_refused_by_code(
        self, marker, stderr, refusal
    ):
        """The path can go, become a directory or lose permission between the probe's
        branches and `wc`, or between the probe and the read; the words still name it."""

        class Shifting(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                if marker in command:
                    return ExecResult(stdout="", stderr=stderr, exit_code=1)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        fake = Shifting(outputs={"wc -c": "3\n"})
        with pytest.raises(SandboxFileRefused) as refused:
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert refused.value.refusal is refusal

    def test_a_probe_that_fails_or_answers_nonsense_is_a_failure_not_a_refusal(self):
        with pytest.raises(SandboxShellTransferFailed):
            asyncio.run(
                read_file_over_exec(
                    Answering(exit_code=2, stderr="sh: wc: not found"),
                    "/tmp/x",
                    working_directory=WORK,
                    timeout=5,
                    max_bytes=64,
                )
            )
        with pytest.raises(SandboxShellTransferFailed):
            asyncio.run(
                read_file_over_exec(
                    InProcessSandbox(outputs={"wc -c": "many\n"}),
                    "/tmp/x",
                    working_directory=WORK,
                    timeout=5,
                    max_bytes=64,
                )
            )

    def test_a_read_that_returns_no_base64_is_a_failure(self):
        fake = InProcessSandbox(outputs={"wc -c": "3\n", "base64 <": "not*base64\n"})
        with pytest.raises(SandboxShellTransferFailed):
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )

    def test_a_timed_out_read_is_unfinished(self):
        fake = InProcessSandbox(raises=TimeoutError("late"))
        with pytest.raises(SandboxShellTransferUnfinished) as unfinished:
            asyncio.run(
                read_file_over_exec(fake, "/tmp/x", working_directory=WORK, timeout=5, max_bytes=64)
            )
        assert unfinished.value.over_cap is False

    def test_the_cap_must_be_a_positive_integer(self):
        with pytest.raises(ValueError, match="max_bytes"):
            asyncio.run(
                read_file_over_exec(
                    InProcessSandbox(), "/tmp/x", working_directory=WORK, timeout=5, max_bytes=0
                )
            )


def test_the_utilities_the_road_runs_are_the_ones_it_names():
    fake = InProcessSandbox(outputs={"wc -c": "1\n", "base64 <": "eA==\n"})
    asyncio.run(write_file_over_exec(fake, "/tmp/f", b"x", working_directory=WORK, timeout=5))
    asyncio.run(read_file_over_exec(fake, "/tmp/f", working_directory=WORK, timeout=5, max_bytes=8))

    class RefusingAChunk(InProcessSandbox):
        async def exec(self, command, *, working_directory, timeout):
            if "base64 -d" in command:
                return ExecResult(stdout="", stderr="sh: Permission denied", exit_code=1)
            return await super().exec(command, working_directory=working_directory, timeout=timeout)

    refusing = RefusingAChunk()
    with pytest.raises(SandboxFileRefused):  # `rm` runs only to take a failed write back
        asyncio.run(
            write_file_over_exec(refusing, "/tmp/g", b"x", working_directory=WORK, timeout=5)
        )
    commands = _commands(fake) + _commands(refusing)
    words = {word for command in commands for word in command.replace("|", " ").split()}
    for utility in SHELL_UTILITIES:
        assert utility == "sh" or utility in words
