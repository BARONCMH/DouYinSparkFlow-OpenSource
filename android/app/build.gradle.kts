plugins {
    id("com.android.application")
}

val releaseStorePath = System.getenv("DSF_RELEASE_STORE_FILE")
val configuredSiteUrl = providers.gradleProperty("siteUrl").orElse("https://example.com/").get().trim()
val parsedSiteUrl = java.net.URI(configuredSiteUrl)
require(parsedSiteUrl.scheme.equals("https", ignoreCase = true) && !parsedSiteUrl.host.isNullOrBlank()) {
    "siteUrl must be an HTTPS origin, for example -PsiteUrl=https://your-domain.example/"
}
require(parsedSiteUrl.rawUserInfo == null && parsedSiteUrl.rawQuery == null && parsedSiteUrl.rawFragment == null) {
    "siteUrl must not contain credentials, a query, or a fragment"
}
val normalizedSiteUrl = if (configuredSiteUrl.endsWith("/")) configuredSiteUrl else "$configuredSiteUrl/"
val escapedSiteUrl = normalizedSiteUrl.replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "com.douyinsparkflow.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.douyinsparkflow.mobile"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.0.1"
        buildConfigField("String", "SITE_URL", "\"$escapedSiteUrl\"")
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        if (!releaseStorePath.isNullOrBlank()) {
            create("release") {
                storeFile = file(releaseStorePath)
                storePassword = System.getenv("DSF_RELEASE_STORE_PASSWORD")
                keyAlias = System.getenv("DSF_RELEASE_KEY_ALIAS") ?: "douyinsparkflow"
                keyPassword = System.getenv("DSF_RELEASE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (!releaseStorePath.isNullOrBlank()) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
