package io.github.trvny.wambridge.mobile

import android.content.Context
import android.net.Network
import android.net.Uri
import android.net.wifi.WifiManager
import java.io.BufferedInputStream
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URI
import java.nio.charset.StandardCharsets
import java.util.concurrent.Callable
import java.util.concurrent.ExecutorCompletionService
import java.util.concurrent.Executors

internal object WamDiscovery {
    data class Speaker(val ip: String, val source: String)

    fun discover(
        context: Context,
        allowScan: Boolean,
        ssdpTimeoutMs: Long = 2_500,
    ): List<Speaker> {
        val targets = WifiLan.targets(context)
        if (targets.isEmpty()) return emptyList()

        val wifiManager = context.applicationContext.getSystemService(WifiManager::class.java)
        val multicastLock = wifiManager?.createMulticastLock("wambridge-discovery")?.apply {
            setReferenceCounted(false)
        }
        runCatching { multicastLock?.acquire() }

        return try {
            val ssdp = discoverSsdp(targets, ssdpTimeoutMs)
            if (ssdp.isNotEmpty() || !allowScan) ssdp else scanLocalLan(targets)
        } finally {
            if (multicastLock?.isHeld == true) multicastLock.release()
        }
    }

    private fun discoverSsdp(targets: List<WifiLan.Target>, timeoutMs: Long): List<Speaker> {
        val found = linkedMapOf<String, Speaker>()
        val payloads = SEARCH_TARGETS.map(::searchMessage)

        for (target in targets) {
            DatagramSocket().use { socket ->
                try {
                    target.network.bindSocket(socket)
                    socket.soTimeout = 300
                    for (payload in payloads) {
                        repeat(2) {
                            socket.send(DatagramPacket(payload, payload.size, SSDP_ADDRESS, SSDP_PORT))
                        }
                    }

                    val deadline = System.currentTimeMillis() + timeoutMs
                    while (System.currentTimeMillis() < deadline) {
                        val buffer = ByteArray(65_535)
                        val packet = DatagramPacket(buffer, buffer.size)
                        try {
                            socket.receive(packet)
                        } catch (_: SocketTimeoutException) {
                            continue
                        }

                        val headers = parseHeaders(packet.data, packet.length)
                        if (!looksLikeWam(headers)) continue
                        val location = headers["location"].orEmpty()
                        val locationHost = runCatching { URI(location).host }.getOrNull()
                        val ip = locationHost ?: packet.address.hostAddress ?: continue
                        if (isUsableIpv4(ip)) found[ip] = Speaker(ip, "SSDP")
                    }
                } catch (_: IOException) {
                    // Try another active Wi-Fi network/interface.
                }
            }
        }
        return found.values.sortedBy { it.ip }
    }

    private fun scanLocalLan(targets: List<WifiLan.Target>): List<Speaker> {
        val ownAddresses = targets.map { it.address.hostAddress }.toSet()
        val candidates = linkedMapOf<String, Network>()

        for (target in targets) {
            for (ip in subnetHosts(target)) {
                if (ip !in ownAddresses) candidates.putIfAbsent(ip, target.network)
            }
        }
        if (candidates.isEmpty()) return emptyList()

        val executor = Executors.newFixedThreadPool(minOf(32, candidates.size))
        val completion = ExecutorCompletionService<Speaker?>(executor)
        return try {
            for ((ip, network) in candidates) {
                completion.submit(Callable {
                    if (probeWam(network, ip, SCAN_TIMEOUT_MS)) Speaker(ip, "LAN scan") else null
                })
            }

            val found = linkedMapOf<String, Speaker>()
            repeat(candidates.size) {
                val speaker = runCatching { completion.take().get() }.getOrNull()
                if (speaker != null) found[speaker.ip] = speaker
            }
            found.values.sortedBy { it.ip }
        } finally {
            executor.shutdownNow()
        }
    }

    private fun subnetHosts(target: WifiLan.Target): Sequence<String> {
        val prefix = target.prefixLength.coerceIn(0, 32)
        val hostBits = 32 - prefix
        val addressCount = 1L shl hostBits
        val usableCount = when {
            prefix <= 30 -> (addressCount - 2).coerceAtLeast(0)
            else -> addressCount
        }
        if (usableCount == 0L) return emptySequence()

        val address = ipv4ToLong(target.address)
        val mask = if (prefix == 0) 0L else (0xffff_ffffL shl hostBits) and 0xffff_ffffL
        val network = address and mask
        val broadcast = network + addressCount - 1
        val subnetFirst = if (prefix <= 30) network + 1 else network
        val subnetLast = if (prefix <= 30) broadcast - 1 else broadcast

        if (usableCount <= MAX_SCAN_HOSTS) {
            return (subnetFirst..subnetLast).asSequence().map(::longToIpv4)
        }

        // Do not spray tens of thousands of probes across a /16. On broad
        // Wi-Fi prefixes, keep the fallback useful by probing the local /24
        // window that contains this phone while SSDP remains the primary path.
        val localWindow = address and LOCAL_WINDOW_MASK
        val first = maxOf(subnetFirst, localWindow + 1)
        val last = minOf(subnetLast, localWindow + 254)
        return if (first <= last) {
            (first..last).asSequence().map(::longToIpv4)
        } else {
            emptySequence()
        }
    }

    private fun ipv4ToLong(address: Inet4Address): Long = address.address.fold(0L) { result, byte ->
        (result shl 8) or (byte.toInt() and 0xff).toLong()
    }

    private fun longToIpv4(value: Long): String = listOf(
        (value ushr 24) and 0xff,
        (value ushr 16) and 0xff,
        (value ushr 8) and 0xff,
        value and 0xff,
    ).joinToString(".")

    private fun probeWam(network: Network, ip: String, timeoutMs: Int): Boolean {
        val command = Uri.encode("<name>GetSpkName</name>")
        val request = buildString {
            append("GET /UIC?cmd=").append(command).append(" HTTP/1.1\r\n")
            append("Host: ").append(ip).append(':').append(WAM_PORT).append("\r\n")
            append("Connection: close\r\n\r\n")
        }.toByteArray(StandardCharsets.UTF_8)

        return try {
            Socket().use { socket ->
                network.bindSocket(socket)
                socket.connect(InetSocketAddress(ip, WAM_PORT), timeoutMs)
                socket.soTimeout = timeoutMs
                socket.getOutputStream().apply {
                    write(request)
                    flush()
                }
                val input = BufferedInputStream(socket.getInputStream())
                val buffer = ByteArray(2_048)
                val count = input.read(buffer)
                if (count <= 0) return false
                val response = String(buffer, 0, count, StandardCharsets.UTF_8)
                response.startsWith("HTTP/1.") && response.substringBefore("\r\n").contains(" 200 ")
            }
        } catch (_: IOException) {
            false
        }
    }

    private fun parseHeaders(payload: ByteArray, length: Int): Map<String, String> {
        val text = String(payload, 0, length, StandardCharsets.UTF_8)
        val headers = mutableMapOf<String, String>()
        for (line in text.replace("\r\n", "\n").split('\n').drop(1)) {
            val separator = line.indexOf(':')
            if (separator <= 0) continue
            headers[line.substring(0, separator).trim().lowercase()] =
                line.substring(separator + 1).trim()
        }
        return headers
    }

    private fun looksLikeWam(headers: Map<String, String>): Boolean {
        val text = listOf("st", "usn", "server", "location")
            .joinToString(" ") { headers[it].orEmpty() }
            .lowercase()
        return "remotecontrolreceiver" in text || ("samsung" in text && "audio" in text)
    }

    private fun searchMessage(target: String): ByteArray = buildString {
        append("M-SEARCH * HTTP/1.1\r\n")
        append("HOST: 239.255.255.250:1900\r\n")
        append("MAN: \"ssdp:discover\"\r\n")
        append("MX: 2\r\n")
        append("ST: ").append(target).append("\r\n\r\n")
    }.toByteArray(StandardCharsets.US_ASCII)

    private fun isUsableIpv4(value: String): Boolean {
        val address = runCatching { InetAddress.getByName(value) as? Inet4Address }.getOrNull()
            ?: return false
        return !address.isLoopbackAddress &&
            !address.isLinkLocalAddress &&
            !address.isMulticastAddress &&
            !address.isAnyLocalAddress
    }

    private val SSDP_ADDRESS: InetAddress = InetAddress.getByName("239.255.255.250")
    private const val SSDP_PORT = 1900
    private const val WAM_PORT = 55001
    private const val SCAN_TIMEOUT_MS = 220
    private const val MAX_SCAN_HOSTS = 1_024L
    private const val LOCAL_WINDOW_MASK = 0xffff_ff00L
    private const val WAM_SEARCH_TARGET = "urn:samsung.com:device:RemoteControlReceiver:1"
    private val SEARCH_TARGETS = listOf(WAM_SEARCH_TARGET, "ssdp:all")
}
