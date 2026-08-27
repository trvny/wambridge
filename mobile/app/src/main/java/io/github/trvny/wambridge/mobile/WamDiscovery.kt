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
import java.util.concurrent.TimeUnit

internal object WamDiscovery {
    data class Speaker(val ip: String, val source: String)

    /**
     * How much of the Wi-Fi subnet the LAN fallback actually probed.
     *
     * An empty speaker list means two very different things, and the caller has
     * to be able to tell them apart: "probed every address and the speaker is
     * not there" is a finding, while "probed 254 of 65 534 because the subnet is
     * a /16" is not. Reporting the second as the first is how the UI ended up
     * telling people their network was blocking discovery when the app had
     * simply not looked.
     */
    sealed interface Scan {
        /** SSDP answered, or the caller did not ask for the fallback. */
        object NotRun : Scan

        /** Every usable address in the subnet was probed. */
        data class Full(val hosts: Int) : Scan

        /**
         * The subnet is wider than [MAX_SCAN_HOSTS], so only the /24 window
         * around this phone was probed. [subnetHosts] is what a full sweep
         * would have covered.
         */
        data class Narrowed(val hosts: Int, val prefixLength: Int, val subnetHosts: Long) : Scan

        /**
         * Two active Wi-Fi networks share addresses, and each shared address was
         * probed on the first of them only. Rare, but 192.168.1.0/24 is the most
         * reused range there is, so a speaker on the second network can sit in
         * the swept range and still never be asked.
         */
        data class Overlapping(val hosts: Int, val shared: Int) : Scan

        /** The subnet yielded no probeable address at all. */
        object NoAddresses : Scan
    }

    data class Result(val speakers: List<Speaker>, val scan: Scan)

    fun discover(
        context: Context,
        allowScan: Boolean,
        ssdpTimeoutMs: Long = 2_500,
        shouldContinue: () -> Boolean = { true },
    ): Result {
        val targets = WifiLan.targets(context)
        if (targets.isEmpty() || !shouldContinue()) return Result(emptyList(), Scan.NotRun)

        val wifiManager = context.applicationContext.getSystemService(WifiManager::class.java)
        val multicastLock = wifiManager?.createMulticastLock("wambridge-discovery")?.apply {
            setReferenceCounted(false)
        }
        runCatching { multicastLock?.acquire() }

        return try {
            val ssdp = discoverSsdp(targets, ssdpTimeoutMs, shouldContinue)
            if (ssdp.isNotEmpty() || !allowScan || !shouldContinue()) {
                Result(ssdp, Scan.NotRun)
            } else {
                scanLocalLan(targets, shouldContinue)
            }
        } finally {
            if (multicastLock?.isHeld == true) multicastLock.release()
        }
    }

    private fun discoverSsdp(
        targets: List<WifiLan.Target>,
        timeoutMs: Long,
        shouldContinue: () -> Boolean,
    ): List<Speaker> {
        val found = linkedMapOf<String, Speaker>()
        val payloads = SEARCH_TARGETS.map(::searchMessage)

        for (target in targets) {
            if (!shouldContinue()) break
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
                    while (shouldContinue() && System.currentTimeMillis() < deadline) {
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

    private fun scanLocalLan(
        targets: List<WifiLan.Target>,
        shouldContinue: () -> Boolean,
    ): Result {
        val ownAddresses = targets.map { it.address.hostAddress }.toSet()
        val candidates = linkedMapOf<String, Network>()
        // Several Wi-Fi addresses can be active at once. If any of them had to be
        // narrowed, the sweep as a whole is partial, so that is what gets reported.
        var widest: Plan? = null

        // An address claimed by a second Wi-Fi network is probed on the first one
        // only, because the probe binds to a specific Network. Count those rather
        // than let the sweep call itself complete.
        var shared = 0

        for (target in targets) {
            if (!shouldContinue()) return Result(emptyList(), Scan.NotRun)
            val plan = subnetPlan(target)
            val previous = widest
            if (plan.narrowed && (previous == null || plan.subnetHosts > previous.subnetHosts)) {
                widest = plan
            }
            for (ip in plan.hosts) {
                if (ip in ownAddresses) continue
                val owner = candidates.putIfAbsent(ip, target.network)
                if (owner != null && owner != target.network) shared++
            }
        }

        val narrowed = widest
        val scan = when {
            candidates.isEmpty() -> Scan.NoAddresses
            narrowed != null ->
                Scan.Narrowed(candidates.size, narrowed.prefixLength, narrowed.subnetHosts)
            shared > 0 -> Scan.Overlapping(candidates.size, shared)
            else -> Scan.Full(candidates.size)
        }
        if (candidates.isEmpty()) return Result(emptyList(), scan)

        val executor = Executors.newFixedThreadPool(minOf(32, candidates.size))
        val completion = ExecutorCompletionService<Speaker?>(executor)
        return try {
            for ((ip, network) in candidates) {
                completion.submit(Callable {
                    if (probeWam(network, ip, SCAN_TIMEOUT_MS)) Speaker(ip, "LAN scan") else null
                })
            }

            val found = linkedMapOf<String, Speaker>()
            var remaining = candidates.size
            while (remaining > 0 && shouldContinue()) {
                val future = completion.poll(100, TimeUnit.MILLISECONDS) ?: continue
                remaining--
                val speaker = runCatching { future.get() }.getOrNull()
                if (speaker != null) found[speaker.ip] = speaker
            }
            Result(found.values.sortedBy { it.ip }, scan)
        } finally {
            executor.shutdownNow()
        }
    }

    /** The addresses one Wi-Fi address contributes, plus whether that is the whole subnet. */
    internal class Plan(
        val hosts: Sequence<String>,
        val narrowed: Boolean,
        val prefixLength: Int,
        val subnetHosts: Long,
    )

    private fun subnetPlan(target: WifiLan.Target): Plan =
        scanPlan(ipv4ToLong(target.address), target.prefixLength)

    /**
     * Which addresses to probe for one Wi-Fi address, expressed as plain integers.
     *
     * Split out from [subnetPlan] so the arithmetic can be tested without an
     * Android [Network]: the numbers are the part that is easy to get wrong
     * (the /30-/32 edges, the /22 threshold, a window that falls outside the
     * subnet), and none of them need a device to check.
     */
    internal fun scanPlan(address: Long, prefixLength: Int): Plan {
        val prefix = prefixLength.coerceIn(0, 32)
        val hostBits = 32 - prefix
        val addressCount = 1L shl hostBits
        val usableCount = when {
            prefix <= 30 -> (addressCount - 2).coerceAtLeast(0)
            else -> addressCount
        }
        if (usableCount == 0L) {
            return Plan(emptySequence(), narrowed = false, prefixLength = prefix, subnetHosts = 0L)
        }

        val mask = if (prefix == 0) 0L else (0xffff_ffffL shl hostBits) and 0xffff_ffffL
        val network = address and mask
        val broadcast = network + addressCount - 1
        val subnetFirst = if (prefix <= 30) network + 1 else network
        val subnetLast = if (prefix <= 30) broadcast - 1 else broadcast

        if (usableCount <= MAX_SCAN_HOSTS) {
            val hosts = (subnetFirst..subnetLast).asSequence().map(::longToIpv4)
            return Plan(hosts, narrowed = false, prefixLength = prefix, subnetHosts = usableCount)
        }

        // Do not spray tens of thousands of probes across a /16. On broad
        // Wi-Fi prefixes, keep the fallback useful by probing the local /24
        // window that contains this phone while SSDP remains the primary path.
        val localWindow = address and LOCAL_WINDOW_MASK
        val first = maxOf(subnetFirst, localWindow + 1)
        val last = minOf(subnetLast, localWindow + 254)
        val hosts = if (first <= last) {
            (first..last).asSequence().map(::longToIpv4)
        } else {
            emptySequence()
        }
        return Plan(hosts, narrowed = true, prefixLength = prefix, subnetHosts = usableCount)
    }

    internal fun ipv4ToLong(address: Inet4Address): Long = address.address.fold(0L) { result, byte ->
        (result shl 8) or (byte.toInt() and 0xff).toLong()
    }

    internal fun longToIpv4(value: Long): String = listOf(
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
