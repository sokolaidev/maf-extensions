"""Fixed offline validation launcher; copied into an immutable Linux sandbox image."""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

OUTPUT_LIMIT = 128 * 1024
INSTALL = Path("/opt/maf-terraform")
REGISTRY_HOST = "registry.terraform.io"
MAX_RECORDS = 1024
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_WORD = re.compile(r"[^\W\d][\w-]*")
_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_-]*)\r?\n")
_PART = r"[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?"
_REGISTRY_SOURCE = re.compile(rf"(?:([^/]+)/)?({_PART}/{_PART}/[0-9a-z]{{1,64}})")


def clean_environment(private: Path) -> dict[str, str]:
    """Create a complete environment, inheriting no CLI flags, variables, or credentials."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private / "home"),
        "TMPDIR": str(private / "tmp"),
        "TF_DATA_DIR": str(private / "data"),
        "TF_CLI_CONFIG_FILE": str(INSTALL / "terraform.rc"),
        "TF_INPUT": "0",
        "TF_IN_AUTOMATION": "1",
        "CHECKPOINT_DISABLE": "1",
        "LANG": "C.UTF-8",
    }


class Supervisor:
    """Share one output allowance and deadline across all CLI processes."""

    def __init__(self, timeout: float, environment: dict[str, str]) -> None:
        self.deadline = time.monotonic() + timeout
        self.remaining = OUTPUT_LIMIT
        self.environment = environment

    def execute_phase(self, command: list[str], cwd: Path) -> dict[str, Any]:
        """Bound both streams, kill the process group on every exit, and reap the child."""
        if time.monotonic() >= self.deadline:
            raise TimeoutError("deadline")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        output = {"stdout": bytearray(), "stderr": bytearray()}
        assert process.stdout is not None and process.stderr is not None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    left = self.deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("deadline")
                    for key, _events in selector.select(min(left, 0.1)):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        self.remaining -= len(chunk)
                        if self.remaining < 0:
                            raise RuntimeError("output limit")
                        output[key.data].extend(chunk)
                left = self.deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("deadline")
                code = process.wait(timeout=left)
        finally:
            # Includes descendants that retained a pipe after the parent exited. A process
            # escaping this group still belongs to the disposable sandbox, not a warm session.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # An already-exited process group needs no further signal.
                pass
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)
        return {
            "exit_code": code,
            **{name: data.decode("utf-8", errors="strict") for name, data in output.items()},
        }


def _string_end(text: str, index: int) -> tuple[int, bool]:
    """Skip a quoted template; report whether it held only unescaped literal text."""
    literal = True
    index += 1
    while index < len(text):
        if text[index] == '"':
            return index + 1, literal
        if text[index] == "\n":
            break
        if text[index] == "\\" or text.startswith(("$${", "%%{"), index):
            literal = False
            index += 2 if text[index] == "\\" else 3
        elif text.startswith(("${", "%{"), index):
            literal = False
            index = _interpolation_end(text, index + 2)
        else:
            index += 1
    raise ValueError("unterminated string")


def _interpolation_end(text: str, index: int) -> int:
    """Skip an interpolation body to its matching brace; comments and heredocs are unsure."""
    depth = 1
    while index < len(text):
        if text[index] == '"':
            index = _string_end(text, index)[0]
            continue
        if text[index] == "#" or text.startswith(("//", "/*", "<<"), index):
            break
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        index += 1
        if depth == 0:
            return index
    raise ValueError("unsupported interpolation")


def _hcl_tokens(text: str) -> list[tuple[str, str | None]]:
    """Lex native syntax far enough to see block structure and literal strings."""
    tokens: list[tuple[str, str | None]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\n":
            tokens.append(("newline", None))
            index += 1
        elif char in " \t\r":
            index += 1
        elif char == "#" or text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            index = end + 2
        elif char == '"':
            end, literal = _string_end(text, index)
            tokens.append(("string", text[index + 1 : end - 1] if literal else None))
            index = end
        elif heredoc := _HEREDOC.match(text, index):
            index = heredoc.end()
            while True:
                end = text.find("\n", index)
                if text[index : len(text) if end < 0 else end].strip() == heredoc.group(1):
                    index = len(text) if end < 0 else end
                    break
                if end < 0:
                    raise ValueError("unterminated heredoc")
                index = end + 1
            tokens.append(("string", None))
        elif word := _WORD.match(text, index):
            tokens.append(("word", word.group()))
            index = word.end()
        elif char in "{}" or (char == "=" and not text.startswith(("==", "=>"), index)):
            tokens.append((char, None))
            index += 1
        else:
            tokens.append(("other", None))
            index += 2 if text.startswith(("==", "=>", "!=", ">=", "<="), index) else 1
    return tokens


def _hcl_module_calls(text: str) -> dict[str, dict[str, str | None] | None]:
    """Find top-level module blocks with their literal source and version arguments."""
    tokens = _hcl_tokens(text) + [("newline", None)] * 3
    calls: dict[str, dict[str, str | None] | None] = {}
    depth, index, line_start = 0, 0, True
    while index < len(tokens) - 3:
        kind, value = tokens[index]
        header = [token[0] for token in tokens[index : index + 3]]
        if (
            depth == 0
            and line_start
            and (kind, value) == ("word", "module")
            and (
                header[1:] in (["string", "{"], ["word", "{"]) and tokens[index + 1][1] is not None
            )
        ):
            label = tokens[index + 1][1]
            assert label is not None
            arguments: dict[str, str | None] = {}
            index, block_depth, line_start = index + 3, 1, True
            while block_depth and index < len(tokens) - 3:
                kind, value = tokens[index]
                if (
                    block_depth == 1
                    and line_start
                    and kind == "word"
                    and tokens[index + 1][0] == "="
                ):
                    literal = tokens[index + 2][0] == "string" and tokens[index + 3][0] in (
                        "newline",
                        "}",
                    )
                    if value in ("source", "version"):
                        assert value is not None
                        duplicate = value in arguments
                        arguments[value] = (
                            None if duplicate or not literal else tokens[index + 2][1]
                        )
                block_depth += {"{": 1, "}": -1}.get(kind, 0)
                line_start = kind in ("newline", "{")
                index += 1
            if block_depth:
                raise ValueError("unterminated block")
            calls[label] = None if label in calls else arguments
            line_start = False
            continue
        depth += {"{": 1, "}": -1}.get(kind, 0)
        if depth < 0:
            raise ValueError("unbalanced braces")
        line_start = kind == "newline"
        index += 1
    if depth:
        raise ValueError("unbalanced braces")
    return calls


def _json_module_calls(text: str) -> dict[str, dict[str, str | None] | None]:
    """Read module calls from JSON configuration; template strings are not literals."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate JSON key")
        return dict(pairs)

    document = json.loads(text, object_pairs_hook=unique)
    blocks = document.get("module", []) if isinstance(document, dict) else []
    calls: dict[str, dict[str, str | None] | None] = {}
    for block in blocks if isinstance(blocks, list) else [blocks]:
        for label, body in block.items() if isinstance(block, dict) else ():
            if label in calls or not isinstance(body, dict):
                calls[label] = None
                continue
            calls[label] = {
                name: value
                if isinstance(value, str) and "${" not in value and "%{" not in value
                else None
                for name, value in body.items()
                if name in ("source", "version")
            }
    return calls


def directory_module_calls(directory: Path) -> dict[str, dict[str, str | None]]:
    """Merge a directory's calls in Terraform's order: primary files, then override files."""
    merged: dict[str, dict[str, str | None] | None] = {}
    groups: tuple[list[Path], list[Path]] = ([], [])
    for path in sorted(directory.iterdir()):
        name = path.name
        if path.is_file() and not name.startswith(".") and name.endswith((".tf", ".tf.json")):
            stem = name.removesuffix(".json").removesuffix(".tf")
            groups[stem == "override" or stem.endswith("_override")].append(path)
    for override, paths in enumerate(groups):
        for path in paths:
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    continue
                text = path.read_text(encoding="utf-8")
                found = (_json_module_calls if path.name.endswith(".json") else _hcl_module_calls)(
                    text
                )
            except (OSError, UnicodeError, ValueError, RecursionError):
                # Terraform reports what this reader cannot; it only loses offline records.
                continue
            for label, arguments in found.items():
                base = merged.get(label)
                if not override:
                    merged[label] = None if label in merged else arguments
                elif base is not None and arguments is not None:
                    merged[label] = {**base, **arguments}
                else:
                    merged[label] = None
    return {label: arguments for label, arguments in merged.items() if arguments is not None}


def module_records(
    root: Path, project: Path, packages: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Record baked registry packages for the authored calls whose source they match.

    Terraform still compares each record's source and version with the configuration and
    falls back to the disabled registry on any mismatch, so a missed call stays incomplete.
    """
    catalog = {package["source"].casefold(): package for package in packages}
    names = {package["name"] for package in packages}
    records = [{"Key": "", "Source": "", "Dir": "."}]

    def visit(directory: Path, recorded: str, prefix: str, depth: int) -> None:
        for label, arguments in sorted(directory_module_calls(directory).items()):
            source = arguments.get("source")
            if source is None or not _LABEL.fullmatch(label) or depth > 32:
                continue
            key = f"{prefix}.{label}" if prefix else label
            if source.startswith(("./", "../")):
                clean = posixpath.normpath(source)
                source = clean if clean.startswith("../") else "./" + clean
                child = posixpath.normpath(posixpath.join(recorded, source))
                resolved = (root / child).resolve()
                if (
                    len(records) < MAX_RECORDS
                    and resolved.is_dir()
                    and resolved.is_relative_to(project)
                ):
                    records.append({"Key": key, "Source": source, "Dir": child})
                    visit(resolved, child, key, depth + 1)
                continue
            match = _REGISTRY_SOURCE.fullmatch(source)
            if match is None or (match.group(1) or REGISTRY_HOST).lower() != REGISTRY_HOST:
                continue
            address = f"{REGISTRY_HOST}/{match.group(2)}"
            package = catalog.get(address.casefold())
            if package is None or len(records) + len(package["inventory"]) >= MAX_RECORDS:
                continue
            base = INSTALL.as_posix() + "/registry"
            records.append(
                {
                    "Key": key,
                    "Source": address,
                    "Version": package["version"],
                    "Dir": f"{base}/{package['name']}",
                }
            )
            for item in package["inventory"]:
                if item["package"] not in names:
                    raise ValueError("inventory names an unbaked package")
                record = {
                    "Key": f"{key}.{item['key']}",
                    "Source": item["source"],
                    "Dir": posixpath.normpath(f"{base}/{item['package']}/{item['dir']}"),
                }
                if "version" in item:
                    record["Version"] = item["version"]
                records.append(record)

    visit(root, ".", "", 0)
    return records


def execute(engine: str, root_module: str, timeout: float) -> dict[str, Any]:
    """Initialize, validate, and check formatting using only fixed command arguments."""
    result: dict[str, Any] = {
        "protocol": 1,
        "engine": engine,
        "version": "0.0.0",
        "phases": {},
        "error": None,
    }
    try:
        if (
            engine not in ("terraform", "opentofu")
            or not math.isfinite(timeout)
            or not 0 < timeout <= 600
        ):
            raise ValueError("unsupported request")
        metadata = json.loads((INSTALL / "engine.json").read_text())
        if metadata["engine"] != engine:
            raise ValueError("wrong image engine")
        result["version"] = metadata["version"]
        binary = Path("/usr/local/bin") / ("terraform" if engine == "terraform" else "tofu")
        with binary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["binary_sha256"]:
                raise ValueError("engine digest mismatch")
        call = Path.cwd()
        project = call / "project"
        root = (project / root_module).resolve(strict=True)
        if not root.is_relative_to(project.resolve(strict=True)) or not root.is_dir():
            raise ValueError("root outside project")
        private = call / ".runner"
        for name in ("home", "tmp", "data"):
            (private / name).mkdir(parents=True, exist_ok=False)
        supervisor = Supervisor(timeout, clean_environment(private))
        version = supervisor.execute_phase([str(binary), "version", "-json"], private)
        if (
            version["exit_code"] != 0
            or json.loads(version["stdout"])["terraform_version"] != metadata["version"]
        ):
            raise ValueError("engine version mismatch")
        receipt = INSTALL / "dependencies.json"
        packages = (
            json.loads(receipt.read_text()).get("registry_modules", []) if receipt.is_file() else []
        )
        if packages:
            modules = private / "data" / "modules"
            modules.mkdir()
            records = module_records(root, project.resolve(strict=True), packages)
            (modules / "modules.json").write_text(json.dumps({"Modules": records}))
        lock = root / ".terraform.lock.hcl"
        original_lock = lock.read_bytes() if lock.is_file() else None
        init = [str(binary), "init", "-backend=false", "-input=false", "-no-color"]
        if original_lock is not None:
            init.append("-lockfile=readonly")
        phases = result["phases"]
        phases["init"] = supervisor.execute_phase(init, root)
        if original_lock is not None and lock.read_bytes() != original_lock:
            raise ValueError("supplied lock changed")
        if phases["init"]["exit_code"] == 0:
            phases["validate"] = supervisor.execute_phase([str(binary), "validate", "-json"], root)
            phases["fmt"] = supervisor.execute_phase(
                [str(binary), "fmt", "-check", "-recursive", "-no-color"], project
            )
    except Exception:
        # The host renders this as incomplete regardless of any completed phase's verdict.
        result["error"] = "launcher could not complete the bounded execution"
    return result


def main() -> None:
    """Emit exactly one bounded protocol object; never consume model-authored flags."""
    if len(sys.argv) != 4:
        raise SystemExit(2)
    print(json.dumps(execute(sys.argv[1], sys.argv[2], float(sys.argv[3])), ensure_ascii=True))


if __name__ == "__main__":
    main()
