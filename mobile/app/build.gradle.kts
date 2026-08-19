plugins {
    id("com.android.application")
}

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
        versionCode = 4
        versionName = "0.1.3"
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
