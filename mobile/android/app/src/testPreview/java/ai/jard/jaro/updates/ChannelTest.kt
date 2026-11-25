package ai.jard.jaro.updates

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ChannelTest {
    private fun manifest() = JSONObject()
        .put("versionCode", 12)
        .put("versionName", "0.0.9-preview.12")
        .put("version", "0.0.9")
        .put("minSdk", 31)
        .put("published", "2026-11-24")
        .put("notes", JSONArray(List(30) { "x".repeat(300) }))
        .put("apk", JSONObject()
            .put("url", "https://github.com/AdamGera/Jaro/releases/download/android-preview-12/JaroPreview-0.0.9-preview.12.apk")
            .put("size", 65_000_000)
            .put("sha256", "AB".repeat(32)))

    @Test fun validManifestRoundTripsAndBoundsNotes() {
        val release = checkNotNull(Release.parse(manifest().toString()))
        assertEquals("ab".repeat(32), release.sha256)
        assertEquals(List(8) { "x".repeat(200) }, release.notes)
        assertEquals(release, Release.parse(release.json()))
        assertTrue(release.isNewerThan(installedCode = 11, sdk = 31))
        assertFalse(release.isNewerThan(installedCode = 12, sdk = 31))
        assertFalse(release.isNewerThan(installedCode = 11, sdk = 30))
    }

    @Test fun urlMustNameTheExactJaroPreviewBuild() {
        val url = manifest().getJSONObject("apk").getString("url")
        val addresses = listOf(
            url.replace("https:", "http:"),
            url.replace("github.com", "github.com.example.com"),
            url.replace("github.com", "user@github.com"),
            url.replace("github.com", "github.com:444"),
            url.replace("AdamGera/Jaro", "other/Jaro"),
            url.replace("android-preview-12", "android-preview-11"),
            url.replace("JaroPreview-0.0.9-preview.12.apk", "Jaro-0.0.9.apk"),
            "$url?extra=1",
            "$url#extra",
        )
        for (address in addresses) {
            val invalid = manifest().also { it.getJSONObject("apk").put("url", address) }
            assertNull(address, Release.parse(invalid.toString()))
        }
    }

    @Test fun rejectsMalformedManifestAndInvalidBuildMetadata() {
        assertNull(Release.parse("not json"))
        for ((key, value) in listOf("versionCode" to -1, "versionName" to "internal", "minSdk" to 21, "version" to "0.0")) {
            assertNull(key, Release.parse(manifest().put(key, value).toString()))
        }
        for ((key, value) in listOf("size" to 0, "size" to 400_000_001, "sha256" to "bad")) {
            val invalid = manifest().also { it.getJSONObject("apk").put(key, value) }
            assertNull(key, Release.parse(invalid.toString()))
        }
    }
}
