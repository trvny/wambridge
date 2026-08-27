package io.github.trvny.wambridge.mobile

import java.nio.charset.StandardCharsets

internal object SsdpLifecycle {
    fun advertisedTargets(udn: String, targets: List<String>): List<String> =
        listOf("uuid:$udn") + targets

    fun usn(udn: String, target: String): String =
        if (target.startsWith("uuid:", ignoreCase = true)) {
            "uuid:$udn"
        } else {
            "uuid:$udn::$target"
        }

    fun alive(
        host: String,
        location: String,
        server: String,
        udn: String,
        target: String,
    ): ByteArray = buildString {
        append("NOTIFY * HTTP/1.1\r\n")
        append("HOST: $host\r\n")
        append("CACHE-CONTROL: max-age=1800\r\n")
        append("LOCATION: $location\r\n")
        append("NT: $target\r\n")
        append("NTS: ssdp:alive\r\n")
        append("SERVER: $server\r\n")
        append("USN: ${usn(udn, target)}\r\n\r\n")
    }.toByteArray(StandardCharsets.UTF_8)

    fun byebye(
        host: String,
        udn: String,
        target: String,
    ): ByteArray = buildString {
        append("NOTIFY * HTTP/1.1\r\n")
        append("HOST: $host\r\n")
        append("NT: $target\r\n")
        append("NTS: ssdp:byebye\r\n")
        append("USN: ${usn(udn, target)}\r\n\r\n")
    }.toByteArray(StandardCharsets.UTF_8)
}