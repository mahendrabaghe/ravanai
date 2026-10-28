package ai.ravan.assistant

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.os.Build
import android.os.IBinder
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import androidx.core.app.NotificationCompat
import kotlin.math.abs
import kotlin.math.sin

/**
 * A compact live orb drawn over other apps.
 *
 * This is a real native window (TYPE_APPLICATION_OVERLAY), not a WebView: the
 * main app can be closed and the orb keeps breathing without keeping an entire
 * page alive. It carries exactly the two controls the brief asks for — a mic
 * badge that mutes/unmutes and a close badge that hides it — plus drag and tap
 * to reopen RAVAN.
 *
 * It is deliberately not an accessibility service and reads nothing on screen.
 */
class FloatingOrbService : Service() {

    private var windowManager: WindowManager? = null
    private var orb: OrbView? = null
    private var params: WindowManager.LayoutParams? = null
    private lateinit var prefs: SharedPreferences

    override fun onCreate() {
        super.onCreate()
        isRunning = true
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        createChannel()
        startForegroundCompat()
        showOrb()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_HIDE -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_SET_MUTED -> setMuted(intent.getBooleanExtra(EXTRA_MUTED, false), false)
            ACTION_TOGGLE_MUTED -> setMuted(!prefs.getBoolean(KEY_MUTED, false), true)
        }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        val view = orb ?: return
        val lp = params ?: return
        clampPosition(lp, view.width)
        try {
            windowManager?.updateViewLayout(view, lp)
        } catch (_: Exception) {
            // The window was detached underneath us; the next show re-adds it.
        }
    }

    override fun onDestroy() {
        isRunning = false
        orb?.let { view ->
            try {
                windowManager?.removeView(view)
            } catch (_: Exception) {
                // Already gone.
            }
        }
        orb = null
        params = null
        prefs.edit().putBoolean(KEY_ENABLED, false).apply()
        super.onDestroy()
    }

    // ── window ──────────────────────────────────────────────────────────────

    private fun showOrb() {
        if (orb != null) return
        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val view = OrbView(this)
        val metrics = resources.displayMetrics
        val size = (VIEW_DP * metrics.density).toInt()

        @Suppress("DEPRECATION")
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else WindowManager.LayoutParams.TYPE_PHONE

        val lp = WindowManager.LayoutParams(
            size,
            size,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        )
        lp.gravity = Gravity.TOP or Gravity.START
        lp.x = prefs.getInt(KEY_X, metrics.widthPixels - size)
        lp.y = prefs.getInt(KEY_Y, metrics.heightPixels / 3)
        clampPosition(lp, size)

        view.muted = prefs.getBoolean(KEY_MUTED, false)
        view.onTap = { openApp() }
        view.onToggleMute = { setMuted(!prefs.getBoolean(KEY_MUTED, false), true) }
        view.onClose = { stopSelf() }
        view.onMove = { dx, dy -> moveBy(dx, dy) }
        view.onDrop = { savePosition() }

        try {
            wm.addView(view, lp)
            windowManager = wm
            orb = view
            params = lp
        } catch (_: Exception) {
            // Permission revoked between the check and this call, or the OEM
            // refuses the window. Leave the pref honest and stop.
            prefs.edit().putBoolean(KEY_ENABLED, false).apply()
            stopSelf()
        }
    }

    private fun clampPosition(lp: WindowManager.LayoutParams, size: Int = -1) {
        val metrics = resources.displayMetrics
        val w = if (size > 0) size else dp(VIEW_DP)
        lp.x = lp.x.coerceIn(0, (metrics.widthPixels - w).coerceAtLeast(0))
        lp.y = lp.y.coerceIn(0, (metrics.heightPixels - w).coerceAtLeast(0))
    }

    private fun moveBy(dx: Float, dy: Float) {
        val view = orb ?: return
        val lp = params ?: return
        val metrics = resources.displayMetrics
        val maxX = (metrics.widthPixels - view.width).coerceAtLeast(0)
        val maxY = (metrics.heightPixels - view.height).coerceAtLeast(0)
        lp.x = (lp.x + dx.toInt()).coerceIn(0, maxX)
        lp.y = (lp.y + dy.toInt()).coerceIn(0, maxY)
        try {
            windowManager?.updateViewLayout(view, lp)
        } catch (_: Exception) {
            // Keep the in-memory position; a later frame retries.
        }
    }

    private fun savePosition() {
        val lp = params ?: return
        prefs.edit().putInt(KEY_X, lp.x).putInt(KEY_Y, lp.y).apply()
    }

    private fun openApp() {
        val intent = packageManager.getLaunchIntentForPackage(packageName) ?: return
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        try {
            startActivity(intent)
        } catch (_: Exception) {
            // Nothing to recover: the launcher entry is missing.
        }
    }

    // ── mute ────────────────────────────────────────────────────────────────

    private fun setMuted(muted: Boolean, notifyApp: Boolean) {
        prefs.edit().putBoolean(KEY_MUTED, muted).apply()
        orb?.setMuted(muted)
        if (notifyApp) {
            sendBroadcast(
                Intent(ACTION_MIC_TOGGLED)
                    .setPackage(packageName)
                    .putExtra(EXTRA_MUTED, muted)
            )
        }
    }

    // ── notification ────────────────────────────────────────────────────────

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL,
            "Floating RAVAN",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Keeps the floating orb available over other apps."
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    private fun startForegroundCompat() {
        val open = PendingIntent.getActivity(
            this,
            1,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val hide = PendingIntent.getService(
            this,
            2,
            Intent(this, FloatingOrbService::class.java).setAction(ACTION_HIDE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_ravan_notification)
            .setContentTitle("RAVAN is floating")
            .setContentText("Tap the orb to open RAVAN. The badges mute the mic or hide it.")
            .setContentIntent(open)
            .addAction(0, "Open", open)
            .addAction(0, "Hide", hide)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private fun dp(value: Float): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        const val PREFS = "ravan.prefs"
        const val KEY_ENABLED = "floating_enabled"
        const val KEY_MUTED = "mic_muted"
        private const val KEY_X = "orb_x"
        private const val KEY_Y = "orb_y"

        const val ACTION_SHOW = "ai.ravan.assistant.action.SHOW_ORB"
        const val ACTION_HIDE = "ai.ravan.assistant.action.HIDE_ORB"
        const val ACTION_SET_MUTED = "ai.ravan.assistant.action.SET_MUTED"
        const val ACTION_TOGGLE_MUTED = "ai.ravan.assistant.action.TOGGLE_MUTED"
        const val ACTION_MIC_TOGGLED = "ai.ravan.assistant.MIC_TOGGLED"
        const val EXTRA_MUTED = "muted"

        private const val CHANNEL = "ravan_overlay"
        private const val NOTIF_ID = 4201
        private const val VIEW_DP = 76f

        @Volatile
        var isRunning = false
    }
}

/**
 * The orb itself. Drawn in Canvas rather than shipped as a WebView so the
 * overlay stays a few hundred kilobytes and animates without a page engine.
 */
internal class OrbView(context: Context) : View(context) {

    var onTap: (() -> Unit)? = null
    var onToggleMute: (() -> Unit)? = null
    var onClose: (() -> Unit)? = null
    var onMove: ((Float, Float) -> Unit)? = null
    var onDrop: (() -> Unit)? = null

    private val density = resources.displayMetrics.density
    private val orbR = dp(25f)
    private val micR = dp(11f)
    private val closeR = dp(8.5f)
    private val slop = dp(6f)

    private var phase = 0f
    private var mutedState = false

    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val spherePaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val scanPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.1f)
    }
    private val corePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFFE8FCFF.toInt()
    }
    private val badgeFill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val badgeStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.1f)
    }
    private val glyphFill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glyphStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.4f)
        strokeCap = Paint.Cap.ROUND
        color = 0xFFE8F6FF.toInt()
    }
    private val slashPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
        strokeCap = Paint.Cap.ROUND
        color = 0xFFFF5C7A.toInt()
    }
    private val rect = RectF()

    private var downRawX = 0f
    private var downRawY = 0f
    private var startRawX = 0f
    private var startRawY = 0f
    private var moved = false

    private val ticker = object : Runnable {
        override fun run() {
            phase += 0.012f
            if (phase > 1f) phase -= 1f
            invalidate()
            // 30 fps is smooth for a 50 dp orb and measurably gentler on the
            // battery than redrawing at the panel's 120 Hz.
            postDelayed(this, 33L)
        }
    }

    init {
        isClickable = true
        contentDescription = "RAVAN floating icon"
    }

    fun setMuted(muted: Boolean) {
        if (mutedState == muted) return
        mutedState = muted
        invalidate()
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        postDelayed(ticker, 16L)
    }

    override fun onDetachedFromWindow() {
        removeCallbacks(ticker)
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val cx = width / 2f
        val cy = height / 2f

        glowPaint.shader = RadialGradient(
            cx, cy, orbR * 1.9f,
            intArrayOf(0x5500D4FF.toInt(), 0x0000D4FF),
            null, Shader.TileMode.CLAMP
        )
        canvas.drawCircle(cx, cy, orbR * 1.9f, glowPaint)

        spherePaint.shader = RadialGradient(
            cx - orbR * 0.3f, cy - orbR * 0.35f, orbR * 1.5f,
            intArrayOf(0xFFB9F4FF.toInt(), 0xFF22A9CC.toInt(), 0xFF041722.toInt()),
            floatArrayOf(0f, 0.55f, 1f), Shader.TileMode.CLAMP
        )
        canvas.drawCircle(cx, cy, orbR, spherePaint)

        // The scan band is the same holographic tell as the full-size head.
        canvas.save()
        val clip = Path().apply { addCircle(cx, cy, orbR, Path.Direction.CW) }
        canvas.clipPath(clip)
        val y = cy + sin((phase * 2.0 * Math.PI).toFloat()) * orbR * 0.62f
        scanPaint.shader = LinearGradient(
            0f, y - orbR * 0.2f, 0f, y + orbR * 0.2f,
            intArrayOf(0x00FFFFFF, 0x5CFFFFFF, 0x00FFFFFF),
            null, Shader.TileMode.CLAMP
        )
        canvas.drawRect(cx - orbR, y - orbR * 0.2f, cx + orbR, y + orbR * 0.2f, scanPaint)
        canvas.restore()

        ringPaint.color = 0x9900D4FF.toInt()
        canvas.save()
        canvas.rotate(phase * 360f, cx, cy)
        canvas.drawOval(cx - orbR * 1.16f, cy - orbR * 0.4f, cx + orbR * 1.16f, cy + orbR * 0.4f, ringPaint)
        canvas.restore()

        ringPaint.color = 0x556F7DFF
        canvas.save()
        canvas.rotate(-phase * 240f, cx, cy)
        canvas.drawOval(cx - orbR * 0.86f, cy - orbR * 0.86f, cx + orbR * 0.86f, cy + orbR * 0.86f, ringPaint)
        canvas.restore()

        canvas.drawCircle(cx, cy, orbR * 0.15f, corePaint)

        val micX = cx + orbR * 0.80f
        val micY = cy + orbR * 0.80f
        badgeFill.color = 0xEE07131C.toInt()
        canvas.drawCircle(micX, micY, micR, badgeFill)
        badgeStroke.color = if (mutedState) 0xFFFF5C7A.toInt() else 0xFF00D4FF.toInt()
        canvas.drawCircle(micX, micY, micR, badgeStroke)

        glyphFill.color = if (mutedState) 0xFFFF8FA4.toInt() else 0xFFBFF2FF.toInt()
        rect.set(micX - dp(2.1f), micY - dp(4.6f), micX + dp(2.1f), micY + dp(0.8f))
        canvas.drawRoundRect(rect, dp(2.1f), dp(2.1f), glyphFill)
        rect.set(micX - dp(3.3f), micY - dp(2.6f), micX + dp(3.3f), micY + dp(3.0f))
        glyphStroke.color = glyphFill.color
        canvas.drawArc(rect, 0f, 180f, false, glyphStroke)
        canvas.drawLine(micX, micY + dp(3.0f), micX, micY + dp(4.6f), glyphStroke)
        if (mutedState) {
            canvas.drawLine(micX - dp(4.6f), micY + dp(4.6f), micX + dp(4.6f), micY - dp(4.6f), slashPaint)
        }

        val closeX = cx - orbR * 0.80f
        val closeY = cy - orbR * 0.80f
        badgeFill.color = 0xBB07131C.toInt()
        canvas.drawCircle(closeX, closeY, closeR, badgeFill)
        badgeStroke.color = 0x55FFFFFF
        canvas.drawCircle(closeX, closeY, closeR, badgeStroke)
        glyphStroke.color = 0xAAE8F6FF.toInt()
        canvas.drawLine(closeX - dp(2.2f), closeY - dp(2.2f), closeX + dp(2.2f), closeY + dp(2.2f), glyphStroke)
        canvas.drawLine(closeX + dp(2.2f), closeY - dp(2.2f), closeX - dp(2.2f), closeY + dp(2.2f), glyphStroke)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                startRawX = event.rawX
                startRawY = event.rawY
                downRawX = event.rawX
                downRawY = event.rawY
                moved = false
                return true
            }
            MotionEvent.ACTION_MOVE -> {
                if (!moved) {
                    moved = abs(event.rawX - startRawX) > slop || abs(event.rawY - startRawY) > slop
                }
                if (moved) {
                    onMove?.invoke(event.rawX - downRawX, event.rawY - downRawY)
                    downRawX = event.rawX
                    downRawY = event.rawY
                }
                return true
            }
            MotionEvent.ACTION_UP -> {
                if (moved) {
                    onDrop?.invoke()
                } else {
                    val cx = width / 2f
                    val cy = height / 2f
                    val micX = cx + orbR * 0.80f
                    val micY = cy + orbR * 0.80f
                    val closeX = cx - orbR * 0.80f
                    val closeY = cy - orbR * 0.80f
                    val x = event.x
                    val y = event.y
                    when {
                        near(x, y, micX, micY, micR * 1.6f) -> onToggleMute?.invoke()
                        near(x, y, closeX, closeY, closeR * 1.9f) -> onClose?.invoke()
                        else -> onTap?.invoke()
                    }
                }
                return true
            }
            MotionEvent.ACTION_CANCEL -> {
                if (moved) onDrop?.invoke()
                return true
            }
        }
        return false
    }

    private fun near(x: Float, y: Float, cx: Float, cy: Float, r: Float): Boolean {
        val dx = x - cx
        val dy = y - cy
        return dx * dx + dy * dy <= r * r
    }

    private fun dp(value: Float): Float = value * density
}
