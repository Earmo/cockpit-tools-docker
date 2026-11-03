"""Opt-in checks against a disposable, explicitly labelled remote Docker container."""
import json
import os
import subprocess
import time
import unittest


@unittest.skipUnless(os.environ.get("COCKPIT_UPDATER_TEST_CONTAINER"), "仅用于远程隔离容器")
class UpdaterRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = os.environ["COCKPIT_UPDATER_TEST_CONTAINER"]
        info = json.loads(subprocess.check_output(["docker", "inspect", cls.name]))[0]
        if (info["Config"].get("Labels") or {}).get("codex.test") != "updater-services":
            raise RuntimeError("Refusing to test a container without the disposable test label")
        cls.environment = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)

    def output(self, *command):
        return subprocess.check_output(["docker", "exec", self.name, *command], text=True)

    def test_packagekit_connection_has_install_capability(self):
        result = subprocess.run(["docker", "exec", "-u", "1000", self.name, "pkcon", "get-roles"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertIn("install-files", result.stdout)
        self.assertNotIn("Failed to contact PackageKit", result.stdout)

    def test_services_and_nonlogin_root(self):
        for bus_name in ["org.freedesktop.DBus", "org.freedesktop.PolicyKit1", "org.freedesktop.PackageKit"]:
            subprocess.run(["docker", "exec", self.name, "/usr/local/bin/cockpit-updater-service", "ready", bus_name], check=True, capture_output=True)
        self.assertTrue(self.output("getent", "passwd", "root").strip().endswith("/nologin"))
        self.assertIn("sudo", self.output("id", "-nG", "root").split())

    def test_authorization_requires_password_without_installing(self):
        before = self.output("dpkg-query", "-W", "-f=${Version}", "cockpit-tools")
        parent = self.output("pgrep", "-x", "cockpit-tools").strip()
        window = self.output("env", "DISPLAY=:0", "xdotool", "search", "--name", "^Cockpit Tools - Update Authorization$").strip().splitlines()[0]
        for accepted, password in [(False, "incorrect-test-password"), (True, self.environment["COCKPIT_WEB_PASSWORD"])]:
            with self.subTest(accepted=accepted):
                process = subprocess.Popen(["docker", "exec", "-u", "1000", self.name, "pkcheck", "--action-id", "org.freedesktop.packagekit.package-install-untrusted", "--process", parent, "--allow-user-interaction"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    time.sleep(2)
                    self.assertIsNone(process.poll(), "Authorization must wait for the user")
                    active = self.output("env", "DISPLAY=:0", "xdotool", "getactivewindow").strip()
                    self.assertEqual(active, window, "The real authentication prompt must be visible automatically")
                    subprocess.run(["docker", "exec", "-i", "-e", "DISPLAY=:0", self.name, "xdotool", "type", "--clearmodifiers", "--file", "-"], input=password.encode(), check=True)
                    subprocess.run(["docker", "exec", "-e", "DISPLAY=:0", self.name, "xdotool", "key", "Return"], check=True)
                    process.communicate(timeout=20)
                    self.assertEqual(process.returncode == 0, accepted)
                    time.sleep(0.5)
                    active = self.output("env", "DISPLAY=:0", "xdotool", "getactivewindow").strip()
                    self.assertNotEqual(active, window, "The terminal must minimize after authentication")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
        self.assertEqual(self.output("dpkg-query", "-W", "-f=${Version}", "cockpit-tools"), before)

    def test_agent_is_bound_to_the_application_not_its_launcher(self):
        application = self.output("pgrep", "-x", "cockpit-tools").strip()
        launcher = self.output("pgrep", "-f", "^bash /startapp.sh$").strip()
        self.assertNotEqual(application, launcher)
        agent = self.output("pgrep", "-x", "pkttyagent").strip()
        command = self.output("cat", "/proc/" + agent + "/cmdline").split("\0")
        self.assertEqual(command[command.index("--process") + 1], application)


if __name__ == "__main__":
    unittest.main()
