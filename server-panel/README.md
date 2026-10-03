# Multi-user panel overlay

`panel.py` and `tasks.py` are the server-side multi-user panel and worker overlay used with the upstream Docker image. The worker normalizer import is aligned with upstream `core.douyin_im.norm`. The overlay expects the upstream runtime modules and the compatible image version specified by `PANEL_IMAGE`.

## Douyin account authorization

Start authorization from the account page, then scan the displayed QR code with the Douyin app and confirm on the phone. If Douyin asks for a secondary SMS verification, enter that code in the panel. Direct phone-number login has been replaced by QR authorization; manual browser interaction and Cookie import remain available as alternatives.

Successful QR authorization stores an encrypted Playwright `storage_state` containing cookies and site local storage. Login checks and send tasks restore that state. Older encrypted Cookie-only accounts remain readable, and imported Cookie lists are wrapped as a storage state with an empty local-storage section.

## Deployment

Follow the [detailed multi-user panel deployment guide](../docs/deploy/panel.md) for Docker, HTTPS reverse proxy, first login, backups, updates, rollback, and troubleshooting.

The panel's users and Douyin accounts are configured in its web interface. `config/.env` is for global settings such as the time zone and message template. Do not copy the root `.env.example` with its legacy sample `TASKS` and `COOKIES_*` values into a new panel deployment; the first startup can migrate those legacy values as an account.

Keep `panel.env`, `config/`, and `logs/` private. They contain credentials, encrypted account state, and logs. `PANEL_SECRET` must remain stable after account credentials are saved; if it is left empty, the panel persists a generated value in `config/panel-secret`.

To let the panel serve the public downloads, copy the files into `logs/app-downloads/` from this directory:

   ```sh
   mkdir -p logs/app-downloads
   cp ../downloads/DouYinSparkFlow.apk ../downloads/DouYinSparkFlow.apk.sha256 logs/app-downloads/
   cp ../downloads/Get-Douyin-Cookies.exe ../downloads/Get-Douyin-Cookies.exe.sha256 logs/app-downloads/
   ```

`CONFIG_DIR`, `LOG_DIR`, `PANEL_BIND_PORT`, and `PANEL_IMAGE` can be set in the shell or a Compose `.env` file. `PANEL_ENV_FILE` selects the runtime environment file. By default, Compose uses the pinned image `ghcr.io/2061360308/douyinsparkflow:3.2.2` and binds the service to `127.0.0.1:18080`.

## Private data

The config directory contains user/password databases, hashed redemption codes, session keys, account Cookies, and Web Push keys. The log directory contains send records and diagnostic output. Back up these paths securely; never commit or publish them. Use least-privilege filesystem permissions and keep the reverse proxy, base image, and host patched.

This is a deployment overlay, not a standalone server image. Check the upstream image release notes before upgrading `PANEL_IMAGE`; custom overlays can depend on upstream interfaces.
