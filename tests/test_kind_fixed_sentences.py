"""Every kind declares static result literals, including its refusal helpers."""

import ast
import importlib
import inspect
from dataclasses import replace
from pathlib import Path

import pytest
from maf_sandbox import (
    CallerContext,
    Capability,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InMemoryStore, InProcessSandboxBackend

_KINDS = (
    "azure_pricing",
    "bicep",
    "terraform",
    "codeact",
    "drawio",
    "shell",
    "document",
    "antivirus",
)

_RESULT_HELPERS = {
    "bicep": {"_listing_hint"},
    "codeact": {
        "_resolve_listed_files",
        "_listing_hint",
        "_read_listed_files",
        "_validated_output_names",
        "_collect",
        "_read_manifest",
        "_format_landed",
        "_format_result",
        "_format_withheld",
    },
}


def test_every_shipped_kind_has_a_catalogue_check():
    packages = Path(__file__).resolve().parents[1] / "packages"
    kinds = {
        path.parent.name.removeprefix("maf_sandbox_")
        for path in packages.glob("maf-sandbox-*/src/*/_tool.py")
        if "sandboxed_tool(" in path.read_text("utf-8")
    }
    assert kinds == set(_KINDS)


def _literal_text(node: ast.AST, constants: dict[str, str] | None = None) -> set[str]:
    constants = constants or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value} if node.value.strip() else set()
    if isinstance(node, ast.Name) and node.id in constants:
        return {constants[node.id]}
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                values = _literal_text(part.value, constants)
                if len(values) != 1 or part.format_spec is not None:
                    return set()
                value = next(iter(values))
                parts.append(repr(value) if part.conversion == ord("r") else value)
            elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            else:
                return set()
        return {"".join(parts)}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_literal_text(item, constants) for item in node.elts))
    if isinstance(node, ast.IfExp):
        return _literal_text(node.body, constants) | _literal_text(node.orelse, constants)
    if isinstance(node, ast.BoolOp):
        return set().union(*(_literal_text(item, constants) for item in node.values))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # Tuple concatenation carries whole items; string concatenation may carry call data.
        if isinstance(node.left, ast.Tuple) or isinstance(node.right, ast.Tuple):
            return _literal_text(node.left, constants) | _literal_text(node.right, constants)
    return set()


def _helper_literals(source: str, names: set[str], *, exceptions: bool = False) -> set[str]:
    tree = ast.parse(source)
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id.isupper()
    }
    found: set[str] = set()
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name not in names:
            continue
        for node in ast.walk(function):
            if not exceptions and isinstance(node, ast.Return) and node.value is not None:
                found |= _literal_text(node.value, constants)
            if exceptions and isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ValueError":
                    found |= _literal_text(node.exc.args[0], constants)
    return found


def _result_literals(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in {"_incomplete", "_stopped"} and node.args:
                found |= _literal_text(node.args[0])
            if name in {"SandboxResult", "ReportOutcome", "Outcome"}:
                for keyword in node.keywords:
                    if keyword.arg in {"trusted_output", "output", "reason", "summary"}:
                        found |= _literal_text(keyword.value)
                if name in {"SandboxResult", "Outcome"} and len(node.args) > 2:
                    found |= _literal_text(node.args[2])
                if name == "ReportOutcome" and node.args:
                    found |= _literal_text(node.args[0])
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"notes", "summaries", "lines"}
                and node.func.attr in {"append", "add"}
                and node.args
            ):
                found |= _literal_text(node.args[0])
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "notes" for target in node.targets
        ):
            found |= _literal_text(node.value)
    return found


@pytest.mark.parametrize("kind", _KINDS)
def test_every_static_result_literal_is_declared(kind):
    module = importlib.import_module(f"maf_sandbox_{kind}._tool")
    directory = Path(inspect.getfile(module)).parent
    declared = set(module._FIXED_SENTENCES)
    for filename in ("_tool.py", "_report.py", "_regions.py"):
        path = directory / filename
        if path.exists():
            missing = _result_literals(path.read_text("utf-8")) - declared
            assert not missing, (
                f"{kind}/{filename}: undeclared fixed result text: {sorted(missing)}"
            )


@pytest.mark.parametrize("kind", _RESULT_HELPERS)
def test_static_result_helper_returns_are_declared(kind):
    module = importlib.import_module(f"maf_sandbox_{kind}._tool")
    expected = _helper_literals(inspect.getsource(module), _RESULT_HELPERS[kind])
    assert expected
    assert not (expected - set(module._FIXED_SENTENCES))


@pytest.mark.parametrize("kind", ["shell", "terraform"])
def test_manifest_refusals_are_declared_for_each_operation(kind):
    paths = importlib.import_module(f"maf_sandbox_{kind}._paths")
    module = importlib.import_module(f"maf_sandbox_{kind}._tool")
    reasons = _helper_literals(
        inspect.getsource(paths), {"normalize", "resolve_manifest"}, exceptions=True
    )
    prefixes = (
        ("Validation incomplete: ",)
        if kind == "shell"
        else ("Validation INCOMPLETE: ", "Formatting INCOMPLETE: ")
    )
    assert reasons
    for prefix in prefixes:
        assert not ({prefix + reason for reason in reasons} - set(module._FIXED_SENTENCES))


@pytest.mark.parametrize("kind", _KINDS)
def test_each_attached_kind_exposes_its_declared_text(kind, tmp_path):
    module = importlib.import_module(f"maf_sandbox_{kind}._tool")
    backend = InProcessSandboxBackend(
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities
            | {Capability.EGRESS_METHODS, Capability.FILES_OUT},
            egress_method_tokens=None,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
    )
    arguments = {
        "router": SandboxRouter([backend], min_isolation=Isolation.CONTAINER),
        "file_store": InMemoryStore({}),
        "agent_id": "agent",
        "context": CallerContext(lambda: "scope", lambda: "thread", InMemoryStore.list),
        "sink": make_file_system_sink(tmp_path),
        "flavor": "bash",
        "formatting": True,
    }
    factory = getattr(module, f"make_{kind}_tools")
    selected = {
        key: value
        for key, value in arguments.items()
        if key in inspect.signature(factory).parameters
    }
    for tool in factory(**selected):
        declared = tool.additional_properties["fixed_sentences"]
        assert isinstance(declared, tuple)
        assert {COMPLETED_TEXT, NOT_COMPLETED_TEXT, *module._FIXED_SENTENCES} <= set(declared)


def test_literal_check_detects_new_messages_but_excludes_interpolated_data():
    source = """
def body():
    notes = ("Truncated.",) if truncated else ()
    summaries.add("Fixed summary.")
    _incomplete("Refused.")
    _stopped(f"Refused {name}.")
    return SandboxResult(True, "done", ("New message.",), output=("Prefix: " + data,))
"""
    assert _result_literals(source) == {"Truncated.", "Fixed summary.", "Refused.", "New message."}


def test_helper_check_renders_only_module_constants():
    source = """
_FILENAME = "outputs.json"
_KEY = "outputs"
def helper(name):
    if missing:
        return f"No {_FILENAME}; expected {_KEY!r}."
    if empty:
        return "Nothing saved."
    return f"Missing {name}."
"""
    assert _helper_literals(source, {"helper"}) == {
        "No outputs.json; expected 'outputs'.",
        "Nothing saved.",
    }


@pytest.mark.parametrize("kind", _KINDS)
def test_all_kind_factories_pass_the_catalogue(kind):
    module = importlib.import_module(f"maf_sandbox_{kind}._tool")
    tree = ast.parse(inspect.getsource(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sandboxed_tool"
    ]
    assert calls
    assert all(any(keyword.arg == "fixed_sentences" for keyword in call.keywords) for call in calls)


def test_terraform_declares_both_static_operation_names():
    module = importlib.import_module("maf_sandbox_terraform._tool")
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if not isinstance(node, ast.JoinedStr):
            continue
        fields = [part for part in node.values if isinstance(part, ast.FormattedValue)]
        if len(fields) != 1 or not isinstance(fields[0].value, ast.Name):
            continue
        if fields[0].value.id != "operation":
            continue
        for operation in ("Validation", "Formatting"):
            text = "".join(
                str(part.value) if isinstance(part, ast.Constant) else operation
                for part in node.values
            )
            assert text in module._FIXED_SENTENCES
