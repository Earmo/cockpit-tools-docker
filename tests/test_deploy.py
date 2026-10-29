import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy", ROOT / "deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def asset(arch="amd64", content=b"verified deb"):
    name = "Cockpit.Tools_1.3.75_" + arch + ".deb"
    return {"name": name, "digest": "sha256:" + hashlib.sha256(content).hexdigest(), "browser_download_url": "https://github.com/jlcodes99/cockpit-tools/releases/download/v1.3.75/" + name}


def release(arch="amd64"):
    return {"tag_name": "v1.3.75", "assets": [asset(arch)]}


def config(root):
    return {
        "name": "check",
        "x-cockpit-deploy": {"data_dir": "", "download_proxy": "", "download_prefix": "", "dollar_literal": "$"},
        "services": {"cockpit-vnc": {"container_name": "check", "environment": {"COCKPIT_WEB_USERNAME": "cockpit", "COCKPIT_WEB_PASSWORD": "password$ #quote", "USER_ID": "1000", "GROUP_ID": "1000", "HTTP_PROXY": ""}, "build": {"args": {"HTTP_PROXY": "", "UBUNTU_MIRROR": "http://mirrors.aliyun.com/ubuntu"}}, "volumes": [{"type": "volume", "source": "cockpit_config", "target": "/config"}]}},
        "volumes": {"cockpit_config": {"name": "check_cockpit_config"}},
    }


def existing(kind="volume", source="check_cockpit_config", project="check"):
    return {"Config": {"Labels": {"com.docker.compose.project": project, "com.docker.compose.service": "cockpit-vnc"}}, "Mounts": [{"Destination": "/config", "Type": kind, "Name": source, "Source": source}]}


class ArchitectureTests(unittest.TestCase):
    def test_aliases(self):
        for value, expected in [("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")]:
            with self.subTest(value=value):
                self.assertEqual(deploy.normalize_arch(value), expected)

    def test_unsupported(self):
        for value in ["armv7l", "i686", "riscv64", None]:
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.normalize_arch(value)


class ReleaseTests(unittest.TestCase):
    def test_selects_each_arch(self):
        data = {"assets": [asset("amd64"), asset("arm64"), {"name": "Cockpit.Tools_1.3.75_amd64.deb.sig"}]}
        for arch in ["amd64", "arm64"]:
            self.assertEqual(deploy.select_asset(data, arch)["name"], asset(arch)["name"])

    def test_missing_arch(self):
        with self.assertRaises(deploy.NotFound):
            deploy.select_asset(release(), "arm64")

    def test_rejects_ambiguous_arch(self):
        with self.assertRaises(deploy.DeployError):
            deploy.select_asset({"assets": [asset(), asset()]}, "amd64")

    def test_defaults_latest_endpoint(self):
        client = deploy.ReleaseClient()
        client.read = Mock(return_value=json.dumps(release()).encode())
        client.release("latest")
        client.read.assert_called_once_with(deploy.API + "/releases/latest")

    def test_version_prefix(self):
        for version in ["1.3.75", "v1.3.75"]:
            client = deploy.ReleaseClient()
            client.read = Mock(return_value=json.dumps(release()).encode())
            client.release(version)
            client.read.assert_called_once_with(deploy.API + "/releases/tags/v1.3.75")

    def test_missing_version_falls_back(self):
        client = Mock()
        client.release.side_effect = [deploy.NotFound(), release()]
        client.package.return_value = Path("cache.deb")
        with contextlib.redirect_stdout(io.StringIO()):
            result = deploy.resolve_package(client, "9.9.9", "amd64", Path("cache"))
        self.assertEqual(result[0]["tag_name"], "v1.3.75")
        self.assertEqual([call.args[0] for call in client.release.call_args_list], ["9.9.9", "latest"])

    def test_missing_arch_falls_back(self):
        client = Mock()
        client.release.side_effect = [release("amd64"), release("arm64")]
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.resolve_package(client, "1.0.0", "arm64", Path("cache"))
        self.assertEqual(client.release.call_count, 2)

    def test_deleted_asset_falls_back(self):
        client = Mock()
        client.release.return_value = release()
        client.package.side_effect = [deploy.NotFound(), Path("cached.deb")]
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.resolve_package(client, "1.3.75", "amd64", Path("cache"))
        self.assertEqual(client.release.call_args.args, ("latest",))

    def test_no_compatible_latest_fails(self):
        client = Mock()
        client.release.return_value = release("amd64")
        with self.assertRaises(deploy.DeployError):
            deploy.resolve_package(client, "latest", "arm64", Path("cache"))
        self.assertEqual(client.release.call_count, 1)

    def test_network_and_checksum_errors_do_not_fall_back(self):
        for error in [deploy.DeployError("HTTP 403"), deploy.DeployError("SHA256 mismatch")]:
            client = Mock()
            client.release.return_value = release()
            client.package.side_effect = error
            with self.subTest(error=error), self.assertRaises(deploy.DeployError):
                deploy.resolve_package(client, "1.3.75", "amd64", Path("cache"))
            self.assertEqual(client.release.call_count, 1)

    def test_digest_from_metadata(self):
        client = Mock()
        selected = asset()
        self.assertEqual(deploy.asset_digest(release(), selected, client), selected["digest"][7:])
        client.read.assert_not_called()

    def test_checksum_file_fallback(self):
        selected = asset()
        expected = selected.pop("digest")[7:]
        body = (expected + "  " + selected["name"] + "\n").encode()
        checksum = {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/checksum", "digest": "sha256:" + hashlib.sha256(body).hexdigest()}
        client = Mock()
        client.read.return_value = body
        self.assertEqual(deploy.asset_digest({"assets": [selected, checksum]}, selected, client), expected)

    def test_checksum_file_tampering_rejected(self):
        selected = asset()
        selected.pop("digest")
        client = Mock()
        client.read.return_value = b"tampered"
        checksum = {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/checksum", "digest": "sha256:" + "0" * 64}
        with self.assertRaises(deploy.DeployError):
            deploy.asset_digest({"assets": [checksum]}, selected, client)

    def test_no_checksum_fails(self):
        selected = asset()
        selected.pop("digest")
        with self.assertRaises(deploy.DeployError):
            deploy.asset_digest({"assets": []}, selected, Mock())


class DownloadTests(unittest.TestCase):
    def test_cached_package_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            selected = asset()
            digest = selected["digest"][7:]
            target = cache / (digest + ".deb")
            target.write_bytes(b"verified deb")
            client = deploy.ReleaseClient()
            client.open = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(client.package(selected, digest, cache), target)
            client.open.assert_not_called()

    def test_bad_cached_file_is_replaced_and_download_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            selected = asset()
            digest = selected["digest"][7:]
            target = cache / (digest + ".deb")
            target.write_bytes(b"corrupt")
            client = deploy.ReleaseClient(prefix="https://mirror.invalid/")
            client.open = Mock(return_value=io.BytesIO(b"verified deb"))
            with contextlib.redirect_stdout(io.StringIO()):
                client.package(selected, digest, cache)
            self.assertEqual(target.read_bytes(), b"verified deb")
            self.assertEqual(client.open.call_args.args[0], "https://mirror.invalid/" + selected["browser_download_url"])

    def test_bad_download_is_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = deploy.ReleaseClient()
            client.open = Mock(return_value=io.BytesIO(b"corrupt"))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeployError):
                client.package(asset(), asset()["digest"][7:], Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_token_is_not_sent_to_asset_mirror(self):
        client = deploy.ReleaseClient(token="private-token")
        client.opener = Mock()
        client.open("https://mirror.invalid/package")
        self.assertNotIn("Authorization", client.opener.open.call_args.args[0].headers)
        client.open(deploy.API + "/releases/latest")
        self.assertIn("Authorization", client.opener.open.call_args.args[0].headers)

    def test_403_is_not_treated_as_missing(self):
        client = deploy.ReleaseClient()
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError("https://example.invalid", 403, "limited", {}, None)
        with self.assertRaises(deploy.DeployError) as raised:
            client.open(deploy.API + "/releases/latest")
        self.assertNotIsInstance(raised.exception, deploy.NotFound)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_checksum_file_is_fetched_from_official_origin(self):
        client = deploy.ReleaseClient(prefix="https://untrusted-mirror.invalid/")
        client.open = Mock(return_value=io.BytesIO(b"official checksum"))
        url = "https://github.com/jlcodes99/cockpit-tools/releases/download/v1.3.75/SHA256SUMS.txt"
        self.assertEqual(client.read(url), b"official checksum")
        client.open.assert_called_once_with(url)


class ConfigTests(unittest.TestCase):
    def test_compose_dollar_roundtrip(self):
        original = config(ROOT)
        for escaped in [False, True]:
            rendered = deploy.transform_strings(original, lambda value: value.replace("$", "$$")) if escaped else copy.deepcopy(original)
            self.assertEqual(deploy.decode_compose(rendered), original)

    def test_missing_extension_rejected(self):
        with self.assertRaises(deploy.DeployError):
            deploy.decode_compose({"services": {}})

    def test_credentials_validation(self):
        deploy.validate_settings(config(ROOT))
        for password in ["short", "has\nnewline", "\0" * 12, "x" * 73]:
            data = config(ROOT)
            data["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_PASSWORD"] = password
            with self.subTest(password=repr(password)), self.assertRaises(deploy.DeployError):
                deploy.validate_settings(data)

    def test_runtime_config_has_no_plaintext_password_or_secret_mount(self):
        original = config(ROOT)
        snapshot = deploy.runtime_config(original)
        password = original["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_PASSWORD"]
        self.assertNotIn(password, json.dumps(snapshot))
        self.assertEqual(snapshot["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_PASSWORD"], "${VNC_PASSWORD:?VNC_PASSWORD is required}")
        self.assertNotIn("secrets", snapshot)
        self.assertNotIn("secrets", snapshot["services"]["cockpit-vnc"])

    def test_username_cannot_be_an_option_or_htpasswd_separator(self):
        for username in ["-admin", "name:bad", "name\nother"]:
            data = config(ROOT)
            data["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_USERNAME"] = username
            with self.subTest(username=username), self.assertRaises(deploy.DeployError):
                deploy.validate_settings(data)

    def test_default_uses_named_volume(self):
        data = config(ROOT)
        deploy.configure_storage(data, ROOT, "")
        self.assertEqual(data["services"]["cockpit-vnc"]["volumes"][0]["type"], "volume")

    def test_bind_directory_replaces_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data = config(root)
            deploy.configure_storage(data, root, "data")
            mount = data["services"]["cockpit-vnc"]["volumes"][0]
            self.assertEqual(mount["source"], str(root / "data"))
            self.assertFalse(mount["bind"]["create_host_path"])
            self.assertNotIn("cockpit_config", data["volumes"])

    def test_mounting_deployment_or_parent_is_rejected(self):
        for value in [str(ROOT), str(ROOT.parent)]:
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.configure_storage(config(ROOT), ROOT, value)

    def test_matching_storage_is_reused(self):
        deploy.check_existing_storage(config(ROOT), existing())

    def test_storage_change_is_rejected(self):
        with self.assertRaises(deploy.DeployError):
            deploy.check_existing_storage(config(ROOT), existing("bind", "/old/data"))

    def test_other_project_container_is_rejected(self):
        with self.assertRaises(deploy.DeployError):
            deploy.check_existing_storage(config(ROOT), existing(project="other"))

    def test_existing_bind_match_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data = config(root)
            deploy.configure_storage(data, root, "data")
            deploy.check_existing_storage(data, existing("bind", str(root / "data")))

    def test_new_env_generates_password_and_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(ROOT / ".env.example", root / ".env.example")
            password = "a" * 48
            with patch.object(deploy.secrets, "token_hex", return_value=password) as generate, contextlib.redirect_stdout(io.StringIO()) as output:
                deploy.ensure_env(root)
            generate.assert_called_once_with(24)
            self.assertIn('VNC_PASSWORD="' + password + '"', (root / ".env").read_text(encoding="utf-8"))
            self.assertNotIn(password, output.getvalue())
            (root / ".env").write_text("custom configuration")
            with patch.object(deploy.secrets, "token_hex") as generate:
                deploy.ensure_env(root)
            generate.assert_not_called()
            self.assertEqual((root / ".env").read_text(), "custom configuration")

    def test_capture_does_not_leak_invalid_env_value(self):
        with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "PRIVATE-PASSWORD")):
            with self.assertRaises(deploy.DeployError) as raised:
                deploy.captured(["docker", "compose", "config"], ROOT, {})
        self.assertNotIn("PRIVATE-PASSWORD", str(raised.exception))


@unittest.skipUnless(os.environ.get("COCKPIT_RUN_DOCKER_TESTS") == "1", "仅在远程 Linux Docker 测试环境显式启用")
class ComposeIntegrationTests(unittest.TestCase):
    def test_real_compose_preserves_passwords_and_snapshot(self):
        for password in ["simple-password", "literal$var$$ # spaces", "quotes' and \\ backslashes", "trailing-backslash\\", "both'\"quotes$and#slash\\", "中文密码123456"]:
            with self.subTest(password=password), tempfile.TemporaryDirectory(prefix="cockpit-test-") as tmp:
                root = Path(tmp)
                (root / ".env").write_text("VNC_USERNAME=custom-user\nVNC_PASSWORD=" + deploy.dotenv_quote(password) + "\n", encoding="utf-8")
                shutil.copyfile(ROOT / "compose.yaml", root / "compose.yaml")
                deploy.private_dir(root / ".runtime")
                data = deploy.read_config(root, dict(os.environ))
                self.assertEqual(data["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_PASSWORD"], password)
                self.assertNotIn("secrets", data)
                self.assertNotIn("secrets", data["services"]["cockpit-vnc"])
                data.pop("x-cockpit-deploy")
                snapshot = deploy.runtime_config(data)
                snapshot["services"]["cockpit-vnc"]["environment"]["ROUNDTRIP_PROBE"] = "$$"
                path = root / ".runtime" / "compose.json"
                deploy.write_private(path, json.dumps(snapshot))
                self.assertNotIn(password, path.read_text())
                environment = dict(os.environ, VNC_PASSWORD=password)
                result = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f", str(path), "config", "--format", "json"], env=environment, capture_output=True, text=True, check=True)
                rendered = json.loads(result.stdout)
                rendered["x-cockpit-deploy"] = {"dollar_literal": rendered["services"]["cockpit-vnc"]["environment"]["ROUNDTRIP_PROBE"]}
                actual = deploy.decode_compose(rendered)["services"]["cockpit-vnc"]["environment"]["COCKPIT_WEB_PASSWORD"]
                self.assertEqual(actual, password)


if __name__ == "__main__":
    unittest.main()
