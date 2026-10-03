# 多用户 Web 面板部署教程

本文介绍如何把本仓库的多用户 Web 面板部署到自己的 Linux 服务器。面板使用 Docker Compose 运行，浏览器通过 HTTPS 访问；面板数据和账号配置保存在服务器的 `server-panel/config/` 与 `server-panel/logs/` 目录。

> 本教程用于自建实例。自动化操作可能导致登录态失效、功能限制或账号处罚；只使用你本人拥有或已获授权的账号，并遵守抖音平台规则。

## 部署前准备

- 一台可以访问 GitHub Container Registry（GHCR）的 Linux 服务器。Ubuntu 22.04 / 24.04 可按本文命令安装 Docker；其他发行版请参考其对应的 Docker 安装文档。
- 建议从 **2 vCPU、4 GB 内存**起步。授权浏览器和发送任务都会占用内存，账号较多或同时操作较多时应增加内存。
- 一个指向服务器公网 IP 的域名。DNS 的 `A` 记录应指向服务器；若设置了 `AAAA` 记录，服务器也必须能通过 IPv6 正常接收请求。
- 服务器防火墙或云安全组放行 TCP `80`、`443`，并限制 SSH 来源。**不要向公网开放 `18080` 或容器的 `8080` 端口。**

以下命令以普通部署用户执行；Docker 命令使用 `sudo`。不要把 `panel.env`、`config/` 或 `logs/` 上传到公开仓库。

## 1. 安装 Docker 和 Compose

Ubuntu 上可使用 Docker 官方软件源安装 Docker Engine 与 Compose 插件：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

确认安装结果：

```bash
sudo docker version
sudo docker compose version
```

如使用 Debian、Rocky Linux 等系统，请按 [Docker 官方安装说明](https://docs.docker.com/engine/install/)选择对应发行版，不要混用不同系统的软件源。

## 2. 下载项目并创建私有数据目录

```bash
git clone https://github.com/BARONCMH/DouYinSparkFlow-OpenSource.git
cd DouYinSparkFlow-OpenSource/server-panel
mkdir -p config logs
```

新面板在网页中管理用户和抖音号，因此 `config/.env` 只放时区、消息模板等全局设置。**不要直接把根目录 `.env.example` 复制过来**：其中示例 `TASKS` 和假 Cookie 是旧版格式，面板首次启动会尝试迁移旧格式账号。

创建最小配置：

```bash
cat > config/.env <<'EOF'
TZ=Asia/Shanghai
EOF
```

需要调整消息模板、日志级别等全局选项时，可在面板的设置页管理可用选项，或按根目录 [`.env.example`](../../.env.example) 的说明添加对应变量。不要把 `TASKS`、`COOKIES_*` 示例值放进新面板的 `config/.env`。

## 3. 设置管理员和 Web Push 配置

```bash
cp panel.env.example panel.env
openssl rand -hex 24
nano panel.env
```

把 `openssl` 输出的一串随机字符复制到 `PANEL_PASSWORD`，并将 `WEBPUSH_SUBJECT` 改成你之后实际使用的 HTTPS 网站根地址。例如：

```dotenv
PANEL_USERNAME=admin
PANEL_PASSWORD=这里替换为刚生成的随机密码
PANEL_SECRET=
WEBPUSH_SUBJECT=https://panel.example.com/
PANEL_TRUST_PROXY=1
```

- `PANEL_PASSWORD` 必须设置，否则面板会拒绝启动。请使用唯一的强密码；登录后也可以在面板中修改管理员密码。
- `PANEL_SECRET` 留空时，程序会生成密钥并保存到 `config/panel-secret`。备份时要保留该文件。若自行填写 `PANEL_SECRET`，之后必须一直保留同一个值；更换密钥会使已保存的抖音登录凭据无法解密，并让现有登录会话失效。
- `WEBPUSH_SUBJECT` 填站点的公开 HTTPS origin，并以 `/` 结尾；不要填 IP、HTTP 地址或带页面路径的地址。
- 只有按下文配置了可信的本机反向代理，并让它覆盖转发头时，才把 `PANEL_TRUST_PROXY` 设为 `1`。不要让公网客户端直接控制这些转发头。

### 可选：配置发送结果邮件

邮件由站点管理员配置 SMTP；每位用户再在网页的「我的账号 → 邮件通知」里填写自己的收件地址、发送测试邮件并选择是否开启。未配置 SMTP 时不会发邮件，Web Push 仍可单独使用。

将以下变量追加到服务器私有的 `panel.env`。以 QQ 邮箱为例，须先在邮箱设置里开启 SMTP 并生成授权码；163、企业邮箱等请换成服务商提供的 SMTP 地址、端口和授权码：

```dotenv
SMTP_HOST=smtp.qq.com
SMTP_PORT=587
SMTP_USERNAME=你的完整邮箱地址
SMTP_PASSWORD=邮箱 SMTP 授权码
SMTP_FROM=你的完整邮箱地址
SMTP_FROM_NAME=DouYinSparkFlow
SMTP_USE_SSL=0
SMTP_STARTTLS=1
```

端口 `587` 通常使用 STARTTLS。若邮箱服务商要求 `465` SSL，将 `SMTP_PORT` 改为 `465`、`SMTP_USE_SSL=1`、`SMTP_STARTTLS=0`。`SMTP_FROM` 通常应与认证邮箱一致；用户名和授权码要么都填写，要么都留空（仅适用于服务商允许匿名中继的环境）。不要把邮箱登录密码或配置好的 `panel.env` 提交到 GitHub。

如果授权码包含 `$` 等会被 Compose 展开的字符，在 `panel.env` 中按 Compose 环境文件语法用单引号括起该值，例如 `SMTP_PASSWORD='这里填写完整授权码'`。

保存 SMTP 配置后，在确认没有发送任务运行时重建面板容器，让新环境变量生效：

```bash
sudo docker compose --env-file panel.env -f compose.yml up -d --force-recreate
```

再用用户账号打开「我的账号」，填入个人收件地址，点「保存邮件设置」，点「发送测试邮件」确认收件箱能收到邮件，最后勾选「接收发送结果邮件」并再次保存。测试按钮对每个面板账号有 60 秒冷却，同一收件地址有 5 分钟冷却；测试成功表示 SMTP 接收了邮件，不保证邮件一定进入收件箱。面板只在邮件中提供任务结果摘要，不附 Cookie 或完整日志。用户邮箱地址保存在私有的 `config/panel-email-settings.json` 中。

限制配置文件权限：

```bash
chmod 700 config logs
chmod 600 config/.env panel.env
```

`panel.env` 含管理员密码，`config/` 含用户数据与加密后的登录凭据，`logs/` 含运行记录；三者都应保持私有。

## 4. 启动面板并检查日志

以下命令都在 `server-panel/` 目录运行：

```bash
sudo docker compose --env-file panel.env -f compose.yml pull
sudo docker compose --env-file panel.env -f compose.yml up -d
sudo docker compose --env-file panel.env -f compose.yml ps
sudo docker compose --env-file panel.env -f compose.yml logs --tail=100
```

正常启动时，`ps` 应显示容器处于 `Up` 状态，日志会显示面板监听 `8080`。容器端口只映射到服务器本机的 `127.0.0.1:18080`。在服务器本机可检查登录页：

```bash
curl -I http://127.0.0.1:18080/login
```

配置 HTTPS 反向代理之后，再从浏览器访问域名。不要用公网 IP 的 `http://` 地址登录。

## 5. 配置 HTTPS 反向代理

反向代理与 Docker 面板应运行在同一台服务器上。这样 `127.0.0.1:18080` 只对本机开放，公网流量经由反向代理进入。若你使用另一台机器或另一个 Docker 容器运行代理，应按该架构调整网络，并确保面板端口仍不会直接暴露给公网。

### 使用 Caddy（示例）

先将域名 DNS 指向服务器，并按 [Caddy 官方安装说明](https://caddyserver.com/docs/install)安装 Caddy。确认 TCP `80`、`443` 可从公网访问，然后编辑 `/etc/caddy/Caddyfile`：

```caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:18080 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

将 `panel.example.com` 替换成自己的域名，再检查并重载 Caddy：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

Caddy 会为可公开访问的域名申请并续期 HTTPS 证书。若已有 Nginx 或其他反向代理，也可以继续使用；需将站点代理到 `http://127.0.0.1:18080`，启用有效 HTTPS，并覆盖传给面板的 `Host`、`X-Real-IP`、`X-Forwarded-For` 和 `X-Forwarded-Proto` 请求头。只有代理可信且覆盖这些头时才启用 `PANEL_TRUST_PROXY=1`。

现在可访问 `https://panel.example.com/`。浏览器应显示有效证书，登录 Cookie 会使用 `Secure` 属性，Web Push 也需要 HTTPS。

## 6. 首次登录和添加账号

1. 打开 `https://你的域名/`，使用 `panel.env` 里的 `PANEL_USERNAME` 和 `PANEL_PASSWORD` 登录管理员账号。
2. 如果需要开放普通用户注册，在管理员面板中保持注册开启；要限制使用者时，可在管理页关闭公开注册并自行管理账号。
3. 普通用户通过网站注册并登录后，在账号页添加自己的抖音号。
4. 按页面提示发起授权，并使用抖音 App 扫描网页二维码，在手机上确认。若抖音要求额外短信验证，在页面中填写收到的验证码。
5. 为账号设置目标好友、发送时间和消息配置，再查看面板显示的任务状态与记录。
6. 如果要使用通知，在支持 Web Push 的浏览器中允许通知权限。通知是否及时还受浏览器、系统省电策略和网络状态影响。

每个面板账号只能查看和操作自己名下的抖音号。管理员密码、账号凭据和二维码都不要转发给他人。

## 7. 可选：让面板提供下载文件

若希望面板的下载页面提供本仓库的 APK 或 Cookie 工具，把发行文件及其校验文件复制到 `logs/app-downloads/`：

```bash
mkdir -p logs/app-downloads
cp ../downloads/DouYinSparkFlow.apk ../downloads/DouYinSparkFlow.apk.sha256 logs/app-downloads/
cp ../downloads/Get-Douyin-Cookies.exe ../downloads/Get-Douyin-Cookies.exe.sha256 logs/app-downloads/
```

此步骤为可选项；没有这些文件时仍可正常使用面板。用户安装或运行前，应按页面提供的 SHA-256 校验文件检查下载内容。

## 8. 备份、更新和回滚

### 备份

至少定期备份 `config/`、`logs/`、`panel.env` 和当前 Git 提交版本。它们包含管理员设置、用户数据库、加密后的抖音登录状态、兑换码状态、通知订阅与发送记录。备份文件也属于敏感数据，应限制读取权限并存放在受控位置；不要提交到 GitHub。

建议在没有发送任务运行时进行一致性备份。若通过停止容器备份，先确认没有任务正在执行，因为停止容器会中断面板及其中的任务：

```bash
sudo docker compose --env-file panel.env -f compose.yml stop
cd ..
umask 077
tar -czf "$HOME/panel-backup-$(date +%F-%H%M).tar.gz" server-panel/config server-panel/logs server-panel/panel.env
cd server-panel
sudo docker compose --env-file panel.env -f compose.yml up -d
```

将备份复制到另一块受控存储，并保留生成备份时的 Git 提交号：

```bash
cd ..
git rev-parse HEAD
```

### 更新

更新 overlay 或镜像会重建容器，可能打断正在执行的发送任务。由于 `panel.py` 和 `tasks.py` 是从仓库目录挂载进容器的，请先确认没有任务运行并停止面板，再更新代码，避免容器运行期间直接替换挂载文件：

```bash
cd /path/to/DouYinSparkFlow-OpenSource/server-panel
sudo docker compose --env-file panel.env -f compose.yml stop
cd ..
git pull --ff-only
cd server-panel
sudo docker compose --env-file panel.env -f compose.yml pull
sudo docker compose --env-file panel.env -f compose.yml up -d
sudo docker compose --env-file panel.env -f compose.yml logs --tail=100
```

`compose.yml` 默认使用固定版本的 `PANEL_IMAGE`，不要擅自改为 `latest`。升级前查看仓库的变更说明；若 overlay 更新而镜像接口不兼容，请保留原提交号并先不要升级生产实例。

### 回滚

若升级后出现问题，先保留当前数据目录和日志。停止服务后，将代码检出到升级前记录的提交号，再按该版本的部署文件拉取并启动对应镜像：

```bash
sudo docker compose --env-file panel.env -f compose.yml stop
cd /path/to/DouYinSparkFlow-OpenSource
git checkout --detach <升级前的提交号>
cd server-panel
sudo docker compose --env-file panel.env -f compose.yml pull
sudo docker compose --env-file panel.env -f compose.yml up -d
```

如果升级过程还迁移或改写了数据，应在停机状态下先把 `config/` 和 `logs/` 恢复到同一份升级前备份；不要只恢复其中一个目录。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 容器不断重启，日志提示 `PANEL_PASSWORD 未设置` | 检查 `panel.env` 中密码是否为空；确认命令使用了 `--env-file panel.env`。 |
| 浏览器无法访问域名 | 检查 DNS、云安全组 TCP `80/443`、Caddy 状态和 `127.0.0.1:18080`；不要开放面板容器端口解决。 |
| 浏览器提示不安全或通知不可用 | 确认通过域名 HTTPS 访问且证书有效；`WEBPUSH_SUBJECT` 必须与实际 HTTPS origin 一致。 |
| 测试邮件失败或没有收到 | 检查 `panel.env` 的 SMTP 主机、端口、发件地址和授权码；核对 SSL/STARTTLS 组合以及服务器出站端口是否被服务商或云厂商拦截，并查看垃圾邮件。 |
| 登录 IP 限速没有按真实访客生效 | 确认代理与面板在同一台主机、覆盖设置了转发头，并且 `PANEL_TRUST_PROXY=1`；不要信任可由公网访客自行填写的转发头。 |
| 抖音号授权失败或登录态失效 | 看面板账号页提示和容器日志，重新扫码授权；升级前不要更改 `PANEL_SECRET` 或删除 `config/panel-secret`。 |
| 磁盘或内存不足 | 用 `df -h`、`free -h` 检查主机资源；确认日志增长和同时打开的授权会话数量。不要删除 `config/` 来清理空间。 |

查看容器状态和近期日志：

```bash
cd /path/to/DouYinSparkFlow-OpenSource/server-panel
sudo docker compose --env-file panel.env -f compose.yml ps
sudo docker compose --env-file panel.env -f compose.yml logs --tail=200
df -h
free -h
```

更多面板实现与账号授权说明见 [`server-panel/README.md`](../../server-panel/README.md)。
