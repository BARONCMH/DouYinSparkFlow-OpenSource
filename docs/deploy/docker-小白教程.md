# 多用户面板 Docker 小白教程

本教程按 Windows + Docker Desktop + 本机试用编写。全程复制命令即可启动，不用下载源码或自己编译镜像。

## 1. 安装 Docker Desktop

从 [Docker Desktop 官方下载页](https://www.docker.com/products/docker-desktop/)下载安装 Windows 版本。安装完成后启动 Docker Desktop，等左下角显示 **Engine running**。如果安装程序提示启用 WSL 2，按屏幕提示完成后重启电脑。

## 2. 建立面板文件夹

打开开始菜单，搜索 **PowerShell** 并打开，然后整段复制执行：

```powershell
$panelDir = Join-Path $env:USERPROFILE 'DouYinSparkFlow-Panel'
New-Item -ItemType Directory -Force -Path $panelDir | Out-Null
Set-Location $panelDir
Invoke-WebRequest 'https://raw.githubusercontent.com/BARONCMH/DouYinSparkFlow-OpenSource/main/server-panel/compose.yml' -OutFile 'compose.yml'
Invoke-WebRequest 'https://raw.githubusercontent.com/BARONCMH/DouYinSparkFlow-OpenSource/main/server-panel/panel.env.example' -OutFile 'panel.env.example'
Copy-Item 'panel.env.example' 'panel.env'
New-Item -ItemType Directory -Force -Path 'config','logs' | Out-Null
Set-Content -Encoding ascii 'config/.env' 'TZ=Asia/Shanghai'
notepad 'panel.env'
```

记事本打开后，把 `PANEL_PASSWORD=` 改成你自己的强密码，保存并关闭记事本。请记下这个密码，它是网页管理员的初始密码。邮件通知暂时不用时，`SMTP_HOST` 等项目留空即可。

## 3. 启动面板

在同一个 PowerShell 窗口执行：

```powershell
docker compose up -d
docker compose ps
```

首次启动会自动从 GitHub Container Registry 下载镜像，可能需要几分钟。状态显示 `Up` 后，在浏览器打开 [http://localhost:18080](http://localhost:18080)，用户名默认是 `admin`，密码是刚刚填入 `panel.env` 的值。

查看运行日志：

```powershell
docker compose logs -f
```

按 `Ctrl+C` 只会退出日志查看，不会关闭面板。

## 4. 更新或关闭

更新到最新版本：

```powershell
docker compose pull
docker compose up -d
```

暂时停止面板：

```powershell
docker compose down
```

再次启动：

```powershell
docker compose up -d
```

## 5. 数据和公网访问

账号、加密后的抖音登录状态保存在当前文件夹的 `config`，运行记录保存在 `logs`。不要删除或公开这两个文件夹，也不要把 `panel.env` 发给别人。卸载容器时不要加 `-v`，否则会删除容器卷数据。

当前配置只允许本机访问。**不要把 18080 端口直接映射到公网。**如果要让其他人访问，请先准备域名和 HTTPS 反向代理，再按[详细的多用户部署教程](panel.md)配置 TLS、备份与更新。

## 常见问题

- **`Cannot connect to the Docker daemon`**：Docker Desktop 尚未启动或还没显示 Engine running。
- **登录页打不开**：运行 `docker compose ps` 看状态，再运行 `docker compose logs --tail 100` 查看错误。
- **拉取镜像失败**：确认网络能访问 `ghcr.io`；稍等后执行 `docker compose pull` 重试。镜像是公开的，不需要 Docker 登录。
- **改了密码但网页没变**：`PANEL_PASSWORD` 只用于第一次初始化；如果已经初始化管理员密码，请在面板的账号/安全设置中修改。
