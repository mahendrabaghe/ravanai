"""Compact always-on-top assistant orb.

This is a *view* over the same MainWindow/JarvisLive session, not a second
assistant. It owns no microphone, no speech engine and no conversation state;
it only shows/hides the window it belongs to and forwards the shared controls.

The orb is deliberately independent of the main window's lifecycle: showing or
hiding one never touches the other. Its animation timer only runs while the
orb itself is visible, so a hidden icon costs nothing.
"""
from __future__ import annotations

import math
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QMenu, QWidget

_CYAN = QColor(0, 212, 255)
_VIOLET = QColor(138, 92, 255)
_MUTED = QColor(255, 51, 102)
_GREEN = QColor(0, 255, 136)


class FloatingOrb(QWidget):
    """A 48 px holographic core inside a compact 64 px hit area.

    Left-click toggles the main window, dragging moves the orb, right-click
    opens the shared controls, and the mic badge in the corner mutes or
    unmutes the *same* microphone the main window uses — it is the same signal
    the command dock's MIC button fires, not a second audio path.
    """

    activated              = pyqtSignal()   # click: toggle the main window
    mute_toggled           = pyqtSignal()
    main_toggle_requested  = pyqtSignal()
    stop_speaking_requested = pyqtSignal()
    hide_icon_requested    = pyqtSignal()
    quit_requested         = pyqtSignal()
    moved                  = pyqtSignal(int, int)

    SIZE = 64            # window / drag hit area
    CORE = 48            # visible orb diameter (within the 40–52 px target)
    DOT = 10             # mic badge diameter
    DOT_HIT = 12.0       # generous click radius around the badge centre

    def __init__(self, image_path: str | Path | None = None, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setWindowTitle("RAVAN")
        self.setAccessibleName("RAVAN floating assistant")
        self.setAccessibleDescription(
            "Assistant orb. Click to show or hide the main window. "
            "Click the microphone badge to mute or unmute the microphone.")

        self._muted = False
        self._set_tooltip()
        self._speaking = False
        self._main_visible = True
        self._hover = False
        self._dot_hover = False
        self._press_dot = False
        self._phase = 0.0
        self._press_pos: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._dragged = False

        self._pixmap = QPixmap()
        if image_path:
            self._pixmap = QPixmap(str(image_path))
        if self._pixmap.isNull():
            self._pixmap = self._fallback_pixmap()

        # ~30 fps, and only while visible. A hidden orb has no timer running.
        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)

        # Built once: rebuilding it on every right-click left a QMenu child
        # behind each time, so long sessions accumulated hidden menus.
        self._menu: QMenu | None = None
        self._show_act = None
        self._mute_act = None

    # ── state shared with the main window ──────────────────────────────────
    def _set_tooltip(self) -> None:
        state = "microphone MUTED" if self._muted else "listening"
        self.setToolTip(
            f"RAVAN — {state} · click to show/hide · drag to move · "
            "badge: mute/unmute · right-click for controls"
        )

    def _dot_rect(self) -> QRectF:
        d = float(self.DOT)
        return QRectF(self.width() - d - 5.0, self.height() - d - 5.0, d, d)

    def _dot_center(self) -> QPointF:
        return self._dot_rect().center()

    def _in_dot(self, pos: QPointF) -> bool:
        c = self._dot_center()
        dx = pos.x() - c.x()
        dy = pos.y() - c.y()
        return dx * dx + dy * dy <= self.DOT_HIT * self.DOT_HIT

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._set_tooltip()
        self.update()

    def set_speaking(self, speaking: bool) -> None:
        speaking = bool(speaking)
        if speaking != self._speaking:
            self._speaking = speaking
            self.update()

    def set_main_visible(self, visible: bool) -> None:
        """Only changes the menu wording — never the orb itself."""
        self._main_visible = bool(visible)

    # ── animation ──────────────────────────────────────────────────────────
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._anim.isActive():
            self._anim.start()

    def hideEvent(self, event) -> None:
        self._anim.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._phase = (self._phase + 0.075) % (math.pi * 2)
        self.update()

    # ── painting ───────────────────────────────────────────────────────────
    def _fallback_pixmap(self) -> QPixmap:
        """Only used if the supplied orb asset is missing from disk."""
        pm = QPixmap(self.CORE, self.CORE)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(self.CORE / 2, self.CORE / 2, self.CORE * 0.5)
        g.setColorAt(0.0, QColor(150, 255, 255))
        g.setColorAt(0.35, QColor(0, 110, 255))
        g.setColorAt(1.0, QColor(0, 4, 14))
        p.setBrush(g)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self.CORE, self.CORE)
        p.end()
        return pm

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cx = self.width() / 2
        cy = self.height() / 2
        core = QRectF(cx - self.CORE / 2, cy - self.CORE / 2,
                      self.CORE, self.CORE)

        # Breathing 0..1 — slow, subtle, never a strobe.
        breathe = 0.5 + 0.5 * math.sin(self._phase)
        if self._speaking:
            breathe = 0.5 + 0.5 * math.sin(self._phase * 2.4)

        # 1. Outer aura.
        aura = QRadialGradient(cx, cy, self.CORE * 0.75)
        strength = (170 if self._speaking else 145) if self._hover else 105
        aura.setColorAt(0.50, QColor(0, 212, 255, 0))
        aura.setColorAt(0.74, QColor(0, 212, 255,
                                     int(strength * (0.65 + 0.35 * breathe))))
        aura.setColorAt(0.88, QColor(138, 92, 255, int(strength * 0.35)))
        aura.setColorAt(1.0, QColor(0, 120, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(aura)
        p.drawEllipse(QPoint(int(cx), int(cy)), int(self.CORE * 0.75),
                      int(self.CORE * 0.75))

        # 2. Two counter-rotating holo rings, drawn as arcs so they read as
        #    motion rather than as a border.
        p.save()
        p.translate(cx, cy)

        p.rotate(math.degrees(self._phase) * 0.9)
        ring = QRectF(-self.CORE * 0.56, -self.CORE * 0.36,
                      self.CORE * 1.12, self.CORE * 0.72)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(120, 235, 255, 150 if self._hover else 105), 1.3))
        p.drawArc(ring, 20 * 16, 150 * 16)
        p.setPen(QPen(QColor(120, 235, 255, 55), 1.0))
        p.drawArc(ring, 200 * 16, 110 * 16)

        p.rotate(-math.degrees(self._phase) * 1.7)
        ring2 = QRectF(-self.CORE * 0.38, -self.CORE * 0.58,
                       self.CORE * 0.76, self.CORE * 1.16)
        p.setPen(QPen(QColor(160, 130, 255, 120 if self._hover else 85), 1.2))
        p.drawArc(ring2, 130 * 16, 110 * 16)
        p.restore()

        # 3. Glass sphere.
        path = QPainterPath()
        path.addEllipse(core)
        p.save()
        p.setClipPath(path)
        p.drawPixmap(core, self._pixmap, QRectF(self._pixmap.rect()))

        # Depth: darkened lower edge, then a soft specular highlight.
        shade = QRadialGradient(cx, cy - self.CORE * 0.18, self.CORE * 0.62)
        shade.setColorAt(0.0, QColor(255, 255, 255, 0))
        shade.setColorAt(0.62, QColor(0, 10, 30, 0))
        shade.setColorAt(1.0, QColor(0, 4, 16, 165))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shade)
        p.drawEllipse(core)

        glint = QRadialGradient(cx - self.CORE * 0.17,
                                cy - self.CORE * 0.23, self.CORE * 0.26)
        glint.setColorAt(0.0, QColor(220, 255, 255, 120))
        glint.setColorAt(0.55, QColor(160, 240, 255, 40))
        glint.setColorAt(1.0, QColor(160, 240, 255, 0))
        p.setBrush(glint)
        p.drawEllipse(QRectF(cx - self.CORE * 0.42, cy - self.CORE * 0.46,
                             self.CORE * 0.56, self.CORE * 0.42))

        # 4. Energy core — the part that breathes.
        core_r = self.CORE * (0.13 + 0.035 * breathe)
        energy = QRadialGradient(cx, cy, core_r * 2.2)
        hot = QColor(200, 255, 255, 235) if not self._muted else QColor(
            255, 120, 150, 190)
        energy.setColorAt(0.0, hot)
        energy.setColorAt(0.35, QColor(0, 220, 255, 150))
        energy.setColorAt(1.0, QColor(0, 120, 255, 0))
        p.setBrush(energy)
        p.drawEllipse(QPoint(int(cx), int(cy)), int(core_r * 2.2),
                      int(core_r * 2.2))
        p.restore()

        # 5. Rim light.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(120, 235, 255, 200 if self._hover else 150), 1.3))
        p.drawEllipse(core.adjusted(0.6, 0.6, -0.6, -0.6))

        # 6. Microphone badge — the one control on the icon. Green while the
        #    mic is live, red with a slash when muted, a slow cyan pulse while
        #    speaking. It is drawn last so it stays legible over the glass, and
        #    its hit area is wider than the dot so it is easy to hit.
        dot = self._dot_rect()
        c = dot.center()
        if self._dot_hover:
            halo = QColor(255, 255, 255, 40)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(c, self.DOT_HIT - 2.0, self.DOT_HIT - 2.0)

        p.setPen(QPen(QColor(0, 8, 14, 235), 2))
        if self._muted:
            p.setBrush(_MUTED)
        elif self._speaking:
            pulse = 0.72 + 0.28 * breathe
            p.setBrush(QColor(0, 212, 255, int(255 * pulse)))
        else:
            p.setBrush(_GREEN)
        p.drawEllipse(dot)

        # Specular pinch on the badge keeps it spherical at 10 px.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawEllipse(QPointF(c.x() - 1.6, c.y() - 1.9), 1.7, 1.4)

        if self._muted:
            p.setPen(QPen(QColor(6, 8, 12, 215), 1.6))
            p.drawLine(QPointF(c.x() - 3.0, c.y() + 3.0),
                       QPointF(c.x() + 3.0, c.y() - 3.0))

    # ── mouse: drag to move, click to toggle the main window ───────────────
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._in_dot(event.position()):
                # The badge is a control, not the orb: this press must never
                # start a drag or toggle the main window.
                self._press_dot = True
                self._press_pos = None
                self._drag_offset = None
                self._dragged = False
                event.accept()
                return
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.pos()
            self._dragged = False
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press_dot:
            event.accept()
            return
        hover = self._in_dot(event.position())
        if hover != self._dot_hover:
            self._dot_hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover
                           else Qt.CursorShape.ArrowCursor)
            self.update()
        if self._drag_offset is None or not (
                event.buttons() & Qt.MouseButton.LeftButton):
            return
        pos = event.globalPosition().toPoint()
        delta = pos - self._press_pos
        if not self._dragged and (abs(delta.x()) + abs(delta.y())) > 5:
            self._dragged = True
        if self._dragged:
            self.move(pos - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._press_dot:
            self._press_dot = False
            if self._in_dot(event.position()):
                self.mute_toggled.emit()
            event.accept()
            return
        was_drag = self._dragged
        self._drag_offset = None
        self._press_pos = None
        self._dragged = False
        if was_drag:
            self.moved.emit(self.x(), self.y())
        else:
            self.activated.emit()
        event.accept()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        if self._dot_hover:
            self._dot_hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def _show_menu(self, global_pos: QPoint) -> None:
        if self._menu is None:
            self._menu = self._build_menu()
        self._show_act.setText(
            "Hide RAVAN window" if self._main_visible else "Show RAVAN window")
        self._mute_act.setChecked(self._muted)
        self._menu.exec(global_pos)

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#010d14; color:#8ffcff; border:1px solid #1a5c7a; "
            "padding:5px; }"
            "QMenu::item { padding:6px 20px; }"
            "QMenu::item:selected { background:#001f2e; color:#ffffff; }"
            "QMenu::item:disabled { color:#3d6b80; }"
            "QMenu::separator { height:1px; background:#123a4d; margin:4px 8px; }"
        )
        self._show_act = menu.addAction("Hide RAVAN window")
        self._show_act.triggered.connect(
            lambda _checked=False: self.main_toggle_requested.emit())

        self._mute_act = menu.addAction("Mute microphone")
        self._mute_act.setCheckable(True)
        self._mute_act.triggered.connect(
            lambda _checked=False: self.mute_toggled.emit())

        stop_act = menu.addAction("Stop speaking")
        stop_act.triggered.connect(
            lambda _checked=False: self.stop_speaking_requested.emit())

        menu.addSeparator()
        hide_act = menu.addAction("Hide floating icon")
        hide_act.triggered.connect(
            lambda _checked=False: self.hide_icon_requested.emit())

        quit_act = menu.addAction("Quit RAVAN")
        quit_act.triggered.connect(
            lambda _checked=False: self.quit_requested.emit())
        return menu
