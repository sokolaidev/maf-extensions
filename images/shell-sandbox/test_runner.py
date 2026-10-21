"""Offline qualification executed inside the built image with closed networking."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path


def analyze(flavor, sources, timeout: float = 30):
    """Exercise the installed launcher and ensure the source bundle is removed."""
    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory) / "request.json"
        bundle.write_text(json.dumps({"flavor": flavor, "files": sources}))
        result = subprocess.run(
            ["/usr/local/bin/python3", "-I", "/opt/maf-shell/runner.py", str(timeout)],
            cwd=directory,
            capture_output=True,
            timeout=timeout + 5,
            check=True,
        )
        assert not bundle.exists()
        assert not result.stderr, result.stderr
        return json.loads(result.stdout)


def source(text, path="main.sh", dialect="bash"):
    """Represent an input without creating a file an analyzer could import."""
    return {"path": path, "text": text, "dialect": dialect}


class Qualification(unittest.TestCase):
    """Real analyzers with inert supplied code and complete suppression accounting."""

    def test_supervisor_bounds_combined_output_and_descendant_lifetime(self):
        runner = runpy.run_path("/opt/maf-shell/runner.py")
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            environment = runner["clean_environment"](private)
            supervisor = runner["Supervisor"](5, environment, private)
            command = ["python3", "-c", "import sys; sys.stdout.write('x' * 600000)"]
            supervisor.execute(command)
            with self.assertRaisesRegex(ValueError, "output limit"):
                supervisor.execute(command)
            supervisor = runner["Supervisor"](0.2, environment, private)
            with self.assertRaises(TimeoutError):
                supervisor.execute(
                    [
                        "python3",
                        "-c",
                        "import subprocess; subprocess.Popen(['sleep', '100'])",
                    ]
                )

    def test_shell_dialects_and_unused_suppressions(self):
        report = analyze(
            "bash",
            [
                source("#!/bin/bash\n# shellcheck disable=SC2086,SC9999\necho $word\n"),
                source('#!/bin/sh\necho "$HOME"\n', "posix.sh", "sh"),
                source("#!/bin/dash\nread word\n", "dash.dash", "dash"),
            ],
        )
        self.assertIsNone(report["error"])
        bash, sh, dash = report["files"]
        self.assertTrue(all(item["parsed"] for item in report["files"]))
        self.assertEqual(len(bash["suppressions"]), 1)
        self.assertEqual(len(bash["suppressions"][0]["rules"]), 2)
        self.assertIn("SC2086", [item["rule"] for item in bash["suppressed_diagnostics"]])
        self.assertNotIn("SC2086", [item["rule"] for item in bash["diagnostics"]])
        self.assertFalse(sh["diagnostics"])
        self.assertIn("SC2162", [item["rule"] for item in dash["diagnostics"]])
        self.assertEqual(report["dependency_inventory_sha256"], hashlib.sha256(b"{}\n").hexdigest())

    def test_literals_are_not_suppression_declarations(self):
        report = analyze(
            "bash", [source("cat <<'EOF'\n# shellcheck disable=all\nEOF\necho $word\n")]
        )
        self.assertIsNone(report["error"])
        self.assertFalse(report["files"][0]["suppressions"])
        self.assertTrue(report["files"][0]["diagnostics"])

    def test_sources_are_confined_to_the_manifest(self):
        report = analyze(
            "bash",
            [
                source('. ./library.sh\necho "$word"\n'),
                source("word=hello\n", "library.sh"),
                source(". /etc/passwd\n", "outside.sh"),
            ],
        )
        self.assertIsNone(report["error"])
        self.assertNotIn("SC1091", [item["rule"] for item in report["files"][0]["diagnostics"]])
        self.assertIn("SC1091", [item["rule"] for item in report["files"][2]["diagnostics"]])

    def test_parse_errors_and_deadline_are_incomplete(self):
        report = analyze("bash", [source("if then\n")])
        self.assertFalse(report["files"][0]["parsed"])
        self.assertNotEqual(report["files"][0]["syntax"]["exit_code"], 0)
        report = analyze("pwsh", [source("function {", "main.ps1", "pwsh")])
        self.assertFalse(report["files"][0]["parsed"])
        report = analyze("pwsh", [source("Write-Host hello", "main.ps1", "pwsh")], 0.001)
        self.assertEqual(report["error"], "incomplete")

    def test_powershell_reports_unused_and_effective_suppressions(self):
        text = """[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost','')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingInvokeExpression','')]
param()
Write-Host 'hello'
"""
        report = analyze("pwsh", [source(text, "main.ps1", "pwsh")])
        self.assertIsNone(report["error"])
        item = report["files"][0]
        self.assertEqual(len(item["suppressions"]), 2)
        self.assertEqual(
            [item["rule"] for item in item["suppressed_diagnostics"]], ["PSAvoidUsingWriteHost"]
        )
        self.assertFalse(item["diagnostics"])

    def test_submitted_modules_cannot_be_imported(self):
        marker = Path(tempfile.gettempdir()) / "maf-shell-submitted-module-executed"
        marker.unlink(missing_ok=True)
        payload = (
            "[IO.File]::WriteAllText('/tmp/maf-shell-submitted-module-executed','executed')\n"
            "function Get-Probe { param($Name) $Name }"
        )
        report = analyze(
            "pwsh",
            [
                source("./Probe.psm1\\Get-Probe -Name hello", "main.ps1", "pwsh"),
                source(payload, "Probe.psm1", "pwsh"),
            ],
        )
        self.assertIsNone(report["error"])
        self.assertFalse(marker.exists())
        for text in [
            "using module ./Probe.psm1\nGet-Probe",
            "using assembly ./Probe.dll\nWrite-Output hello",
            (
                "dynamicparam { [IO.File]::WriteAllText("
                "'/tmp/maf-shell-submitted-module-executed','executed') }"
            ),
            "param([ValidateScript({[IO.File]::WriteAllText('/tmp/maf-shell-submitted-module-executed','executed')})]$Value)",
        ]:
            analyze(
                "pwsh", [source(text, "main.ps1", "pwsh"), source(payload, "Probe.psm1", "pwsh")]
            )
            self.assertFalse(marker.exists(), text)

    def test_shell_commands_are_never_executed(self):
        marker = Path(tempfile.gettempdir()) / "maf-shell-script-executed"
        marker.unlink(missing_ok=True)
        report = analyze("bash", [source("touch /tmp/maf-shell-script-executed\n")])
        self.assertIsNone(report["error"])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
