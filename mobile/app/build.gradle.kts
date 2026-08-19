plugins {
    id("com.android.application")
}

android {
    namespace = "io.github.trvny.wambridge.mobile"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.trvny.wambridge.mobile"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
