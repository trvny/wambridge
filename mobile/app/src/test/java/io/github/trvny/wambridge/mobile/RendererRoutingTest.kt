package io.github.trvny.wambridge.mobile

import java.net.InetAddress
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Who the renderer's HTTP port answers.
 *
 * The listener sits on the phone's Wi-Fi address, so the whole network can
 * reach it and the per-session stream path travels in plaintext UPnP. What
 * keeps that harmless is two rules and the order between them, which is
 * exactly what these cases pin down. None of it needs a phone or a speaker.
 */
class RendererRoutingTest {
    private val phone: InetAddress = InetAddress.getByName("192.168.1.20")
    private val speaker = "192.168.1.50"
    private val stranger: InetAddress = InetAddress.getByName("192.168.1.99")
    private val streamPath = "/stream/0123456789abcdef0123456789abcdef"

    private fun route(
        method: String,
        path: String,
        peer: InetAddress,
        speakerIp: String = speaker,
    ): RendererRoute = RendererRouting.route(
        method = method,
        path = path,
        peer = peer,
        streamPath = streamPath,
        speakerIp = speakerIp,
        localAddress = phone,
    )

    @Test
    fun `the speaker gets the audio stream`() {
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.STREAM),
            route("GET", streamPath, InetAddress.getByName(speaker)),
        )
    }

    @Test
    fun `anyone else guessing the stream path is refused`() {
        assertEquals(
            RendererRoute.Denied(RendererRouting.STREAM_DENIED),
            route("GET", streamPath, stranger),
        )
    }

    @Test
    fun `the phone itself may not pull the stream either`() {
        // The phone is the source of the audio, never a consumer of it. Letting
        // the local-control rule spill onto the stream path would be a widening
        // nobody asked for.
        assertEquals(
            RendererRoute.Denied(RendererRouting.STREAM_DENIED),
            route("GET", streamPath, phone),
        )
    }

    @Test
    fun `an unresolved speaker matches nobody`() {
        assertEquals(
            RendererRoute.Denied(RendererRouting.STREAM_DENIED),
            route("GET", streamPath, stranger, speakerIp = ""),
        )
    }

    @Test
    fun `the stream path takes only GET`() {
        assertEquals(
            RendererRoute.Denied(RendererRouting.STREAM_DENIED),
            route("POST", streamPath, InetAddress.getByName(speaker)),
        )
    }

    @Test
    fun `the phone reaches the description and the control endpoints`() {
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.DESCRIPTION),
            route("GET", "/description.xml", phone),
        )
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.AV_TRANSPORT_CONTROL),
            route("POST", "/upnp/control/avtransport", phone),
        )
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.RENDERING_CONTROL_CONTROL),
            route("POST", "/upnp/control/renderingcontrol", phone),
        )
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.CONNECTION_MANAGER_CONTROL),
            route("POST", "/upnp/control/connectionmanager", phone),
        )
    }

    @Test
    fun `loopback counts as this phone`() {
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.DESCRIPTION),
            route("GET", "/description.xml", InetAddress.getByName("127.0.0.1")),
        )
    }

    @Test
    fun `the speaker gets no control, only its stream`() {
        assertEquals(
            RendererRoute.Denied(RendererRouting.CONTROL_DENIED),
            route("POST", "/upnp/control/avtransport", InetAddress.getByName(speaker)),
        )
        assertEquals(
            RendererRoute.Denied(RendererRouting.CONTROL_DENIED),
            route("GET", "/description.xml", InetAddress.getByName(speaker)),
        )
    }

    @Test
    fun `a stranger is refused before the path is even looked at`() {
        // Denied, not NotFound: an unknown path from a stranger must not tell
        // them which paths exist.
        assertEquals(
            RendererRoute.Denied(RendererRouting.CONTROL_DENIED),
            route("GET", "/no/such/thing", stranger),
        )
    }

    @Test
    fun `event subscriptions are matched by prefix`() {
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.SUBSCRIBE),
            route("SUBSCRIBE", "/upnp/event/avtransport", phone),
        )
        assertEquals(
            RendererRoute.Allowed(RendererEndpoint.UNSUBSCRIBE),
            route("UNSUBSCRIBE", "/upnp/event/renderingcontrol", phone),
        )
    }

    @Test
    fun `an unknown path from the phone is a plain 404`() {
        assertEquals(RendererRoute.NotFound, route("GET", "/no/such/thing", phone))
        assertEquals(RendererRoute.NotFound, route("PUT", "/description.xml", phone))
    }
}
