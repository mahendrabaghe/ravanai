package ai.ravan.assistant

import android.Manifest
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.provider.Settings
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.net.URLEncoder
import java.util.Locale

/**
 * The only bridge between the web UI and the phone.
 *
 * Every method validates its own arguments even though the backend already
 * did, because the WebView is a separate trust boundary: a compromised page
 * must not be able to launch arbitrary packages, open non-https URLs, or send
 * a WhatsApp message. Nothing here reads contacts, files or messages, and
 * nothing can send a message — WhatsApp only ever receives a pre-filled
 * composer.
 */
@CapacitorPlugin(name = "PhoneActions")
class PhoneActionsPlugin : Plugin() {

    private val prefs: SharedPreferences
        get() = context.getSharedPreferences(FloatingOrbService.PREFS, Context.MODE_PRIVATE)

    /** The overlay writes a package-scoped broadcast; forward it to the page. */
    private val micReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context?, intent: Intent?) {
            val muted = intent?.getBooleanExtra(FloatingOrbService.EXTRA_MUTED, false) ?: false
            notifyListeners("micToggled", JSObject().put("muted", muted))
        }
    }

    override fun load() {
        ContextCompat.registerReceiver(
            context,
            micReceiver,
            IntentFilter(FloatingOrbService.ACTION_MIC_TOGGLED),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
    }

    override fun handleOnDestroy() {
        try {
            context.unregisterReceiver(micReceiver)
        } catch (_: Exception) {
            // Already unregistered, or the plugin never loaded.
        }
        super.handleOnDestroy()
    }

    // ── apps and links ──────────────────────────────────────────────────────

    @PluginMethod
    fun openApp(call: PluginCall) {
        val key = (call.getString("app") ?: "").lowercase(Locale.ROOT).trim()
        if (!LABELS.containsKey(key)) {
            call.reject("That app is not in the allowed list.")
            return
        }
        val label = LABELS.getValue(key)

        val intent: Intent? = when (key) {
            "settings" -> Intent(Settings.ACTION_SETTINGS)
            "camera" -> Intent(MediaStore.INTENT_ACTION_STILL_IMAGE_CAMERA)
            "downloads" -> Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
            else -> {
                val pkg = PACKAGES[key]
                if (pkg == null) null else launchIntentFor(pkg)
            }
        }

        if (intent == null) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "$label is not installed on this phone."))
            return
        }
        if (!fire(intent)) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "$label could not be opened on this phone."))
            return
        }
        call.resolve(JSObject().put("ok", true).put("detail", "Opened $label."))
    }

    @PluginMethod
    fun openUrl(call: PluginCall) {
        val raw = (call.getString("url") ?: "").trim()
        val uri = runCatching { Uri.parse(raw) }.getOrNull()
        if (uri == null || uri.scheme?.lowercase(Locale.ROOT) != "https" || uri.host.isNullOrBlank()) {
            call.reject("Only absolute https links can be opened.")
            return
        }
        if (!fire(Intent(Intent.ACTION_VIEW, uri))) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "No app on this phone could open that link."))
            return
        }
        call.resolve(JSObject().put("ok", true)
            .put("detail", "Opened the link in the browser."))
    }

    @PluginMethod
    fun openYouTubeSearch(call: PluginCall) {
        val query = (call.getString("query") ?: "").trim()
        if (query.isEmpty() || query.length > 160) {
            call.reject("A YouTube search needs 1-160 characters.")
            return
        }
        val url = "https://www.youtube.com/results?search_query=" +
            URLEncoder.encode(query, "UTF-8")
        val uri = Uri.parse(url)
        val installed = launchIntentFor(YOUTUBE) != null

        var opened = false
        if (installed) {
            opened = fire(Intent(Intent.ACTION_VIEW, uri).setPackage(YOUTUBE))
        }
        if (!opened) opened = fire(Intent(Intent.ACTION_VIEW, uri))

        if (!opened) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "No app on this phone could open that YouTube search."))
            return
        }
        call.resolve(JSObject().put("ok", true)
            .put("detail", "YouTube search opened for “$query”. Playback starts when you pick a result."))
    }

    @PluginMethod
    fun composeWhatsApp(call: PluginCall) {
        val message = (call.getString("message") ?: "").trim()
        if (message.isEmpty() || message.length > 1000) {
            call.reject("A WhatsApp message needs 1-1000 characters.")
            return
        }
        if (launchIntentFor(WHATSAPP) == null) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "WhatsApp is not installed on this phone."))
            return
        }

        val digits = (call.getString("contact") ?: "").filter { it.isDigit() }
        val hasNumber = digits.length in 7..15
        val text = URLEncoder.encode(message, "UTF-8")
        val uri = Uri.parse(
            if (hasNumber) "https://wa.me/$digits?text=$text" else "https://wa.me/?text=$text"
        )

        var opened = fire(Intent(Intent.ACTION_VIEW, uri).setPackage(WHATSAPP))
        if (!opened) opened = fire(Intent(Intent.ACTION_VIEW, uri))
        if (!opened) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "WhatsApp could not be opened."))
            return
        }

        // The composer is the whole action. Sending is a tap the user makes,
        // and RAVAN has no way to observe it, so it never claims otherwise.
        val detail = if (hasNumber) {
            "WhatsApp opened the chat for that number with the message ready. You press send; nothing was sent."
        } else {
            "WhatsApp opened the contact picker with the message ready. You choose the contact and press send; nothing was sent."
        }
        call.resolve(JSObject().put("ok", true).put("detail", detail))
    }

    // ── floating orb ────────────────────────────────────────────────────────

    @PluginMethod
    fun setFloatingIcon(call: PluginCall) {
        val enabled = call.getBoolean("enabled") ?: false
        prefs.edit().putBoolean(FloatingOrbService.KEY_ENABLED, enabled).apply()

        if (!enabled) {
            context.stopService(Intent(context, FloatingOrbService::class.java))
            call.resolve(JSObject().put("ok", true).put("enabled", false))
            return
        }

        // Overlay permission can only be granted in system settings, so the
        // request is transparent: the sheet tells the user why before this runs.
        if (!Settings.canDrawOverlays(context)) {
            prefs.edit().putBoolean(FloatingOrbService.KEY_ENABLED, false).apply()
            requestOverlayPermission()
            call.resolve(JSObject().put("ok", false)
                .put("permissionRequired", true)
                .put("detail", "Allow “display over other apps” for RAVAN, then try again."))
            return
        }

        requestNotificationPermission()
        val started = try {
            ContextCompat.startForegroundService(
                context,
                Intent(context, FloatingOrbService::class.java)
                    .setAction(FloatingOrbService.ACTION_SHOW)
            )
            true
        } catch (_: Exception) {
            false
        }
        if (!started) {
            call.resolve(JSObject().put("ok", false)
                .put("detail", "The floating icon could not start on this phone."))
            return
        }
        call.resolve(JSObject().put("ok", true).put("enabled", true))
    }

    @PluginMethod
    fun getFloatingState(call: PluginCall) {
        call.resolve(
            JSObject()
                .put("enabled", FloatingOrbService.isRunning)
                .put("muted", prefs.getBoolean(FloatingOrbService.KEY_MUTED, false))
                .put("canDraw", Settings.canDrawOverlays(context))
        )
    }

    @PluginMethod
    fun setMicMuted(call: PluginCall) {
        val muted = call.getBoolean("muted") ?: false
        prefs.edit().putBoolean(FloatingOrbService.KEY_MUTED, muted).apply()
        if (FloatingOrbService.isRunning) {
            try {
                context.startService(
                    Intent(context, FloatingOrbService::class.java)
                        .setAction(FloatingOrbService.ACTION_SET_MUTED)
                        .putExtra(FloatingOrbService.EXTRA_MUTED, muted)
                )
            } catch (_: Exception) {
                // The in-app state stays authoritative if the service died.
            }
        }
        call.resolve(JSObject().put("ok", true).put("muted", muted))
    }

    @PluginMethod
    fun exitApp(call: PluginCall) {
        context.stopService(Intent(context, FloatingOrbService::class.java))
        call.resolve(JSObject().put("ok", true))
        activity?.finishAndRemoveTask()
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun launchIntentFor(pkg: String): Intent? = try {
        context.packageManager.getLaunchIntentForPackage(pkg)
    } catch (_: Exception) {
        null
    }

    /** Launch with the app's own window task; false if nothing handled it. */
    private fun fire(intent: Intent): Boolean = try {
        val act = activity
        if (act == null) intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        (act ?: context).startActivity(intent)
        true
    } catch (_: Exception) {
        false
    }

    private fun requestOverlayPermission() {
        val intent = Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:${context.packageName}")
        )
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            context.startActivity(intent)
        } catch (_: Exception) {
            // Some OEM builds hide the screen; the toast still explains it.
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val act = activity ?: return
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) return
        try {
            ActivityCompat.requestPermissions(
                act, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 9101
            )
        } catch (_: Exception) {
            // The orb still runs; only its notification is quieter.
        }
    }

    companion object {
        private const val YOUTUBE = "com.google.android.youtube"
        private const val WHATSAPP = "com.whatsapp"

        // Keep identical to APP_ALLOWLIST in server/app/tools.py. The label is
        // only for the user-facing result; the packages are what get launched.
        private val PACKAGES = mapOf(
            "chrome" to "com.android.chrome",
            "youtube" to YOUTUBE,
            "whatsapp" to WHATSAPP,
            "maps" to "com.google.android.apps.maps",
            "gmail" to "com.google.android.gm",
            "spotify" to "com.spotify.music",
            "instagram" to "com.instagram.android",
            "telegram" to "org.telegram.messenger"
        )

        private val LABELS = mapOf(
            "chrome" to "Chrome",
            "youtube" to "YouTube",
            "whatsapp" to "WhatsApp",
            "settings" to "Settings",
            "downloads" to "Downloads",
            "camera" to "Camera",
            "maps" to "Maps",
            "gmail" to "Gmail",
            "spotify" to "Spotify",
            "instagram" to "Instagram",
            "telegram" to "Telegram",
            "calculator" to "Calculator",
            "clock" to "Clock"
        )
    }
}
