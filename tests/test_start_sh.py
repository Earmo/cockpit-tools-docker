import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "需要 Linux Bash；不调用 Docker")
class StartScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cockpit-start-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.script = root / "start.sh"
        shutil.copyfile(ROOT / "start.sh", self.script)
        python = root / "python3"
        python.write_text("#!/bin/sh\nprintf 'DEPLOYER_CALLED\\n'\nexit 17\n")
        python.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get("PATH", ""))

    def run_shell(self, command):
        return subprocess.run(["bash", "-c", command, "bash", str(self.script)], env=self.environment, capture_output=True, text=True, timeout=10)

    def test_source_preserves_shell_and_options_without_deploying(self):
        result = self.run_shell('before=$-; . "$1"; test "$-" = "$before" || exit 99; printf "SHELL_SURVIVED\\n"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("SHELL_SURVIVED", result.stdout)
        self.assertNotIn("DEPLOYER_CALLED", result.stdout)
        self.assertIn("bash start.sh", result.stderr)

    def test_source_preserves_shell_with_errexit_enabled(self):
        result = self.run_shell('set -e; . "$1"; printf "SHELL_SURVIVED\\n"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("SHELL_SURVIVED", result.stdout)
        self.assertNotIn("DEPLOYER_CALLED", result.stdout)

    def test_normal_execution_propagates_deployer_failure(self):
        result = subprocess.run(["bash", str(self.script)], env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17)
        self.assertIn("DEPLOYER_CALLED", result.stdout)


if __name__ == "__main__":
    unittest.main()
