# cockpit-tools-docker

[Cockpit Tools](https://github.com/jlcodes99/cockpit-tools) 的 Docker 部署方案，通过浏览器访问容器内的 Cockpit Tools 桌面应用，适用于 Linux 服务器和 NAS，无需宿主机桌面或 GPU。

本项目提供部署脚本与容器配置，应用安装包来自上游官方 Releases；本项目并非 Cockpit Tools 官方发布的 Docker 镜像。

## 功能

- 一键下载、校验、构建并启动容器，默认使用最新稳定版。
- 支持指定 Release 版本；版本或对应架构安装包不存在时自动使用最新版。
- 自动识别 `amd64/x86_64`、`arm64/aarch64`，选择对应的 Linux deb 包。
- 支持自动创建存储卷，或挂载指定的宿主机数据目录。
- 在 `.env` 中配置网页登录账号、密码、端口和代理。
- 基于固定摘要的多架构 Ubuntu 24.04 / VNC / Openbox 镜像，包含 NAS 权限兼容处理。

## 环境要求

- Linux 主机，CPU 架构为 x86-64 或 ARM64。
- Bash、Python 3.9+、Docker Engine 和 Docker Compose v2 插件。
- 当前用户具有 Docker 访问权限，Docker 服务端与部署脚本位于同一机器。
- 可访问 GitHub Releases、Docker 镜像仓库及 Ubuntu 软件源，或配置相应代理。

## 快速开始

下载本项目文件，进入项目目录后执行：

```sh
cd cockpit-tools-docker
bash start.sh
```

首次启动会生成 `.env`，其中包含默认用户名 `cockpit` 和随机密码。部署完成后，访问 `https://<服务器IP>:13001`，使用 `.env` 中的账号密码登录。浏览器可能提示自签名证书。

默认自动创建数据存储卷。首次构建需要下载应用和系统依赖，耗时取决于网络情况。

### 启动参数

```sh
# 最新稳定版
bash start.sh

# 指定版本，支持带或不带 v
bash start.sh --version 1.3.75
bash start.sh v1.3.75

# 挂载指定数据目录；相对路径以此部署目录为基准
bash start.sh --data-dir /opt/cockpit-tools-docker/data

# 调整端口或设置本次运行的代理
bash start.sh --port 13002 --api-port 42992
bash start.sh --proxy http://192.168.1.10:7890
```

不指定版本时，每次查询 GitHub 最新稳定版。指定版本不存在、该版本缺少对应架构的 deb 包，或包下载返回 HTTP 404 时，改用最新稳定版并显示实际版本。网络错误、限流、权限错误和 SHA256 校验失败均停止部署，不会自动切换版本或架构。

安装包通过 GitHub Release 的 SHA256 digest 或官方 `SHA256SUMS.txt` 校验；构建时再次校验并检查 deb 与容器架构一致。已校验的包缓存于 `.cache/`，重复构建使用 Docker 缓存。

每次成功执行会重建容器，确保新版本、账号和密码生效；数据挂载保持不变。脚本会在启动后检查网页和应用进程。

## 账号与配置

首次运行自动生成 `.env`，默认账号为 `cockpit`，随机密码保存在该文件中，不输出到终端。已有 `.env` 不会覆盖，账号密码以 `.env` 中的配置为准。

也可复制 `.env.example` 为 `.env`，填写密码后再启动：

```dotenv
VNC_USERNAME=cockpit
VNC_PASSWORD='replace-with-a-strong-password$#'
DATA_DIR=
VNC_WEB_PORT=13001
API_PORT=42991
```

- 密码至少 8 个字符，最多 72 个 UTF-8 字节，不允许换行。含 `$`、`#` 或空格的密码可用单引号包裹；其中的单引号可写成 `\'`，普通反斜杠保持原样。脚本生成的双引号格式会自动处理转义。
- 账号支持字母、数字、下划线、点、`@` 和连字符，不能以连字符开头。
- `.env` 由 Compose 解析，不会作为 Shell 脚本执行；文件权限设为 `0600`。
- 账号密码由 `.env` 通过环境变量传入容器，不生成或挂载独立密码文件，也不写入镜像。拥有 Docker 管理权限的人可以查看容器环境变量中的密码。启动时更新网页登录认证库，仅保留 `.env` 指定的登录账号。
- 这是浏览器 VNC 桌面的登录账号；不发布原生 VNC `5900` 端口。

端口默认监听所有 IPv4/IPv6 网卡，访问 `https://<服务器IP>:13001`。API 端口 `42991` 只做映射，仍需在应用中启用 API 服务、设为监听 `0.0.0.0:42991` 并配置 API Key。API 不继承桌面网页登录认证，不应无保护暴露公网。

## 数据持久化

`DATA_DIR` 为空且不传 `--data-dir` 时，Compose 自动创建项目专属存储卷，通常名为 `<项目名>_cockpit_config`，挂载到 `/config`。重复部署沿用该卷。

指定目录时使用 bind mount，不另建应用存储卷。新目录自动创建，按 `.env` 的 `USER_ID/GROUP_ID` 设置所有权；已有目录不由部署脚本递归改权限。基础镜像初始化仍可能调整应用配置权限，因此请使用独立的应用数据目录。

命令行参数只对本次执行生效。固定使用某个目录时写入 `.env`：

```dotenv
DATA_DIR=/opt/cockpit-tools-docker/data
USER_ID=1000
GROUP_ID=1000
```

脚本拒绝自动改变现有容器的 `/config` 挂载，或接管其他 Compose 项目的同名容器。沿用已有 `data/` 目录时，应设置 `DATA_DIR=./data`；沿用已有命名卷时，可在 `compose.yaml` 的 `volumes.cockpit_config` 中显式设置原卷的 `name` 与 `external: true`。同时保持原 Compose 项目名。

不要让多个实例同时使用同一数据目录或卷。不要运行 `down -v`，除非确实需要删除卷中的账号与配置。新机器安装不会自动复制旧机器的应用账号。

## 网络与多实例

`.env` 的可选项：

```dotenv
APP_PROXY=
DOWNLOAD_PROXY=
BUILD_PROXY=
DOWNLOAD_PREFIX=
UBUNTU_MIRROR=http://mirrors.aliyun.com/ubuntu
```

`DOWNLOAD_PROXY` 和 `BUILD_PROXY` 留空时继承 `APP_PROXY`。`--proxy` 同时覆盖本次下载、构建和应用代理，不修改 Docker 守护进程配置；基础镜像拉取仍使用 Docker 自身的网络设置。

`DOWNLOAD_PREFIX` 可配置安装包下载加速前缀，例如 `https://gh-proxy.com/`；版本元数据仍来自官方 API，安装包仍按官方 SHA256 校验。GitHub API 限流时可在执行前设置 `GITHUB_TOKEN`，该令牌不发送给安装包镜像站。

`UBUNTU_MIRROR` 用于替换基础镜像的 Ubuntu archive/security 源，默认使用阿里云；ARM64 镜像原有的 Ubuntu ports 源保持不变。

同一主机部署多个实例时，为每份 `.env` 设置不同的项目名、容器名和端口：

```dotenv
COMPOSE_PROJECT_NAME=cockpit-second
CONTAINER_NAME=cockpit-second
VNC_WEB_PORT=13002
API_PORT=42992
```

## 更新

在原部署目录重新执行即可查询并部署最新稳定版：

```sh
bash start.sh
```

固定版本时使用 `bash start.sh --version <版本号>`。更新前备份 `.env` 和应用数据；保留原项目名及数据挂载配置。脚本不会自动迁移不同目录或存储卷之间的数据。

## 项目文件

部署所需文件：`start.sh`、`deploy.py`、`Dockerfile`、`compose.yaml`、`.env.example`、`.dockerignore`、`startapp-vnc.sh`、`vnc-auth-init.sh`。首次运行自动准备安装包和运行时文件。

`.runtime/` 包含生成的 Compose 配置、构建包及实际版本记录，不要跨机器复用。生成的配置仅引用密码环境变量，不保存密码明文。`.cache/` 可删除，之后重新下载。`.env` 和应用存储属于敏感数据，应单独妥善备份。

## 测试

```sh
# 不使用 Docker 的逻辑测试
python3 -m unittest discover -s tests -v

# 仅在远程 Linux Docker 测试机执行，额外验证真实 Compose 的 .env 解析
COCKPIT_RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests -v
```

amd64 的实际部署、登录认证、存储卷、目录挂载及重复部署后的数据保留已验证。ARM64 已核验官方安装包、基础镜像支持及架构选择逻辑，尚未进行实机部署验证。

## 上游项目

- [Cockpit Tools](https://github.com/jlcodes99/cockpit-tools)：应用源码与功能说明。
- [Cockpit Tools Releases](https://github.com/jlcodes99/cockpit-tools/releases)：官方安装包及版本记录。
- [jlesage/baseimage-gui](https://github.com/jlesage/docker-baseimage-gui)：容器图形环境基础镜像。

Cockpit Tools 及基础镜像的使用和分发需遵循各自上游项目的许可证。
