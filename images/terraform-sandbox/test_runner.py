"""Linux launcher regressions, run over stdin in the built image without host mounts."""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("runner", "/opt/maf-terraform/runner.py")
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class LauncherTests(unittest.TestCase):
    """Exercise actual subprocess pipes, environment, locks, and engine commands."""

    def test_environment_is_built_without_ambient_values(self):
        """No inherited TF flags, credentials, logging, or variables reach a child."""
        os.environ.update(TF_CLI_ARGS="-help", AWS_ACCESS_KEY_ID="sentinel", TF_VAR_x="sentinel")
        with tempfile.TemporaryDirectory() as directory:
            supervisor = runner.Supervisor(2, runner.clean_environment(Path(directory)))
            phase = supervisor.execute_phase(
                [sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                Path(directory),
            )
            environment = json.loads(phase["stdout"])
            self.assertNotIn("TF_CLI_ARGS", environment)
            self.assertNotIn("AWS_ACCESS_KEY_ID", environment)
            self.assertNotIn("TF_VAR_x", environment)
            self.assertEqual(environment["TF_CLI_CONFIG_FILE"], "/opt/maf-terraform/terraform.rc")

    def test_both_pipes_share_the_output_bound(self):
        """Simultaneous noisy streams cannot block or exceed retained output allowance."""
        with tempfile.TemporaryDirectory() as directory:
            supervisor = runner.Supervisor(2, runner.clean_environment(Path(directory)))
            with self.assertRaisesRegex(RuntimeError, "output limit"):
                supervisor.execute_phase(
                    [
                        sys.executable,
                        "-c",
                        "import os\nwhile True: os.write(1,b'x'*4096); os.write(2,b'y'*4096)",
                    ],
                    Path(directory),
                )

    def test_deadline_covers_descendants_holding_pipes(self):
        """A parent exit does not turn an inherited pipe into an unbounded wait."""
        with tempfile.TemporaryDirectory() as directory:
            supervisor = runner.Supervisor(0.25, runner.clean_environment(Path(directory)))
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                supervisor.execute_phase(
                    [sys.executable, "-c", "import os,time\nif os.fork()==0: time.sleep(10)"],
                    Path(directory),
                )
            self.assertLess(time.monotonic() - start, 2)

    def test_deadline_is_shared_across_commands(self):
        """Later phases receive only the time left by earlier phases."""
        with tempfile.TemporaryDirectory() as directory:
            supervisor = runner.Supervisor(0.35, runner.clean_environment(Path(directory)))
            command = [sys.executable, "-c", "import time; time.sleep(0.2)"]
            supervisor.execute_phase(command, Path(directory))
            with self.assertRaises(TimeoutError):
                supervisor.execute_phase(command, Path(directory))

    def test_supplied_lock_readonly_and_sources_unchanged(self):
        """A generated lock works on the next call; a wrong-registry lock cannot be repaired."""
        metadata = json.loads((runner.INSTALL / "engine.json").read_text())
        engine = metadata["engine"]
        source = """terraform {
  required_providers {
    random = { source = "hashicorp/random", version = "3.7.2" }
  }
}
resource "random_integer" "r" {
  min = 1
  max = 10
}
"""
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                lock = None
                for number in range(3):
                    call = base / str(number)
                    project = call / "project"
                    project.mkdir(parents=True)
                    (project / "main.tf").write_text(source)
                    if number == 2:
                        self.assertIsNotNone(lock)
                        lock = lock.replace("registry.terraform.io", "wrong.invalid").replace(
                            "registry.opentofu.org", "wrong.invalid"
                        )
                    if lock is not None:
                        (project / ".terraform.lock.hcl").write_text(lock)
                    os.chdir(call)
                    result = runner.execute(engine, ".", 10)
                    self.assertIsNone(result["error"], result)
                    if number == 2:
                        self.assertNotEqual(result["phases"]["init"]["exit_code"], 0)
                        self.assertNotIn("validate", result["phases"])
                    else:
                        self.assertEqual(result["phases"]["validate"]["exit_code"], 0, result)
                    current_lock = (project / ".terraform.lock.hcl").read_text()
                    if lock is not None:
                        self.assertEqual(lock, current_lock)
                    lock = current_lock
                    self.assertEqual((project / "main.tf").read_text(), source)
                    self.assertFalse(list(project.rglob("*.tfstate*")))
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
