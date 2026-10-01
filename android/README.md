# Android app

The app is a HTTPS-only WebView client for the mobile control panel. It supports the website's login, registration, account settings, redemption, and send history, and includes a confirmed logout flow for switching accounts. It has no JavaScript-to-native bridge. TLS failures are cancelled; file access, content access, third-party cookies, mixed content, and extra windows are disabled.

Background send-result checks use the account-scoped `/api/mobile/notifications` endpoint. Session cookies are stored encrypted with Android Keystore. Android may delay periodic background jobs because of battery policies.

## Build

Install Android SDK Platform 35 and Build Tools 35.0.0. Configure the HTTPS origin for your own panel with a Gradle property, then build:

```powershell
cd android
.\gradlew.bat assembleDebug -PsiteUrl=https://your-domain.example/
```

The default origin is `https://example.com/` so a self-built app cannot accidentally sign into the hosted service. Release signing must be configured using environment variables (`DSF_RELEASE_STORE_FILE`, `DSF_RELEASE_STORE_PASSWORD`, `DSF_RELEASE_KEY_ALIAS`, and `DSF_RELEASE_KEY_PASSWORD`). Never commit a keystore or signing password. The prebuilt APK in `../downloads/` is the existing hosted-service release and its SHA-256 is provided beside it.

## iOS

The website can be added to the iOS Home Screen as a web app. Native iOS builds require macOS and Xcode. Safari Web Push is available for Home Screen web apps on supported iOS versions; ordinary Safari tabs do not deliver system notifications.
