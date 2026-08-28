package io.github.trvny.wambridge.mobile

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * Whether "Up" has to re-open the catalogue instead of climbing one level.
 *
 * `ascend` cannot leave the search tree - measured, along with three other
 * commands that also could not, see [SamsungCatalogue.open]. Only re-opening
 * crosses back, and from the top of a result list that is what "up" means anyway.
 */
internal fun catalogueUpNeedsReopen(inSearch: Boolean, atRoot: Boolean): Boolean =
    inSearch && atRoot

/**
 * Folds a freshly fetched page into what is already on screen.
 *
 * Paging appends so a long level reads as one list; everything else replaces.
 * The fresh page wins on all the level metadata, because it is the speaker's
 * account of where the cursor now is.
 */
internal fun mergedCataloguePage(
    previous: SamsungCatalogue.Page?,
    fresh: SamsungCatalogue.Page,
    append: Boolean,
): SamsungCatalogue.Page =
    if (append && previous != null) fresh.copy(entries = previous.entries + fresh.entries) else fresh

/**
 * Browses the speaker's own TuneIn catalogue.
 *
 * **The cursor lives in the speaker, not here.** Every call moves one shared
 * position, so this screen runs catalogue work on a single thread and refuses to
 * start a second request while one is in flight. Firing two would interleave on
 * the speaker and the answers would describe levels neither request asked for.
 *
 * For the same reason "Up" sends [SamsungCatalogue.ascend] instead of popping a
 * local stack: a stack would only describe where this screen believes it is.
 * The breadcrumb below is a display of what the speaker last reported, and is
 * rebuilt from its answers rather than maintained as truth.
 *
 * Playing needs no `GetStationData`: `mediaid` on a listed station is its TuneIn
 * id, and [RadioService] resolves that to a stream and relays it from the phone,
 * which is the road every other station here takes.
 */
class CatalogueActivity : Activity() {
    private lateinit var searchInput: EditText
    private lateinit var breadcrumbView: TextView
    private lateinit var statusView: TextView
    private lateinit var entriesView: LinearLayout
    private lateinit var upButton: Button
    private lateinit var moreButton: Button

    private var padding = 0

    /** Last page the speaker reported, or null before the first answer. */
    private var page: SamsungCatalogue.Page? = null

    /** Guards the speaker-side cursor: one request at a time, no queue. */
    private var busy = false

    /** Set while the cursor sits in the search tree, which needs a different way back. */
    private var inSearch = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        padding = (24 * resources.displayMetrics.density).toInt()
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        content.addView(TextView(this).apply {
            text = "TuneIn catalogue"
            textSize = 24f
        })
        content.addView(TextView(this).apply {
            text = "\nBrowsed from the speaker itself, so it shows what the M5 shows. " +
                "Tapping a station resolves it and plays it through this phone.\n"
            textSize = 14f
        })

        searchInput = EditText(this).apply {
            hint = "Search TuneIn"
            setSingleLine(true)
        }
        content.addView(searchInput)

        content.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(Button(this@CatalogueActivity).apply {
                text = "Search"
                setOnClickListener { runSearch() }
            })
            upButton = Button(this@CatalogueActivity).apply {
                text = "Up"
                setOnClickListener { goUp() }
            }
            addView(upButton)
            addView(Button(this@CatalogueActivity).apply {
                text = "Top"
                setOnClickListener { openRoot() }
            })
        })

        breadcrumbView = TextView(this).apply {
            textSize = 15f
            setPadding(0, padding / 2, 0, 0)
        }
        content.addView(breadcrumbView)

        statusView = TextView(this).apply {
            textSize = 15f
            setPadding(0, padding / 4, 0, padding / 4)
        }
        content.addView(statusView)

        entriesView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        content.addView(entriesView)

        moreButton = Button(this).apply {
            text = "Load more"
            setOnClickListener { loadMore() }
            visibility = View.GONE
        }
        content.addView(moreButton)

        setContentView(ScrollView(this).apply { addView(content) })
        openRoot()
    }

    // ---- speaker-side navigation -------------------------------------------------

    private fun openRoot() = onSpeaker("Opening the catalogue…") { context, ip ->
        inSearch = false
        SamsungCatalogue.open(context, ip)
    }

    private fun goUp() {
        if (catalogueUpNeedsReopen(inSearch, page?.isRoot != false)) {
            openRoot()
            return
        }
        onSpeaker("Going up…") { context, ip -> SamsungCatalogue.ascend(context, ip) }
    }

    private fun descend(entry: SamsungCatalogue.Entry) =
        onSpeaker("Opening ${entry.title}…") { context, ip ->
            SamsungCatalogue.descend(context, ip, entry.index)
        }

    private fun runSearch() {
        val query = searchInput.text.toString().trim()
        if (query.isEmpty()) {
            statusView.text = "Type something to search for."
            return
        }
        onSpeaker("Searching for $query…") { context, ip ->
            inSearch = true
            SamsungCatalogue.search(context, ip, query)
        }
    }

    private fun loadMore() {
        val current = page ?: return
        val next = current.startIndex + current.entries.size
        onSpeaker("Loading more…", append = true) { context, ip ->
            SamsungCatalogue.currentPage(context, ip, next)
        }
    }

    /**
     * Runs one catalogue call off the UI thread and renders whatever comes back.
     *
     * Refuses to start while another is in flight rather than queueing: a queued
     * call would be relative to a cursor the user has since moved.
     */
    private fun onSpeaker(
        message: String,
        append: Boolean = false,
        work: (Context, String) -> SamsungCatalogue.Page,
    ) {
        if (busy) {
            statusView.text = "Still working on the last request…"
            return
        }
        val target = speakerAddress() ?: run {
            statusView.text = "Configure the M5 address in WAM Bridge first."
            return
        }
        // The application context, not this activity: the request outlives a rotation
        // or a back press, and holding the activity from a background thread leaks it.
        val appContext = applicationContext
        busy = true
        statusView.text = message
        Thread({
            val result = runCatching { work(appContext, target) }
            runOnUiThread {
                busy = false
                result.fold(
                    onSuccess = { fresh ->
                        page = mergedCataloguePage(page, fresh, append)
                        render()
                    },
                    onFailure = { error ->
                        statusView.text =
                            error.message ?: "Catalogue request failed (${error.javaClass.simpleName})"
                    },
                )
            }
        }, "wam-catalogue").start()
    }

    // ---- rendering ---------------------------------------------------------------

    private fun render() {
        val current = page
        entriesView.removeAllViews()
        if (current == null) {
            statusView.text = "Nothing loaded."
            return
        }

        val where = current.category ?: current.root ?: SamsungCatalogue.BROWSE_ROOT
        val counted = current.total?.let { " — ${current.entries.size} of $it" }.orEmpty()
        breadcrumbView.text = where + counted
        upButton.isEnabled = !current.isRoot || inSearch
        moreButton.visibility =
            if (current.hasMore) View.VISIBLE else View.GONE

        if (current.entries.isEmpty()) {
            statusView.text = "This level is empty."
            return
        }
        statusView.text = ""

        current.entries.forEach { entry ->
            entriesView.addView(rowFor(entry))
        }
    }

    private fun rowFor(entry: SamsungCatalogue.Entry): LinearLayout {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, padding / 3, 0, padding / 3)
        }

        row.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(TextView(this@CatalogueActivity).apply {
                text = if (entry.isFolder) "▸  ${entry.title}" else entry.title
                textSize = 17f
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                // The whole title is the target for a folder; a station's title is
                // not tappable, so its Play button cannot be hit by accident.
                if (entry.isFolder) setOnClickListener { descend(entry) }
            })
            if (entry.isStation) {
                addView(Button(this@CatalogueActivity).apply {
                    text = "Play"
                    setOnClickListener { play(entry) }
                })
            }
        })

        // `description` is what the speaker shows under a station - usually its genre.
        entry.description?.takeIf { it.isNotBlank() }?.let { description ->
            row.addView(TextView(this).apply {
                text = description
                textSize = 12f
            })
        }
        return row
    }

    // ---- playing -----------------------------------------------------------------

    private fun play(entry: SamsungCatalogue.Entry) {
        val tuneInId = entry.mediaId
        if (tuneInId.isNullOrBlank()) {
            // isStation already excludes this, so reaching it means the listing
            // changed shape rather than that the user did anything wrong.
            statusView.text = "${entry.title} has no TuneIn id to play."
            return
        }
        startForegroundService(
            Intent(this, RadioService::class.java).apply {
                action = RadioService.ACTION_PLAY
                putExtra(RadioService.EXTRA_ALIAS, entry.title)
                putExtra(RadioService.EXTRA_TUNEIN_ID, tuneInId)
            },
        )
        statusView.text = "Starting ${entry.title}…"
        window.decorView.postDelayed({
            if (!busy) statusView.text = "● ${RadioService.lastStatus}"
        }, 1_200)
    }

    private fun speakerAddress(): String? {
        val target = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
            .getString(RendererService.KEY_SPEAKER_IP, "")
            .orEmpty()
            .trim()
        return if (RendererService.isReasonableIpv4(target)) target else null
    }
}
