#!/usr/bin/env python3
"""Deploy Cockpit Tools using GitHub release metadata and Docker Compose."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


API = "https://api.github.com/repos/jlcodes99/cockpit-tools"
SERVICE = "cockpit-vnc"


class DeployError(Exception):
    pass


class NotFound(DeployError):
    pass


def normalize_arch(value):
    aliases = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    try:
        return aliases[str(value).lower()]
    except KeyError:
        raise DeployError("仅支持 amd64/x86_64 和 arm64/aarch64；官方未提供此架构的 deb 包。") from None


def transform_strings(value, transform):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [transform_strings(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: transform_strings(item, transform) for key, item in value.items()}
    return value


def decode_compose(config):
    # Compose versions differ in whether JSON output escapes literal dollar signs.
    probe = config.get("x-cockpit-deploy", {}).get("dollar_literal")
    if probe == "$$":
        return transform_strings(config, lambda value: value.replace("$$", "$"))
    if probe == "$":
        return config
    raise DeployError("Compose 未保留部署配置，请安装支持 config --format json 的 Compose v2。")


def runtime_config(config):
    snapshot = transform_strings(config, lambda value: value.replace("$", "$$"))
    snapshot["services"][SERVICE]["environment"]["COCKPIT_WEB_PASSWORD"] = "${VNC_PASSWORD:?VNC_PASSWORD is required}"
    return snapshot


def private_dir(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise DeployError(f"工作目录必须是实际目录：{path.name}")
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def write_private(path, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def dotenv_quote(value):
    if any(character in value for character in "\r\n\0"):
        raise DeployError("密码包含换行或空字符，请在 .env 中设置单行 VNC_PASSWORD。")
    return json.dumps(value, ensure_ascii=False).replace("$", "$$")


def ensure_env(root):
    path = root / ".env"
    if path.is_symlink():
        raise DeployError(".env 不得是符号链接。")
    if not path.exists():
        password = secrets.token_hex(24)
        template = (root / ".env.example").read_text(encoding="utf-8")
        if template.count("\nVNC_PASSWORD=\n") != 1:
            raise DeployError(".env.example 缺少唯一的 VNC_PASSWORD 空值项。")
        write_private(path, template.replace("\nVNC_PASSWORD=\n", "\nVNC_PASSWORD=" + dotenv_quote(password) + "\n"))
        print("已创建 .env；登录密码保存在该文件中，不会输出到终端。", flush=True)
    path.chmod(0o600)


@contextlib.contextmanager
def deployment_lock(root):
    import fcntl
    with (root / ".start.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("已有部署正在运行。") from None
        yield


def captured(command, root, environment):
    result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True)
    if result.returncode:
        # Config errors may echo a malformed .env value, so never relay raw stderr.
        raise DeployError("命令执行失败：" + " ".join(command[:3]) + "；请检查 Docker 和 .env 配置。")
    return result.stdout


def read_config(root, environment):
    command = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "compose.yaml"), "config", "--format", "json"]
    return decode_compose(json.loads(captured(command, root, environment)))


def validate_settings(config):
    settings = config["x-cockpit-deploy"]
    service = config["services"][SERVICE]
    username = service["environment"]["COCKPIT_WEB_USERNAME"]
    password = service["environment"]["COCKPIT_WEB_PASSWORD"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}", username):
        raise DeployError("VNC_USERNAME 仅允许字母、数字、下划线、点、@ 和连字符，且不能以连字符开头。")
    if not isinstance(password, str) or len(password) < 8 or len(password.encode("utf-8")) > 72 or any(c in password for c in "\r\n\0"):
        raise DeployError("VNC_PASSWORD 至少 8 个字符、最多 72 个 UTF-8 字节，且不能包含换行或空字符。")
    for name in ("USER_ID", "GROUP_ID"):
        if not re.fullmatch(r"[0-9]+", str(service["environment"][name])):
            raise DeployError(name + " 必须是非负整数。")
    for proxy in [settings["download_proxy"], service["build"]["args"].get("HTTP_PROXY", ""), service["environment"].get("HTTP_PROXY", "")]:
        if proxy and urllib.parse.urlsplit(proxy).scheme not in ("http", "https"):
            raise DeployError("代理地址必须以 http:// 或 https:// 开头。")
    prefix = settings["download_prefix"]
    if prefix and urllib.parse.urlsplit(prefix).scheme not in ("http", "https"):
        raise DeployError("DOWNLOAD_PREFIX 必须以 http:// 或 https:// 开头。")
    mirror = service["build"]["args"].get("UBUNTU_MIRROR", "")
    if not re.fullmatch(r"https?://[A-Za-z0-9.:/_~-]+", mirror):
        raise DeployError("UBUNTU_MIRROR 必须是无查询参数的 HTTP/HTTPS 软件源地址。")


def select_asset(release, arch):
    suffix = r"(?:amd64|x86_64)" if arch == "amd64" else r"(?:arm64|aarch64)"
    candidates = [asset for asset in release.get("assets", []) if re.search(r"[_-]" + suffix + r"\.deb$", asset.get("name", ""), re.I) and "cockpit" in asset.get("name", "").lower()]
    if not candidates:
        raise NotFound("该版本未提供 " + arch + " 的 Cockpit Tools deb 包。")
    if len(candidates) != 1:
        raise DeployError("该版本存在多个匹配的 deb 包，无法安全选择。")
    return candidates[0]


def asset_digest(release, asset, client):
    digest = asset.get("digest") or ""
    if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        return digest.split(":", 1)[1].lower()
    checksum = next((item for item in release.get("assets", []) if item.get("name") == "SHA256SUMS.txt"), None)
    if checksum is None:
        raise DeployError("该版本未提供可验证的 SHA256，已停止部署。")
    try:
        content = client.read(checksum["browser_download_url"])
    except NotFound:
        raise DeployError("官方 SHA256 校验文件不存在，已停止部署。") from None
    checksum_digest = checksum.get("digest") or ""
    if checksum_digest.startswith("sha256:") and hashlib.sha256(content).hexdigest() != checksum_digest[7:].lower():
        raise DeployError("SHA256 校验文件自身的校验失败。")
    for line in content.decode("utf-8").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line)
        if match and match[2] == asset["name"]:
            return match[1].lower()
    raise DeployError("官方校验文件中缺少所选安装包的 SHA256。")


class ReleaseClient:
    def __init__(self, proxy="", prefix="", token=""):
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler()
        self.opener = urllib.request.build_opener(handler)
        self.prefix = prefix or ""
        self.token = token

    def open(self, url):
        headers = {"User-Agent": "cockpit-webtop-deployer"}
        if url.startswith(API + "/"):
            headers["Accept"] = "application/vnd.github+json"
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):
            try:
                return self.opener.open(urllib.request.Request(url, headers=headers), timeout=60)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise NotFound("官方资源不存在（HTTP 404）。") from None
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise DeployError(f"下载请求失败（HTTP {error.code}）；403 可能是 GitHub 限流，可设置 GITHUB_TOKEN。") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise DeployError("无法连接下载服务，请检查网络或 DOWNLOAD_PROXY。") from None
            time.sleep(attempt + 1)

    def read(self, url):
        try:
            with self.open(url) as response:
                content = response.read(4 * 1024 * 1024 + 1)
        except (OSError, http.client.HTTPException):
            raise DeployError("版本元数据或校验文件下载中断。") from None
        if len(content) > 4 * 1024 * 1024:
            raise DeployError("版本元数据或校验文件超出大小限制。")
        return content

    def release(self, version):
        endpoint = "/releases/latest" if version == "latest" else "/releases/tags/" + urllib.parse.quote(version if version.startswith("v") else "v" + version, safe="")
        release = json.loads(self.read(API + endpoint))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,100}", release.get("tag_name", "")):
            raise DeployError("官方 Release 返回了无效的版本标签。")
        return release

    def package(self, asset, digest, cache):
        path = cache / (digest + ".deb")
        if path.is_file() and file_digest(path) == digest:
            print("复用已通过 SHA256 校验的安装包。", flush=True)
            return path
        url = asset["browser_download_url"]
        if not url.startswith("https://github.com/jlcodes99/cockpit-tools/releases/download/"):
            raise DeployError("Release 安装包地址不属于官方仓库。")
        print("下载 " + asset["name"], flush=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
                temporary = Path(output.name)
                with self.open(self.prefix + url) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            if file_digest(temporary) != digest:
                raise DeployError("安装包 SHA256 校验失败，未修改容器。")
            os.replace(temporary, path)
            return path
        except (OSError, http.client.HTTPException):
            raise DeployError("安装包下载中断，请检查网络后重试。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_package(client, requested, arch, cache):
    try:
        release = client.release(requested)
        asset = select_asset(release, arch)
        digest = asset_digest(release, asset, client)
        package = client.package(asset, digest, cache)
    except NotFound:
        if requested == "latest":
            raise DeployError("最新版本没有可下载的 " + arch + " Cockpit Tools deb 包。") from None
        print("指定版本或对应架构的安装包不存在，改用最新稳定版本。", flush=True)
        return resolve_package(client, "latest", arch, cache)
    return release, asset, digest, package


def configure_storage(config, root, data_dir):
    service = config["services"][SERVICE]
    if not data_dir:
        return
    target = Path(data_dir).expanduser()
    target = (root / target).resolve() if not target.is_absolute() else target.resolve()
    if target == root or target in root.parents:
        raise DeployError("数据目录不能是部署目录本身或其上级目录，请使用独立目录。")
    if target.exists() and not target.is_dir():
        raise DeployError("指定的数据目录不是目录。")
    service["volumes"] = [mount for mount in service.get("volumes", []) if mount["target"] != "/config"]
    service["volumes"].append({"type": "bind", "source": str(target), "target": "/config", "bind": {"create_host_path": False}})
    config.get("volumes", {}).pop("cockpit_config", None)


def check_existing_storage(config, existing):
    if existing is None:
        return
    labels = existing["Config"].get("Labels") or {}
    if labels.get("com.docker.compose.project") != config["name"] or labels.get("com.docker.compose.service") != SERVICE:
        raise DeployError("同名容器属于其他项目；请修改 .env 的 CONTAINER_NAME 或 COMPOSE_PROJECT_NAME。")
    desired = next(mount for mount in config["services"][SERVICE]["volumes"] if mount["target"] == "/config")
    actual = next((mount for mount in existing["Mounts"] if mount["Destination"] == "/config"), None)
    if desired["type"] == "volume":
        expected = config["volumes"][desired["source"]]["name"]
        matches = actual and actual["Type"] == "volume" and actual.get("Name") == expected
    else:
        matches = actual and actual["Type"] == "bind" and actual["Source"] == desired["source"]
    if not matches:
        raise DeployError("现有容器的数据挂载与本次配置不同，已停止以保护数据。请通过 DATA_DIR 或原项目/卷配置明确沿用原存储。")


def inspect_container(name, root, environment):
    result = subprocess.run(["docker", "container", "inspect", name], cwd=root, env=environment, capture_output=True, text=True)
    if result.returncode == 0:
        return json.loads(result.stdout)[0]
    if "No such" in result.stderr:
        return None
    raise DeployError("无法检查现有容器，请确认 Docker 正常运行。")


def prepare_bind(config):
    service = config["services"][SERVICE]
    mount = next(mount for mount in service["volumes"] if mount["target"] == "/config")
    if mount["type"] != "bind":
        return
    target = Path(mount["source"])
    if not target.exists():
        target.mkdir(mode=0o700, parents=True)
        uid = int(service["environment"]["USER_ID"])
        gid = int(service["environment"]["GROUP_ID"])
        if os.geteuid() == 0:
            os.chown(target, uid, gid)
        elif target.stat().st_uid != uid or target.stat().st_gid != gid:
            raise DeployError("无法为新数据目录设置指定 UID/GID，请预先创建目录并授予相应权限。")


def wait_ready(config, root, environment, timeout=120):
    service = config["services"][SERVICE]
    name = service["container_name"]
    port = next(item["published"] for item in service["ports"] if item["target"] == 5800)
    # Only the local readiness probe bypasses the generated self-signed certificate.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect_container(name, root, environment)
        if info is None or info["State"].get("OOMKilled") or info.get("RestartCount", 0):
            raise DeployError("容器初始化失败，请检查 docker logs " + name)
        process = subprocess.run(["docker", "exec", name, "pgrep", "-xc", "cockpit-tools"], env=environment, capture_output=True, text=True)
        status = 0
        try:
            with client.open("https://127.0.0.1:" + str(port) + "/", timeout=3) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except (OSError, urllib.error.URLError):
            pass
        if process.returncode == 0 and process.stdout.strip() == "1" and status in (200, 302):
            return port
        time.sleep(2)
    raise DeployError("启动检查超时，请检查 docker logs " + name)


def parser():
    result = argparse.ArgumentParser(description="部署 Cockpit Tools；默认使用最新稳定版本。")
    result.add_argument("release", nargs="?", help="版本，例如 1.3.75 或 latest")
    result.add_argument("--version", dest="release_option", help="指定 Release 版本，不存在时改用最新版")
    result.add_argument("--data-dir", help="挂载的数据目录；相对路径以部署目录为基准")
    result.add_argument("--port", type=int, help="网页映射端口")
    result.add_argument("--api-port", type=int, help="API 映射端口")
    result.add_argument("--proxy", help="设置本次下载、构建及应用使用的 HTTP/HTTPS 代理")
    return result


def deploy(args, root):
    if not sys.platform.startswith("linux"):
        raise DeployError("请在目标 Linux 主机上执行 start.sh，不要在 Windows 上直接部署。")
    if shutil.which("docker") is None:
        raise DeployError("请先安装 Docker Engine 和 Docker Compose v2 插件。")
    if args.release and args.release_option:
        raise DeployError("版本只能通过位置参数或 --version 指定一种。")
    requested = args.release_option or args.release or "latest"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,100}", requested):
        raise DeployError("版本格式无效。")
    environment = dict(os.environ)
    if args.proxy is not None:
        environment.update(APP_PROXY=args.proxy, DOWNLOAD_PROXY=args.proxy, BUILD_PROXY=args.proxy)
    for value, key in [(args.port, "VNC_WEB_PORT"), (args.api_port, "API_PORT")]:
        if value is not None:
            if not 1 <= value <= 65535:
                raise DeployError("端口范围必须为 1 到 65535。")
            environment[key] = str(value)
    captured(["docker", "compose", "version"], root, environment)
    daemon = json.loads(captured(["docker", "info", "--format", "{{json .}}"], root, environment))
    if daemon.get("OSType") != "linux":
        raise DeployError("Docker 服务端必须运行 Linux 容器。")
    arch = normalize_arch(daemon["Architecture"])
    with deployment_lock(root):
        ensure_env(root)
        private_dir(root / ".runtime")
        private_dir(root / ".cache")
        config = read_config(root, environment)
        validate_settings(config)
        settings = config.pop("x-cockpit-deploy")
        configure_storage(config, root, settings["data_dir"] if args.data_dir is None else args.data_dir)
        existing = inspect_container(config["services"][SERVICE]["container_name"], root, environment)
        check_existing_storage(config, existing)
        client = ReleaseClient(settings["download_proxy"], settings["download_prefix"], environment.get("GITHUB_TOKEN", ""))
        release, asset, digest, package = resolve_package(client, requested, arch, root / ".cache")
        tag = release["tag_name"]
        print("使用版本 " + tag + "，架构 " + arch, flush=True)
        service = config["services"][SERVICE]
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", tag[1:] if tag.startswith("v") else tag)
        service["image"] = "cockpit-tools-vnc:" + slug + "-" + arch
        service["platform"] = "linux/" + arch
        service["build"]["args"].update(COCKPIT_VERSION=tag, COCKPIT_SHA256=digest, COCKPIT_ARCH=arch)
        prepare_bind(config)
        shutil.copyfile(package, root / ".runtime" / "package.deb")
        runtime = root / ".runtime" / "compose.json"
        # Re-escape dollars when handing the normalized model back to Compose.
        # Keep credentials out of the generated configuration file.
        snapshot = runtime_config(config)
        environment["VNC_PASSWORD"] = service["environment"]["COCKPIT_WEB_PASSWORD"]
        write_private(runtime, json.dumps(snapshot, ensure_ascii=False, indent=2))
        command = ["docker", "compose", "--env-file", "/dev/null", "-f", str(runtime)]
        captured(command + ["config", "--quiet"], root, environment)
        subprocess.run(command + ["build"], cwd=root, env=environment, check=True)
        subprocess.run(command + ["up", "-d", "--no-build", "--force-recreate"], cwd=root, env=environment, check=True)
        port = wait_ready(config, root, environment)
        write_private(root / ".runtime" / "release.json", json.dumps({"tag": tag, "arch": arch, "asset": asset["name"], "sha256": digest}, indent=2))
        print(f"部署完成：https://<服务器IP>:{port}\n用户名：{service['environment']['COCKPIT_WEB_USERNAME']}\n密码文件：{root / '.env'}", flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        deploy(args, Path(__file__).resolve().parent)
        return 0
    except (DeployError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
        if isinstance(error, DeployError):
            print("错误：" + str(error), file=sys.stderr)
        else:
            print("错误：部署失败，请检查网络、磁盘空间、文件权限和 Docker 状态。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中止部署。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
