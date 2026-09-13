"""Diagnostic messages use the caller's path display policy regardless of rule id."""

import pytest

from maf_sandbox_bicep import format_diagnostics


def _render(message, *, prefix="/work/call", rename=None, rule="BCP091"):
    return format_diagnostics(
        [
            {
                "rule": rule,
                "level": "error",
                "message": message,
                "locations": [{"file": f"file://{prefix}/main.bicep", "line": 1}],
            }
        ],
        "build(main.bicep)",
        strip_prefix=prefix,
        rename=rename,
    )


@pytest.mark.parametrize("rule", ["BCP091", "BCP192", "future-rule"])
@pytest.mark.parametrize("name", ["absent.bicep", "absent.txt", "directory"])
def test_missing_file_messages_are_identical_across_calls(rule, name):
    rendered = []
    for call in ("004d8fc49aa745d99aa4c354aa492692", "fc4e2fec85074b019c5538b54cc93658"):
        prefix = f"/maf-sandbox/work/{call}"
        rendered.append(
            _render(
                f"An error occurred reading file. Could not find file '{prefix}/{name}'.",
                prefix=prefix,
                rule=rule,
            )
        )
    assert rendered[0] == rendered[1]
    assert "/maf-sandbox/work/" not in rendered[0]
    assert (
        f"@ main.bicep:1: An error occurred reading file. Could not find file '{name}'."
        in rendered[0]
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Cannot read '/work/call/secret.bicep'.", "Cannot read 'the value at files[0]'."),
        ('Cannot read "file:///work/call/secret.bicep".', 'Cannot read "the value at files[0]".'),
        ("Cannot read `secret.bicep`.", "Cannot read `the value at files[0]`."),
        ("Cannot read secret.bicep.", "Cannot read the value at files[0]."),
        (
            "Paths: (/work/call/secret.bicep), [secret.bicep];",
            "Paths: (the value at files[0]), [the value at files[0]];",
        ),
        ("Cannot read '/vendor/secret.bicep'.", "Cannot read 'an unidentified file'."),
        ("Cannot read 'dir/secret.bicep'.", "Cannot read 'dir/secret.bicep'."),
        ("Cannot read '/work/call/dir/secret.bicep'.", "Cannot read 'dir/secret.bicep'."),
        ("Cannot read '/work/call/other.txt'.", "Cannot read 'other.txt'."),
        ("Cannot read '/vendor/other.txt'.", "Cannot read '/vendor/other.txt'."),
        ("Cannot read '/work/call-other/other.txt'.", "Cannot read '/work/call-other/other.txt'."),
        (
            "secret.bicep.bak mysecret.bicep secretXbicep",
            "secret.bicep.bak mysecret.bicep secretXbicep",
        ),
        ("See https://example.test/secret.bicep", "See https://example.test/secret.bicep"),
        (
            "Restore br:example.test/module/secret.bicep",
            "Restore br:example.test/module/secret.bicep",
        ),
        (
            "Use '/work/call/secret.bicep' with /work/call/other.txt.",
            "Use 'the value at files[0]' with other.txt.",
        ),
        ("'Cannot read /work/call/secret.bicep.'", "'Cannot read the value at files[0].'"),
    ],
)
def test_message_paths_follow_the_location_rename_rules(message, expected):
    out = _render(
        message,
        rename={"secret.bicep": "the value at files[0]", "dir/secret.bicep": "dir/secret.bicep"},
    )
    assert out.split("@ main.bicep:1: ", 1)[1] == expected


@pytest.mark.parametrize("uri", ["/work/call/secret.bicep", "file:///work/call/secret.bicep"])
def test_an_absolute_rename_works_without_a_matching_prefix(uri):
    out = _render(
        f"Cannot read '{uri}'.",
        prefix=None,
        rename={"/work/call/secret.bicep": "the value at files[0]"},
    )
    assert "Cannot read 'the value at files[0]'." in out
    assert "secret.bicep" not in out


@pytest.mark.parametrize("base", ["/engine/private", "C:/runtime/temp"])
@pytest.mark.parametrize("scheme", ["", "file://"])
def test_a_relative_call_directory_strips_the_backend_base(base, scheme):
    out = _render(f"Cannot read '{scheme}{base}/call-unique/absent.txt'.", prefix="call-unique")
    assert "Cannot read 'absent.txt'." in out
    assert base not in out


def test_native_windows_message_paths_use_the_relative_call_directory():
    out = _render(
        r"Cannot read 'C:\runtime\temp\call-unique\secret.bicep'.",
        prefix="call-unique",
        rename={"secret.bicep": "the value at files[0]"},
    )
    assert "Cannot read 'the value at files[0]'." in out


def test_quoted_paths_can_contain_spaces_and_prefix_metacharacters():
    out = _render(
        'Cannot read "file:///work/call[1]+/dir/a file.txt".',
        prefix="/work/call[1]+/",
        rename={"dir/a file.txt": "the value at files[0]"},
    )
    assert 'Cannot read "the value at files[0]".' in out


def test_renames_are_not_applied_recursively_to_replacement_text():
    out = _render(
        "Read 'first.bicep' and 'second.bicep'.",
        rename={"first.bicep": "second.bicep", "second.bicep": "hidden"},
    )
    assert "Read 'second.bicep' and 'hidden'." in out


def test_without_a_display_policy_the_message_is_unchanged():
    message = "Cannot read 'file:///work/call/absent.txt'."
    out = _render(message, prefix=None)
    assert out.endswith(message)


def test_unknown_external_file_uris_are_preserved():
    message = "Cannot read 'file:///vendor/other.bicep'."
    out = _render(message, rename={"main.bicep": "main.bicep"})
    assert out.endswith(message)
