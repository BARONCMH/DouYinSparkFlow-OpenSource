# DouYinSparkFlow-OpenSource

上游 [2061360308/DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow)（MIT）的完整源码，
加上本仓库自建的多用户面板 overlay、Android 客户端、Windows Cookie 导出工具，以及可校验的成品下载。

当前版本 **3.2.2**（见 `VERSION`）· 上游镜像 tag 与本仓库发布的镜像 tag 均以该文件为准

---

## 目录

- [这个仓库是什么，不是什么](#这个仓库是什么不是什么)
- [仓库地图](#仓库地图)
- [部署形态怎么选](#部署形态怎么选)
- [设计要点：出口 IP 一致性](#设计要点出口-ip-一致性)
- [快速开始](#快速开始)
- [配置项速查](#配置项速查)
- [客户端与工具](#客户端与工具)
- [成品下载与校验](#成品下载与校验)
- [安全与数据边界](#安全与数据边界)
- [文档站](#文档站)
- [托管面板](#托管面板)
- [常见误解](#常见误解)
- [署名与许可](#署名与许可)

---

## 这个仓库是什么，不是什么

先把边界说清楚，省得部署到一半才发现方向不对。

| | |
|---|---|
| **是** | 一套可自部署的抖音火花续期任务系统：上游任务执行器源码，加上本仓库新增的多用户面板、Android 客户端、Windows Cookie 工具、成品下载。 |
| **是** | 上游代码的 MIT 保留副本，上游 `LICENSE` 未改动。 |
| **不是** | 不是上游项目的官方仓库，也不是抖音及其关联方的官方或合作项目。 |
| **不是** | 不含任何账号、Cookie、好友数据或服务器配置。首次使用必须自己扫码登录、自己导出 Cookie。 |
| **不是** | 不是"防封 / 过风控"工具。不承诺平台兼容性，不承诺账号安全。 |
| **不是** | 不是 [`halfwaystudent/douyin-sparkflow`](https://github.com/halfwaystudent/douyin-sparkflow) 的分支或衍生。那是另一套独立的单用户实现（自带 WebUI，PolyForm 非商业许可）。本仓库的上游是 `2061360308/DouYinSparkFlow`。 |

---

## 仓库地图

| 路径 | 来源 | 作用 | 运行时是否产生敏感数据 |
|---|---|---|---|
| `core/` | 上游 | 浏览器控制、抖音 IM 扫描、消息构建、任务主流程 | 否 |
| `utils/` | 上游 | 配置、日志、一言、GitHub 环境变量导出 | 否 |
| `configTool/` | 上游 | 本地 GUI：扫码登录 → 挑好友 → 导出 Cookie（可借道隧道） | 本地生成 |
| `docker/` | 上游 | 入口脚本，按 `LAUNCH_MODE` 分派 cron / fc 两种模式 | 否 |
| `docs/` | 上游 | docsify 文档站源（无构建步骤） | 否 |
| `main.py` / `fc_server.py` | 上游 | 本地与容器入口 / 云函数入口 | 否 |
| `server-panel/` | **本仓库新增** | 多用户面板与 worker overlay（`panel.py` + `tasks.py` + `compose.yml`） | 是 |
| `android/` | **本仓库新增** | HTTPS-only WebView 客户端源码 | 构建产物含签名 |
| `cookie-tool/` | **本仓库新增** | Windows Cookie 导出工具源码与打包脚本 | 导出文件本身就是凭据 |
| `downloads/` | **本仓库新增** | 成品 APK / EXE 及配套 `.sha256` | 否 |
| `tools/` / `tests/` | 上游 | HAR 录制工具 / pytest 测试集 | 否 |
| `.github/workflows/` | 混合 | 镜像发布、configTool 构建、手动任务 | 否 |
| `aliyun-fc-ros-template.yaml` | 上游 | 阿里云函数 ROS 模板 | 否 |

相对上游，本仓库的增量就是 `server-panel/`、`android/`、`cookie-tool/`、`downloads/` 四项；其余按目录原样保留，方便随时和上游对 diff。

---

## 部署形态怎么选

四种形态，先按"你在哪台机器上跑、出网 IP 是不是同一个"来选。

| 形态 | 适合 | 出网 IP | 入口 |
|---|---|---|---|
| 单用户 Docker（双容器） | 自用、一台 VPS 就够 | 任务容器与 gost 容器同机，天然一致 | `docker compose up -d` |
| 阿里云函数 | 不想养服务器 | 按 `docs/guide/02-cookie与出口IP.md` 处理 | `aliyun-fc-ros-template.yaml` + `fc_server.py` |
| **多用户面板**（`server-panel/`） | 多人共用，要注册 / 兑换 / 推送 | 面板与 worker 同机 | TLS 反代 → `127.0.0.1:18080` |
| GitHub Actions | 仅仓库维护者手动兜底 | GitHub 机房 | `schedule.yml`，**只能手动触发** |

---

## 设计要点：出口 IP 一致性

这是本仓库在部署上最值得先弄懂的一件事。

抓 Cookie 时的出口 IP，和真正跑任务时的出口 IP，**必须是同一个**。不一致会直接导致登录态被判定为异常、任务跑到一半掉线。根目录 `docker-compose.yml` 用两个容器解决这个问题：

- `douyin-spark-flow` —— 任务执行器，容器内 cron 到点跑一轮续火花。
- `gost` —— 配套代理，只给**本地** `configTool` 抓 Cookie 时借道。

两个容器跑在同一台机器上，出网 IP 完全相同，于是"抓 Cookie 的出口"和"跑任务的出口"天然一致，不需要额外搭隧道服务。

本地 `configTool` 的「工具配置」页签这样填：

| 字段 | 值 |
|---|---|
| 隧道地址 | `ws://<服务器公网IP>:9000?path=/ws`（端口不能省，工具不会给 `ws://` 兜默认端口） |
| 隧道账号 | 与 `.env` 的 `GOST_USER` 一致 |
| 隧道密码 | 与 `.env` 的 `GOST_PASSWORD` 一致 |

两个凭据**没有默认值**：`.env` 里不设，compose 会直接拒绝启动（用了 `:?` 强制非空校验）。隧道是明文 `ws`，`GOST_PASSWORD` 务必是足够长的随机串，并在云服务器安全组里把 9000 的来源 IP 限制成你自己的出口 IP。

---

## 快速开始

### A. 单用户 / 自建（推荐先跑通这条）

```bash
git clone https://github.com/BARONCMH/DouYinSparkFlow-OpenSource.git
cd DouYinSparkFlow-OpenSource
cp .env.example .env
# 填 TASKS 与 COOKIES_<unique_id>，再设 GOST_USER / GOST_PASSWORD
docker compose up -d --build
docker compose logs -f
```

**四个容易踩的点：**

1. `TASKS` 和 `COOKIES_<unique_id>` 是必填项。`COOKIES_` 的后缀必须与 `TASKS` 里该账号的 `unique_id` **完全一致**；多账号就是多行。
2. 里面的 JSON 必须保持**单行**，不能写成多行。
3. Cookie 必须是浏览器导出的 **JSON 数组**，不能填 `sessionid=xxx; ttwid=yyy` 这种原始字符串。
4. `fingerprint` 建议填上。填了就锁定该账号的指纹种子；不填则每轮随机，指纹一变账号容易触发风控。

提醒两点部署细节：

- **镜像来源**：`docker compose` 默认直接拉阿里云 ACR 的现成镜像，国内拉之前需要先 `docker login` 对应 registry（地址见 `docker-compose.yml` 注释）。不想登录可以给服务加 `build:` 段，用仓库里的 `Dockerfile` 本地构建；`gost` 也可以换成官方 `gogost/gost:latest`（需要能访问 Docker Hub）。
- **定时点**：由 `.env` 里的 `CRON_HOUR` / `CRON_MINUTE` / `CRON_SECOND` + `TZ` 决定，容器内 cron 到点执行，不需要动宿主机的 crontab。
- Chromium 需要较大的 `/dev/shm`，compose 里已设 `shm_size: 1gb`，别删。

### B. 多用户面板

面板是 **overlay**，不是独立镜像：拿上游镜像当底座，把 `panel.py` 和 `tasks.py` 挂进去。

```sh
# 1) 私有目录（只在本机存在，绝不提交）
mkdir -p server-panel/config server-panel/logs
cp .env.example server-panel/config/.env        # 任务配置，只加自己的

# 2) 面板环境
cp server-panel/panel.env.example server-panel/panel.env
#    必须设：PANEL_PASSWORD（唯一强口令）、WEBPUSH_SUBJECT（你的公网 HTTPS 源）

# 3) 想让面板的下载路由提供成品，就放进去
mkdir -p server-panel/logs/app-downloads
cp downloads/DouYinSparkFlow.apk downloads/DouYinSparkFlow.apk.sha256 server-panel/logs/app-downloads/
cp downloads/Get-Douyin-Cookies.exe downloads/Get-Douyin-Cookies.exe.sha256 server-panel/logs/app-downloads/

# 4) 启动
cd server-panel && docker compose --env-file panel.env -f compose.yml up -d
```

- 面板在容器里监听 `8080`，映射到宿主机 `127.0.0.1:18080`。前面**必须有带有效证书的 TLS 反代**，不要把 18080 直接暴露到公网。
- `PANEL_IMAGE` 必须与上游接口匹配。升级前先看上游 release notes —— 自定义 overlay 可能依赖被改动过的上游接口。
- `PANEL_TRUST_PROXY` 默认 `0`。只有当前面是可信反代、会覆写转发头时才设 `1`，否则会信任伪造的 `X-Forwarded-*`。
- `CONFIG_DIR` / `LOG_DIR` / `PANEL_BIND_PORT` / `PANEL_IMAGE` / `PANEL_ENV_FILE` 都可以在 shell 或 Compose `.env` 里覆盖；Docker 单用户场景另外还有 `CONFIG_ENV_FILE` / `LOGS_DIR` / `GOST_PORT`。

### C. 客户端与工具

见下方[客户端与工具](#客户端与工具)一节。

---

## 配置项速查

| 变量 | 必填 | 说明 |
|---|---|---|
| `TASKS` | 是 | JSON 数组。`username`（仅日志标识）、`unique_id`（抖音号）、`targets`（好友列表，至少一个）；可选 `fingerprint` |
| `COOKIES_<unique_id>` | 是 | 浏览器导出的 Cookie JSON 数组，后缀与 `TASKS` 的 `unique_id` 一致 |
| `GOST_USER` / `GOST_PASSWORD` | 是（Docker） | 隧道凭据，无默认值，不设则 compose 拒绝启动 |
| `CRON_HOUR` / `CRON_MINUTE` / `CRON_SECOND` | 是（Docker） | 容器内定时执行的时间点 |
| `TZ` | 否 | 容器时区，默认 `Asia/Shanghai` |
| `MESSAGE_TEMPLATE` | 否 | `\n` 表示换行，`[API]` 会替换成一言内容 |
| `HITOKOTO_TYPES` | 否 | 一言类型 JSON 数组，如 `["文学","影视","诗词","哲学"]` |
| `BROWSER_ACTION_TIMEOUT` | 否 | 单次浏览器操作 / 导航的最长等待，默认 120 秒 |
| `IM_SCAN_TIMEOUT` | 否 | 整轮扫描总预算。超时即停，此时"未找到"≠"不存在" |
| `IM_READY_TIMEOUT` | 否 | 门禁等待上限（登录校验 + 会话列表就绪） |
| `FRIEND_LIST_WAIT_TIME` | 否 | 好友资料静默窗，调大会明显拖慢扫描 |
| `IM_MAX_STEPS` | 否 | 滚动步数硬上限（步长 = 可视高度 40%） |
| `TASK_RETRY_TIMES` | 否 | 任务失败重试次数，默认 3 |
| `LOG_LEVEL` | 否 | `Debug` / `Info` / `Warning` / `Error` |
| `PROXY_ADDRESS` | 否 | 当前代码中未实际使用，可留空 |

面板侧（`panel.env`）：

| 变量 | 说明 |
|---|---|
| `PANEL_USERNAME` | 默认 `admin` |
| `PANEL_PASSWORD` | 必填，唯一强口令 |
| `PANEL_SECRET` | 可留空；留空则自动生成并把密钥持久化到 config 目录 |
| `WEBPUSH_SUBJECT` | Web Push VAPID 联系地址，填你的公网 HTTPS 源 |
| `PANEL_TRUST_PROXY` | 默认 `0`，仅可信反代场景设 `1` |

---

## 客户端与工具

### Android 客户端

需要 Android SDK Platform 35 与 Build Tools 35.0.0。

```powershell
cd android
.\gradlew.bat assembleDebug -PsiteUrl=https://你的域名/
```

默认 origin 是 `https://example.com/` —— 这样自己构建的 APK 不会"不小心"登进托管服务。

客户端的安全取舍是刻意做紧的：HTTPS-only，TLS 校验失败直接取消请求；关闭 file access、content access、第三方 Cookie、混合内容与额外窗口；没有 JS↔原生桥。会话 Cookie 用 Android Keystore 加密存储。后台发送结果轮询走账号级接口 `/api/mobile/notifications`，可能被系统省电策略延迟。

正式签名走环境变量 `DSF_RELEASE_STORE_FILE`、`DSF_RELEASE_STORE_PASSWORD`、`DSF_RELEASE_KEY_ALIAS`、`DSF_RELEASE_KEY_PASSWORD`。**keystore 和签名口令永远不要提交。**

**iOS**：把站点"添加到主屏幕"即可当作 web app 使用。只有主屏 web app 才能收到 Web Push，普通 Safari 标签页收不到系统通知。原生构建需要 macOS + Xcode。

### Windows Cookie 导出工具

源码在 `cookie-tool/`（`Get-Douyin-Cookies.py` + `build.ps1`），成品在 `downloads/Get-Douyin-Cookies.exe`。

它在本地导出 `douyin_cookies.json`。**把它当密码对待**：导入到 `.env` 之后立刻删除。

---

## 成品下载与校验

| 文件 | 用途 |
|---|---|
| `downloads/DouYinSparkFlow.apk` | Android 客户端成品（托管服务发布产物） |
| `downloads/Get-Douyin-Cookies.exe` | Windows Cookie 导出工具成品 |

每个成品旁边都有 `.sha256`，装之前先校验：

```bash
sha256sum -c downloads/DouYinSparkFlow.apk.sha256
sha256sum -c downloads/Get-Douyin-Cookies.exe.sha256
```

Windows 上可以用 `certutil -hashfile <文件> SHA256` 手动比对。

注意：预编译 APK 的 origin 指向**托管服务**。自建服务请按 `android/README.md` 配置自己的 HTTPS origin 后重新构建，源码在本仓库内。

---

## 安全与数据边界

**永远不要提交**：`.env` 文件、Cookie、账号 / 用户数据库、日志、截图、私钥、keystore、签名口令、服务器配置。`.gitignore` 已排除这些，但仍要养成习惯 —— **每次提交前 `git status` 过一遍**。

**面板的私有数据落在哪：**

| 路径 | 里面是什么 |
|---|---|
| `server-panel/config/` | 用户与口令库、兑换码哈希、会话密钥、账号 Cookie、Web Push 密钥 |
| `server-panel/logs/` | 发送记录与诊断输出 |

这两个目录要最小权限、独立备份，绝不提交或公开。定期更新反代、基础镜像与宿主机补丁。

**暴露面：**

- 面板、登录桌面、代理这类入口默认只绑 `127.0.0.1`。远程访问优先走 SSH 隧道 / VPN / 带认证的 HTTPS 反代，不要直接把端口怼到公网。
- 本地 `configTool` 借道的 `ws://` 隧道是**明文**。除了长随机口令，建议在安全组里限制 9000 端口的来源 IP。想要加密传输，可以给 `gost` 换成 `http+wss://` 并挂载证书，本地地址相应改成 `wss://`。

**漏洞上报**：走 [`SECURITY.md`](SECURITY.md)，用私密渠道，不要在公开 issue 里贴账号数据、Cookie、token、日志或截图。凭据一旦疑似暴露，立刻轮换。

**固有风险**：自动化操作会触发验证码、限流、功能限制、登录态失效，甚至账号封禁。只操作你本人拥有或已获得明确授权的账号，保持低频调用。这些后果由操作者自行承担。

---

## 文档站

`docs/` 是一个 docsify 站点，没有构建步骤：直接打开 `docs/index.html` 就能看，也可以直接读源文件。

| 主题 | 位置 |
|---|---|
| 项目介绍 / 讨论与贡献 | `docs/intro/` |
| 选择部署方式 | `docs/guide/01-选择部署方式.md` |
| Cookie 与出口 IP | `docs/guide/02-cookie与出口IP.md` |
| 配置生成器 | `docs/guide/03-配置生成器.md` |
| Docker（推荐）/ 云函数 / GitHub Action（过时）/ 源码 | `docs/deploy/` |
| 仓库结构 / 工具与测试 / 本地代理调试 | `docs/dev/` |
| 问答 | `docs/faq/faq.md` |

上游讨论区在 [2061360308/DouYinSparkFlow/discussions](https://github.com/2061360308/DouYinSparkFlow/discussions)。

---

## 托管面板

托管控制台在 <https://124.220.96.161/>。这是一项与源码仓库**分开运营**的公共服务 —— 创建账号或输入凭据之前，请先看清它自己的服务条款与隐私做法。

想自己掌控数据，就用 `server-panel/` 自建一套。

---

## 常见误解

**"这是某个项目的 fork 吗？"**
上游是 [`2061360308/DouYinSparkFlow`](https://github.com/2061360308/DouYinSparkFlow)（MIT）。`halfwaystudent/douyin-sparkflow` 是另一套完全独立的实现，本仓库不含它的代码。

**"fork 之后会自动发消息吗？"**
不会。`schedule.yml` 只有 `workflow_dispatch`，且需要在 `user-data` environment 里配好你自己的 Variables 与 Secrets。默认状态下它什么都不会做。

**"面板是一个独立镜像吗？"**
不是。它是 overlay —— 上游镜像 + 挂载进去的 `panel.py` / `tasks.py`。

**"能防封吗？"**
不能。本仓库只做低频率自动化，对平台风控不做任何承诺，也不提供绕过手段。

**"`downloads/` 里的 APK 能直接连我自己的服务吗？"**
不能。预编译 APK 指向托管服务，自建必须改 origin 重新构建。

---

## 署名与许可

- 上游源码：[`2061360308/DouYinSparkFlow`](https://github.com/2061360308/DouYinSparkFlow)，Copyright 2026 2061360308 盧瞳，MIT。上游 `LICENSE` 原样保留。
- 本仓库新增的 `server-panel/`、`android/`、`cookie-tool/`：同为本仓库 MIT 许可，除非文件内另有说明。
- `downloads/` 中是发布产物；其构建源码在本仓库内。
- 第三方素材（图标、字体、截图、商标、平台内容）不自动继承本项目许可，以各自目录中的授权声明为准。
- 详见 [`NOTICE.md`](NOTICE.md)。
