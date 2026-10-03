# Multi-user panel overlay

`panel.py` and `tasks.py` are the server-side multi-user panel and worker overlay used with the upstream Docker image. The worker normalizer import is aligned with upstream `core.douyin_im.norm`. The overlay expects the upstream runtime modules and the compatible image version specified by `PANEL_IMAGE`.

## Douyin account authorization

Start authorization from the account page, then scan the displayed QR code with the Douyin app and confirm on the phone. If Douyin asks for a secondary SMS verification, enter that code in the panel. Direct phone-number login has been replaced by QR authorization; manual browser interaction and Cookie import remain available as alternatives.

Successful QR authorization stores an encrypted Playwright `storage_state` containing cookies and site local storage. Login checks and send tasks restore that state. Older encrypted Cookie-only accounts remain readable, and imported Cookie lists are wrapped as a storage state with an empty local-storage section.

## Deployment

1. Use a dedicated Linux host and configure a reverse proxy with a valid TLS certificate.
2. Create private `config/` and `logs/` directories beside this file. Copy the root `.env.example` to `config/.env`, then add only your own task configuration and Cookies.
3. Copy `panel.env.example` to `panel.env`, set a unique strong `PANEL_PASSWORD`, and set `WEBPUSH_SUBJECT` to your public HTTPS origin. Keep `panel.env`, `config/`, and `logs/` private.
4. Put the public downloads in `logs/app-downloads/` if you want the panel's download routes to serve them:

   ```sh
   mkdir -p logs/app-downloads
   cp ../downloads/DouYinSparkFlow.apk ../downloads/DouYinSparkFlow.apk.sha256 logs/app-downloads/
   cp ../downloads/Get-Douyin-Cookies.exe ../downloads/Get-Douyin-Cookies.exe.sha256 logs/app-downloads/
   ```

5. Start the container with `docker compose --env-file panel.env -f compose.yml up -d`. The panel listens on `127.0.0.1:18080`; proxy HTTPS traffic to that local port.

`CONFIG_DIR`, `LOG_DIR`, `PANEL_BIND_PORT`, and `PANEL_IMAGE` can be set in the shell or a Compose `.env` file. `PANEL_ENV_FILE` selects the runtime environment file. The worker's task configuration is separate in `config/.env`.

## Private data

The config directory contains user/password databases, hashed redemption codes, session keys, account Cookies, and Web Push keys. The log directory contains send records and diagnostic output. Back up these paths securely; never commit or publish them. Use least-privilege filesystem permissions and keep the reverse proxy, base image, and host patched.

This is a deployment overlay, not a standalone server image. Check the upstream image release notes before upgrading `PANEL_IMAGE`; custom overlays can depend on upstream interfaces.
