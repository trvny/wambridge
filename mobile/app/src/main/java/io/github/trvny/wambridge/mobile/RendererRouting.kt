package io.github.trvny.wambridge.mobile

import java.net.InetAddress

/** One reachable endpoint on the renderer's HTTP port. */
internal enum class RendererEndpoint {
    STREAM,
    DESCRIPTION,
    ICON,
    AV_TRANSPORT_SCPD,
    RENDERING_CONTROL_SCPD,
    CONNECTION_MANAGER_SCPD,
    AV_TRANSPORT_CONTROL,
    RENDERING_CONTROL_CONTROL,
    CONNECTION_MANAGER_CONTROL,
    SUBSCRIBE,
    UNSUBSCRIBE,
}

/** What a request is allowed to reach, before anything is written back. */
internal sealed interface RendererRoute {
    data class Allowed(val endpoint: RendererEndpoint) : RendererRoute

    /** 403, with the message the peer is told. */
    data class Denied(val message: String) : RendererRoute

    /** 404: the peer was allowed here, but there is nothing at that path. */
    object NotFound : RendererRoute
}

/**
 * Who may reach what on the renderer's HTTP port.
 *
 * The listener binds to the phone's Wi-Fi address, so every device on the
 * network can open a socket to it and the per-session stream path is only as
 * secret as a UUID that travels over plaintext UPnP. Two rules carry the whole
 * access decision, and the **order** between them is the property worth
 * protecting: the audio stream belongs to the speaker and to nobody else, and
 * everything else belongs to this phone and to nobody else - the speaker
 * included, since it has no business issuing UPnP control.
 *
 * It lives apart from [UpnpRenderer] so it can be checked without a phone, a
 * speaker or a socket. Sockets are where this used to be decided, which is why
 * nothing could test it.
 */
internal object RendererRouting {
    const val STREAM_DENIED = "Speaker stream only"
    const val CONTROL_DENIED = "Local control only"

    fun route(
        method: String,
        path: String,
        peer: InetAddress,
        streamPath: String,
        speakerIp: String,
        localAddress: InetAddress,
    ): RendererRoute {
        if (path == streamPath) {
            // Blank means discovery has not resolved a speaker yet. Compared as
            // strings this would still fail closed, but saying so explicitly keeps
            // an empty target from ever reading as a match.
            val fromSpeaker = speakerIp.isNotBlank() && peer.hostAddress == speakerIp
            return if (method == "GET" && fromSpeaker) {
                RendererRoute.Allowed(RendererEndpoint.STREAM)
            } else {
                RendererRoute.Denied(STREAM_DENIED)
            }
        }

        val local = peer.isLoopbackAddress || peer.hostAddress == localAddress.hostAddress
        if (!local) return RendererRoute.Denied(CONTROL_DENIED)

        val endpoint = when {
            method == "GET" && path == "/description.xml" -> RendererEndpoint.DESCRIPTION
            method == "GET" && path == "/icon.png" -> RendererEndpoint.ICON
            method == "GET" && path == "/upnp/avtransport.xml" -> RendererEndpoint.AV_TRANSPORT_SCPD
            method == "GET" && path == "/upnp/renderingcontrol.xml" ->
                RendererEndpoint.RENDERING_CONTROL_SCPD
            method == "GET" && path == "/upnp/connectionmanager.xml" ->
                RendererEndpoint.CONNECTION_MANAGER_SCPD
            method == "POST" && path == "/upnp/control/avtransport" ->
                RendererEndpoint.AV_TRANSPORT_CONTROL
            method == "POST" && path == "/upnp/control/renderingcontrol" ->
                RendererEndpoint.RENDERING_CONTROL_CONTROL
            method == "POST" && path == "/upnp/control/connectionmanager" ->
                RendererEndpoint.CONNECTION_MANAGER_CONTROL
            method == "SUBSCRIBE" && path.startsWith("/upnp/event/") -> RendererEndpoint.SUBSCRIBE
            method == "UNSUBSCRIBE" && path.startsWith("/upnp/event/") ->
                RendererEndpoint.UNSUBSCRIBE
            else -> return RendererRoute.NotFound
        }
        return RendererRoute.Allowed(endpoint)
    }
}
