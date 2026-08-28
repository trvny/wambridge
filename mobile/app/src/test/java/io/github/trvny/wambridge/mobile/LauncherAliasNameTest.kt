package io.github.trvny.wambridge.mobile

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The launcher alias name in the manifest and the one the code toggles must be the same string.
 *
 * They stopped being the same the moment debug builds got their own applicationId: the alias is
 * declared absolutely, so it stays `trvny.wambridge.mobile.LauncherAlias` in every variant, while
 * the code built it from `packageName`, which a debug build suffixes. The result was not a crash -
 * `setComponentEnabledSetting` on a component that does not exist does nothing at all, so the
 * hide-launcher button would have looked healthy and had no effect.
 *
 * A relative `.LauncherAlias` would not fix it either: relative manifest names resolve against the
 * `namespace` (`io.github.trvny.wambridge.mobile`), not against the applicationId. Measured by
 * dumping the built APK's manifest on 2026-08-28.
 */
class LauncherAliasNameTest {
    @Test
    fun `the constant matches what the manifest declares`() {
        val manifest = File("src/main/AndroidManifest.xml")
        assertTrue(
            "expected the manifest at ${manifest.absolutePath}",
            manifest.isFile,
        )

        val declared = Regex(
            """<activity-alias\b[^>]*?android:name="([^"]+)"""",
            RegexOption.DOT_MATCHES_ALL,
        ).find(manifest.readText())?.groupValues?.get(1)

        assertEquals(LAUNCHER_ALIAS_CLASS, declared)
    }

    @Test
    fun `the alias is declared absolutely, so it survives an applicationId suffix`() {
        assertTrue(
            "a relative name would resolve against the namespace, not the applicationId",
            !LAUNCHER_ALIAS_CLASS.startsWith("."),
        )
    }
}
