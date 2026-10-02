# DouYin Spark Flow

This repository contains the upstream automation project, the multi-user web panel used by this deployment, Android client source, the Windows Cookie export tool, and the current Android/Windows downloads.

## Repository contents

- `core/`, `configTool/`, `utils/`, and `docs/`: upstream DouYinSparkFlow source and documentation.
- `server-panel/`: the multi-user panel and worker overlay, with a Compose example and environment template.
- `android/`: Android WebView client source, including account switching and send-result notifications.
- `cookie-tool/`: source and build helper for the Windows Cookie export utility.
- `downloads/`: current Android APK and Windows EXE with SHA-256 files.

The existing Android APK is a release artifact built for the hosted service. For a self-hosted build, configure the HTTPS origin as described in [`android/README.md`](android/README.md). The Windows Cookie tool exports credentials locally; treat its generated `douyin_cookies.json` as a password and delete it after importing.

## Quick start

For the upstream single-user project, start with the [deployment guide](docs/guide/01-选择部署方式.md) and `.env.example`.

For the multi-user panel overlay, read [`server-panel/README.md`](server-panel/README.md) before deployment. It requires an HTTPS reverse proxy, a strong administrator password, persistent private config/log directories, and the compatible upstream container image.

## Online site

The hosted control panel is available at [续火花控制台](https://124.220.96.161/). This public service is operated separately from the source repository; review its terms and privacy practices before creating an account or entering credentials.

## Security and privacy

Never commit `.env` files, Cookies, account/user databases, logs, screenshots, private keys, keystores, signing passwords, or server configuration. These are excluded by `.gitignore`; review `git status` before every commit. Report suspected security issues privately using the process in [`SECURITY.md`](SECURITY.md).

The project automates browser interaction with Douyin. Use it only for accounts you control, at a low frequency, and in compliance with the platform's terms and applicable law. Account restrictions and other consequences remain the operator's responsibility.

## Attribution and license

The upstream project is [`2061360308/DouYinSparkFlow`](https://github.com/2061360308/DouYinSparkFlow) and is distributed under MIT. The upstream `LICENSE` is retained. The panel, Android client, and Cookie utility additions are provided in this repository under the same license unless a file says otherwise.
