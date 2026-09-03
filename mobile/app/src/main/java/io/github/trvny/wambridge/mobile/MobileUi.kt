package io.github.trvny.wambridge.mobile

import android.app.Activity
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

internal object MobileUi {
    enum class ButtonKind { PRIMARY, SECONDARY, QUIET, DANGER }

    fun applyWindow(activity: Activity) {
        activity.window.statusBarColor = activity.getColor(R.color.wam_background)
        activity.window.navigationBarColor = activity.getColor(R.color.wam_background)
        activity.window.decorView.systemUiVisibility =
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
    }

    fun page(context: Context): LinearLayout = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(context, 20), dp(context, 18), dp(context, 20), dp(context, 32))
        setBackgroundColor(context.getColor(R.color.wam_background))
    }

    fun header(context: Context, title: String, subtitle: String): LinearLayout =
        LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(context).apply {
                text = title
                textSize = 30f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(context.getColor(R.color.wam_text))
            })
            addView(TextView(context).apply {
                text = subtitle
                textSize = 14f
                setTextColor(context.getColor(R.color.wam_muted))
                setPadding(0, dp(context, 4), 0, dp(context, 18))
            })
        }

    fun sectionTitle(context: Context, text: String): TextView = TextView(context).apply {
        this.text = text
        textSize = 18f
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(context.getColor(R.color.wam_text))
        setPadding(dp(context, 2), dp(context, 22), 0, dp(context, 10))
    }

    fun card(context: Context): LinearLayout = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(context, 16), dp(context, 16), dp(context, 16), dp(context, 16))
        background = rounded(
            context,
            fill = context.getColor(R.color.wam_surface),
            stroke = context.getColor(R.color.wam_border),
            radiusDp = 20,
        )
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { bottomMargin = dp(context, 12) }
    }

    fun status(context: Context, text: String = ""): TextView = TextView(context).apply {
        this.text = text
        textSize = 14f
        setTextColor(context.getColor(R.color.wam_text))
        setPadding(dp(context, 12), dp(context, 10), dp(context, 12), dp(context, 10))
        background = rounded(
            context,
            fill = context.getColor(R.color.wam_accent_soft),
            stroke = context.getColor(R.color.wam_accent_soft),
            radiusDp = 14,
        )
    }

    fun body(context: Context, text: String): TextView = TextView(context).apply {
        this.text = text
        textSize = 14f
        setTextColor(context.getColor(R.color.wam_muted))
        setLineSpacing(0f, 1.08f)
    }

    fun label(context: Context, text: String): TextView = TextView(context).apply {
        this.text = text
        textSize = 13f
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(context.getColor(R.color.wam_muted))
        setPadding(dp(context, 2), 0, 0, dp(context, 6))
    }

    fun field(context: Context, hint: String, multiline: Boolean = false): EditText =
        EditText(context).apply {
            this.hint = hint
            textSize = 15f
            setTextColor(context.getColor(R.color.wam_text))
            setHintTextColor(context.getColor(R.color.wam_muted))
            setPadding(dp(context, 14), dp(context, 12), dp(context, 14), dp(context, 12))
            background = rounded(
                context,
                fill = context.getColor(R.color.wam_background),
                stroke = context.getColor(R.color.wam_border),
                radiusDp = 14,
            )
            minHeight = dp(context, if (multiline) 104 else 52)
        }

    fun button(
        context: Context,
        text: String,
        kind: ButtonKind = ButtonKind.SECONDARY,
        click: () -> Unit,
    ): Button = Button(context).apply {
        this.text = text
        isAllCaps = false
        textSize = 14f
        typeface = Typeface.DEFAULT_BOLD
        minHeight = dp(context, 48)
        minWidth = 0
        val (fill, ink, stroke) = when (kind) {
            ButtonKind.PRIMARY -> Triple(R.color.wam_accent, android.R.color.white, R.color.wam_accent)
            ButtonKind.SECONDARY -> Triple(R.color.wam_surface_alt, R.color.wam_text, R.color.wam_surface_alt)
            ButtonKind.QUIET -> Triple(R.color.wam_surface, R.color.wam_text, R.color.wam_border)
            ButtonKind.DANGER -> Triple(R.color.wam_danger_soft, R.color.wam_danger, R.color.wam_danger_soft)
        }
        background = RippleDrawable(
            ColorStateList.valueOf(Color.argb(24, 0, 0, 0)),
            rounded(context, context.getColor(fill), context.getColor(stroke), 14),
            null,
        )
        setTextColor(context.getColor(ink))
        elevation = 0f
        stateListAnimator = null
        setPadding(dp(context, 10), 0, dp(context, 10), 0)
        setOnClickListener { click() }
    }

    fun row(context: Context): LinearLayout = LinearLayout(context).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    fun addWeighted(row: LinearLayout, view: View, weight: Float = 1f, marginDp: Int = 6) {
        row.addView(
            view,
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, weight).apply {
                marginEnd = dp(row.context, marginDp)
            },
        )
    }

    fun rounded(context: Context, fill: Int, stroke: Int, radiusDp: Int): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(context, radiusDp).toFloat()
            setColor(fill)
            setStroke(dp(context, 1), stroke)
        }

    fun dp(context: Context, value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()
}
