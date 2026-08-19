plugins {
    id("com.android.application")
}

// The release workflow supplies both. The displayed version comes from one place
// only - `version` in the repository's pyproject.toml - so nothing can quietly
// disagree with it again; the literals below are the fallback for local and CI
// debug builds and should be kept equal to it.
val wamVersionName = (findProperty("wamVersionName") as String?)
    ?.takeIf { it.isNotBlank() }
    ?: "0.1.3"
val wamVersionCode = (findProperty("wamVersionCode") as String?)
    ?.toIntOrNull()
    ?: 103

val releaseKeystorePath = System.getenv("WAMBRIDGE_KEYSTORE_PATH")
val releaseStorePassword = System.getenv("WAMBRIDGE_KEYSTORE_PASSWORD")
val releaseKeyAlias = System.getenv("WAMBRIDGE_KEY_ALIAS")
val releaseKeyPassword = System.getenv("WAMBRIDGE_KEY_PASSWORD")
val releaseSigningReady = listOf(
    releaseKeystorePath,
    releaseStorePassword,
    releaseKeyAlias,
    releaseKeyPassword,
).all { !it.isNullOrBlank() }

android {
    namespace = "io.github.trvny.wambridge.mobile"
    compileSdk = 36

    defaultConfig {
        applicationId = "trvny.wambridge.mobile"
        minSdk = 26
        targetSdk = 36
        versionCode = wamVersionCode
        versionName = wamVersionName
    }

    signingConfigs {
        if (releaseSigningReady) {
            create("release") {
                storeFile = file(requireNotNull(releaseKeystorePath))
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (releaseSigningReady) {
                signingConfig = signingConfigs.getByName("release")
            }
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
