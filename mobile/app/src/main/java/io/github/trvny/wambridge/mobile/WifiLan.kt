package io.github.trvny.wambridge.mobile

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL

internal object WifiLan {
    data class Target(
        val network: Network,
        val address: Inet4Address,
        val prefixLength: Int,
    )

    fun targets(context: Context): List<Target> {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val result = mutableListOf<Target>()

        for (network in connectivity.allNetworks) {
            val capabilities = connectivity.getNetworkCapabilities(network) ?: continue
            if (!capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            val properties = connectivity.getLinkProperties(network) ?: continue

            for (linkAddress in properties.linkAddresses) {
                val address = linkAddress.address as? Inet4Address ?: continue
                if (address.isLoopbackAddress || address.isLinkLocalAddress) continue
                val target = Target(network, address, linkAddress.prefixLength)
                if (result.none { it.network == network && it.address == address }) result += target
            }
        }
        return result
    }

    fun connectSocket(context: Context, host: String, port: Int, timeoutMs: Int): Socket {
        var lastError: Exception? = null
        for (target in targets(context)) {
            val socket = Socket()
            try {
                target.network.bindSocket(socket)
                socket.connect(InetSocketAddress(host, port), timeoutMs)
                return socket
            } catch (error: Exception) {
                lastError = error
                runCatching { socket.close() }
            }
        }
        throw lastError ?: IllegalStateException("No active Wi-Fi network")
    }

    fun openHttpConnections(context: Context, url: URL): Sequence<HttpURLConnection> = sequence {
        for (target in targets(context)) {
            val connection = target.network.openConnection(url) as HttpURLConnection
            yield(connection)
        }
    }
}
