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
_SUBDIR = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
_REGISTRY_SOURCE = re.compile(rf"(?:([^/]+)/)?({_PART}/{_PART}/[0-9a-z]{{1,64}})(?://({_SUBDIR}))?")
_RELEASE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_CONSTRAINT = re.compile(
    r"\s*(=|!=|>=|<=|>|<|~>)?\s*((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2})\s*"
)
_CLOSING = {"{": "}", "[": "]", "(": ")"}


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
        elif char in "{}[](),:" or (char == "=" and not text.startswith(("==", "=>"), index)):
            tokens.append((char, None))
            index += 1
        else:
            tokens.append(("other", None))
            index += 2 if text.startswith(("==", "=>", "!=", ">=", "<="), index) else 1
    return tokens


# An attribute's value is its literal string, an object of such values, or None.
HclValue = str | dict[str, Any] | None
# A block item carries its labels and body; an attribute item has no labels.
HclItem = tuple[str, list[str | None] | None, Any]


def _hcl_expression(
    tokens: list[tuple[str, str | None]], index: int, stops: tuple[str, ...]
) -> tuple[HclValue, int]:
    """Skip one expression to a line end outside brackets; keep only literal shapes."""
    start, closers = index, []
    while index < len(tokens):
        kind = tokens[index][0]
        if not closers and (kind in stops or kind in ("newline", "}")):
            break
        if kind in _CLOSING:
            closers.append(_CLOSING[kind])
        elif kind in ("}", "]", ")") and (not closers or closers.pop() != kind):
            raise ValueError("unbalanced brackets")
        index += 1
    span = tokens[start:index]
    if closers or not span:
        raise ValueError("incomplete expression")
    if len(span) == 1 and span[0][0] == "string":
        return span[0][1], index
    if span[0][0] == "{":
        depth = 0
        for position, (kind, _) in enumerate(span):
            depth += 1 if kind in _CLOSING else -1 if kind in ("}", "]", ")") else 0
            if depth == 0:
                return (_hcl_object(span[1:-1]) if position == len(span) - 1 else None), index
    return None, index


def _hcl_object(tokens: list[tuple[str, str | None]]) -> dict[str, Any] | None:
    """Read an object constructor whose keys are all literal; None for anything else."""
    result: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        kind, key = tokens[index]
        if kind in ("newline", ","):
            index += 1
            continue
        if (
            kind not in ("word", "string")
            or key is None
            or index + 1 == len(tokens)
            or tokens[index + 1][0] not in ("=", ":")
        ):
            return None
        value, index = _hcl_expression(tokens, index + 2, (",",))
        result[key] = None if key in result else value
    return result


def _hcl_body(
    tokens: list[tuple[str, str | None]], index: int, nested: bool
) -> tuple[list[HclItem], int]:
    """Read attributes and blocks up to the closing brace, or to the end of the file."""
    items: list[HclItem] = []
    while True:
        while index < len(tokens) and tokens[index][0] == "newline":
            index += 1
        if index == len(tokens):
            if nested:
                raise ValueError("unterminated block")
            return items, index
        kind, name = tokens[index]
        if kind == "}" and nested:
            return items, index + 1
        if kind != "word" or name is None:
            raise ValueError("unexpected token")
        index += 1
        if index < len(tokens) and tokens[index][0] == "=":
            value, index = _hcl_expression(tokens, index + 1, ())
            items.append((name, None, value))
            continue
        labels: list[str | None] = []
        while index < len(tokens) and tokens[index][0] in ("string", "word"):
            labels.append(tokens[index][1])
            index += 1
        if index == len(tokens) or tokens[index][0] != "{":
            raise ValueError("unexpected token")
        body, index = _hcl_body(tokens, index + 1, True)
        items.append((name, labels, body))


def parse_hcl(text: str) -> list[HclItem]:
    """Parse native syntax into blocks and attributes; raise on what this reader cannot follow."""
    return _hcl_body(_hcl_tokens(text), 0, False)[0]


def _hcl_module_calls(text: str) -> dict[str, dict[str, str | None] | None]:
    """Find top-level module blocks with their literal source and version arguments."""
    calls: dict[str, dict[str, str | None] | None] = {}
    for kind, labels, body in parse_hcl(text):
        if kind != "module" or labels is None or len(labels) != 1 or labels[0] is None:
            continue
        arguments: dict[str, str | None] = {}
        for name, inner, value in body:
            if inner is None and name in ("source", "version"):
                arguments[name] = None if name in arguments or not isinstance(value, str) else value
        calls[labels[0]] = None if labels[0] in calls else arguments
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


def is_override(name: str) -> bool:
    """Terraform merges these files into a directory's primary configuration."""
    stem = name.removesuffix(".json").removesuffix(".tf")
    return stem == "override" or stem.endswith("_override")


def merge_module_calls(
    found: list[tuple[str, dict[str, dict[str, str | None] | None]]],
) -> dict[str, dict[str, str | None] | None]:
    """Merge per-file calls in Terraform's order: primary files, then override files.

    A label declared twice, or overridden without a base, maps to None.
    """
    merged: dict[str, dict[str, str | None] | None] = {}
    for override in (False, True):
        for name, calls in sorted(found, key=lambda item: item[0]):
            if is_override(name) != override:
                continue
            for label, arguments in calls.items():
                base = merged.get(label)
                if not override:
                    merged[label] = None if label in merged else arguments
                elif base is not None and arguments is not None:
                    merged[label] = {**base, **arguments}
                else:
                    merged[label] = None
    return merged


def directory_module_calls(directory: Path) -> dict[str, dict[str, str | None]]:
    """Read and merge a directory's module calls, dropping files this reader cannot follow."""
    found: list[tuple[str, dict[str, dict[str, str | None] | None]]] = []
    for path in sorted(directory.iterdir()):
        name = path.name
        if not (path.is_file() and not name.startswith(".") and name.endswith((".tf", ".tf.json"))):
            continue
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
            found.append(
                (name, (_json_module_calls if name.endswith(".json") else _hcl_module_calls)(text))
            )
        except (OSError, UnicodeError, ValueError, RecursionError):
            # Terraform reports what this reader cannot; it only loses offline records.
            continue
    merged = merge_module_calls(found)
    return {label: arguments for label, arguments in merged.items() if arguments is not None}


def satisfies(version: str, constraint: str) -> bool:
    """Check a release against a registry constraint; raise ValueError on refused syntax.

    A one-segment `~>` is refused because the module and provider libraries read it differently.
    """
    if not _RELEASE.fullmatch(version):
        raise ValueError("not a release version")
    target = tuple(int(part) for part in version.split("."))
    for term in constraint.split(","):
        match = _CONSTRAINT.fullmatch(term)
        if match is None:
            raise ValueError("unsupported constraint")
        operator, given = match.group(1) or "=", [int(part) for part in match.group(2).split(".")]
        if operator == "~>" and len(given) == 1:
            raise ValueError("unsupported constraint")
        bound = tuple(given + [0] * (3 - len(given)))
        allowed = {
            "=": target == bound,
            "!=": target != bound,
            ">": target > bound,
            ">=": target >= bound,
            "<": target < bound,
            "<=": target <= bound,
            "~>": target >= bound and target[: len(given) - 1] == bound[: len(given) - 1],
        }[operator]
        if not allowed:
            return False
    return True


def _newest_admitted(
    packages: list[dict[str, Any]], arguments: dict[str, str | None]
) -> dict[str, Any] | None:
    """Pick the newest baked version the call's constraint admits, as Terraform would."""
    constraint = arguments.get("version", "")
    if constraint is None:
        return None
    try:
        admitted = [
            item for item in packages if not constraint or satisfies(item["version"], constraint)
        ]
    except ValueError:
        return None
    return max(
        admitted,
        key=lambda item: tuple(int(part) for part in item["version"].split(".")),
        default=None,
    )


def module_records(
    root: Path, project: Path, packages: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Record baked registry packages for the authored calls whose source they match.

    Terraform still compares each record's source and version with the configuration and
    falls back to the disabled registry on any mismatch, so a missed call stays incomplete.
    """
    catalog: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        catalog.setdefault(package["source"].casefold(), []).append(package)
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
            subdir = match.group(3)
            if subdir is not None and any(part in (".", "..") for part in subdir.split("/")):
                continue
            address = f"{REGISTRY_HOST}/{match.group(2)}"
            package = _newest_admitted(catalog.get(address.casefold(), []), arguments)
            inventory = None if package is None else package["inventories"].get(subdir or ".")
            if package is None or inventory is None or len(records) + len(inventory) >= MAX_RECORDS:
                continue
            base = INSTALL.as_posix() + "/registry"
            records.append(
                {
                    "Key": key,
                    "Source": address + (f"//{subdir}" if subdir else ""),
                    "Version": package["version"],
                    "Dir": f"{base}/{package['name']}" + (f"/{subdir}" if subdir else ""),
                }
            )
            for item in inventory:
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


def refuse_copied_providers(data_dir: Path) -> None:
    """A prepared mirror serves providers by symlink; Terraform copies when it cannot.

    It reports success either way, so every entry below the call's providers directory
    must be a link into the image mirror, never a regular file.
    """
    providers = data_dir / "providers"
    if not providers.is_dir():
        return
    for directory, names, files in os.walk(providers, followlinks=False):
        for name in names + files:
            entry = Path(directory) / name
            if entry.is_symlink():
                if not entry.resolve().is_relative_to(INSTALL / "mirror"):
                    raise ValueError("provider linked outside the image mirror")
            elif entry.is_file():
                raise ValueError("provider copied into the call")


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
            if metadata["profile"] == "prepared":
                # Only the prepared mirror serves providers unpacked; the ZIP profiles copy.
                refuse_copied_providers(private / "data")
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
