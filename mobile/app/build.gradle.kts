plugins {
    id("com.android.application")
}

// The release workflow supplies both. The displayed version comes from one place
// only - `version` in the repository's pyproject.toml - so nothing can quietly
// disagree with it again; the literals below are the fallback for local and CI
// debug builds and should be kept equal to it.
val wamVersionName = (findProperty("wamVersionName") as String?)
    ?.takeIf { it.isNotBlank() }
    ?: "0.0.5"
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

val sharedStationPack = rootProject.file("../src/wambridge/station_packs.json")
val generatedStationAssets = layout.buildDirectory.dir("generated/station-packs").get().asFile
val syncStationPacks = tasks.register<Copy>("syncStationPacks") {
    from(sharedStationPack)
    into(generatedStationAssets)
}

android {
    namespace = "io.github.trvny.wambridge.mobile"
    compileSdk = 37

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

androidComponents {
    onVariants(selector().all()) { variant ->
        variant.sources.assets?.addStaticSourceDirectory(generatedStationAssets.absolutePath)
    }
}

tasks.named("preBuild").configure {
    dependsOn(syncStationPacks)
}

dependencies {
    // Plain JVM unit tests only. The parts worth testing here are arithmetic
    // (subnet planning) rather than anything that needs a device or Robolectric,
    // so the adapter stays free of an instrumentation harness.
    testImplementation("junit:junit:4.13.2")
}
