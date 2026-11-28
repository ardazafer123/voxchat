"""VoxChat desktop interface."""
import argparse
import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import urllib.error

from PySide6.QtCore import (QEasingCurve, QElapsedTimer, QEvent, QObject, QPointF, QRectF, QSize, QTimer, Qt,
    QUrl, QVariantAnimation)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication, QCheckBox, QComboBox,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPlainTextEdit, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

import os
import platform

from live.auth import (CONFIG_DIR, DEFAULT_NPSSO_PATH, AuthError, active_account, adopt_legacy_npsso,
    list_accounts, remove_account, save_account, set_active, sign_in)
from live import social, updates
from live.share_view import DEFAULT_QUALITY, QUALITIES
from live.voice import BLOCKED, SessionControl, account_allowed, discover, run
from live.whoami import account_id_from_token, get_own_profile

NPSSO_URL = 'https://ca.account.sony.com/api/v1/ssocookie'
# ACCOUNTS and PROFILE have no rail button: the rail avatar opens Accounts, people's avatars and rows open Profile.
PARTIES, MESSAGES, FRIENDS, SETTINGS, ACCOUNTS, PROFILE = range(6)


@dataclass
class VoiceOptions:
    session: str
    owner: str
    input_device: int | None
    output_device: int | None
    muted: bool = False
    silent: bool = False
    seconds: float = 0
    list: bool = False
    create: bool = False


# Design tokens. Original palette: deep night surfaces, periwinkle accent,
# mint for live voice. No third-party logos, icons or artwork.
BG, SURFACE, SURFACE2, LINE = '#070b14', '#0d1322', '#141c2f', '#1f2a44'
TEXT, TEXT2, TEXT3 = '#eef1f7', '#9aa5bd', '#626d86'
ACCENT, VIOLET, LIVE, DANGER = '#4a7dff', '#8a5cff', '#3ddc97', '#ff4d67'
# shortcut: each poll costs 1 + one GET per invitation; 3 s keeps that well under any plausible
# server rate limit. Push events for invitations would make this instant; not implemented yet.
INVITE_POLL_MS = 3000
REQUEST_POLL_MS = 60_000  # friend requests change rarely; this only feeds notifications
THEIRS = '#26324d'  # other person's chat bubble; must stand out from the dark stage

STYLE = f'''
QWidget {{ background: transparent; color: {TEXT}; font-family: "Noto Sans", "DejaVu Sans"; font-size: 14px; }}
QWidget#root {{ background: {BG}; }}
QFrame#rail {{ background: {BG}; border-right: 1px solid {LINE}; }}
QFrame#sidebar {{ background: {SURFACE}; border-right: 1px solid {LINE}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 18px; }}
QFrame#dock {{ background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 34px; }}
QFrame#chip {{ background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 15px; }}
QLabel#h1 {{ font-size: 30px; font-weight: 700; }}
QLabel#h2 {{ font-size: 20px; font-weight: 700; }}
QLabel#h3 {{ font-size: 15px; font-weight: 600; }}
QLabel#section {{ color: {TEXT3}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }}
QLabel#muted {{ color: {TEXT2}; }}
QLabel#small {{ color: {TEXT3}; font-size: 12px; }}
QLabel#tag {{ color: {TEXT2}; background: {SURFACE2}; border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 600; }}
QLabel#status {{ color: {TEXT2}; font-size: 12px; font-weight: 600; }}
QListWidget {{ background: transparent; border: 0; outline: 0; }}
QListWidget::item {{ border-radius: 14px; margin: 2px 0; }}
QListWidget::item:hover {{ background: #121a2c; }}
QListWidget::item:selected {{ background: #18234a; }}
QComboBox {{ background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 12px; padding: 9px 12px; }}
QComboBox:hover {{ border-color: #2d3b5e; }}
QComboBox:disabled {{ color: {TEXT3}; }}
QComboBox::drop-down {{ border: 0; width: 26px; }}
QComboBox QAbstractItemView {{ background: {SURFACE2}; border: 1px solid {LINE}; selection-background-color: {ACCENT}; padding: 4px; }}
QPlainTextEdit {{ background: {BG}; color: {TEXT2}; border: 1px solid {LINE}; border-radius: 12px; padding: 10px; font-family: monospace; font-size: 12px; }}
QFrame#warn {{ background: #2a1220; border: 1px solid #5a2236; border-radius: 12px; }}
QLabel#warnText {{ color: #ffb3c0; }}
QFrame#blocked {{ background: #3d0f1c; border: 1px solid {DANGER}; border-radius: 12px; margin-top: 16px; }}
QLabel#blockedText {{ color: #ffd0d8; }}
QLineEdit {{ background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 12px; padding: 10px 12px; selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QCheckBox {{ spacing: 10px; color: {TEXT2}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid #3a4870; background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QListWidget#thread::item, QListWidget#thread::item:hover {{ background: transparent; }}
QFrame#composerBox {{ background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 26px; }}
QLineEdit#composer {{ background: transparent; border: 0; padding: 8px 0; }}
QLabel#unread {{ color: #ffffff; background: {ACCENT}; border-radius: 10px; padding: 1px 7px; font-size: 11px; font-weight: 700; }}
QFrame#updateBar {{ background: #12245c; border-bottom: 1px solid #2c4aa8; }}
QLabel#updateText {{ color: {TEXT}; }}
QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px 8px; }}
'''


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def hue_pair(name):
    hue = sum(ord(c) * 31 for c in name or '?') % 360
    return QColor.fromHsl(hue, 170, 120), QColor.fromHsl((hue + 45) % 360, 190, 95)


def draw_icon(painter, name, rect, color, width=2.0):
    """Stroke a 24-unit line icon into rect."""
    painter.save()
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 24, rect.height() / 24)
    pen = QPen(QColor(color), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if name in ('mic', 'mic-off'):
        path.addRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
        path.moveTo(5.5, 11)
        path.arcTo(QRectF(5.5, 4.5, 13, 13), 180, 180)
        path.moveTo(12, 17.5)
        path.lineTo(12, 21)
        if name == 'mic-off':
            path.moveTo(4, 4)
            path.lineTo(20, 20)
    elif name == 'leave':
        path.moveTo(14, 4)
        path.lineTo(6, 4)
        path.quadTo(4, 4, 4, 6)
        path.lineTo(4, 18)
        path.quadTo(4, 20, 6, 20)
        path.lineTo(14, 20)
        path.moveTo(10, 12)
        path.lineTo(20, 12)
        path.moveTo(16.5, 8.5)
        path.lineTo(20, 12)
        path.lineTo(16.5, 15.5)
    elif name == 'sliders':
        for y, knob in ((6, 15), (12, 8), (18, 13)):
            path.moveTo(4, y)
            path.lineTo(20, y)
            path.addEllipse(QPointF(knob, y), 2, 2)
    elif name == 'refresh':
        path.arcMoveTo(QRectF(4, 4, 16, 16), 60)
        path.arcTo(QRectF(4, 4, 16, 16), 60, 290)
        path.moveTo(16, 4)
        path.lineTo(16.2, 8.2)
        path.lineTo(20, 6.5)
    elif name == 'people':
        path.addEllipse(QPointF(9, 8), 3.5, 3.5)
        path.moveTo(2.5, 20)
        path.quadTo(3, 14, 9, 14)
        path.quadTo(15, 14, 15.5, 20)
        path.moveTo(15.5, 4.8)
        path.arcTo(QRectF(14, 4.5, 6.5, 7), 110, -220)
        path.moveTo(17.5, 14.3)
        path.quadTo(21, 15.5, 21.5, 20)
    elif name == 'gear':
        import math
        points = []
        for tooth in range(8):
            centre = tooth * 45
            for offset, radius in ((-20, 6.8), (-11, 9.5), (11, 9.5), (20, 6.8)):
                angle = math.radians(centre + offset)
                points.append(QPointF(12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        path.closeSubpath()
        path.addEllipse(QPointF(12, 12), 3, 3)
    elif name == 'speaker-off':
        path.moveTo(4, 9)
        path.lineTo(8, 9)
        path.lineTo(13, 5)
        path.lineTo(13, 19)
        path.lineTo(8, 15)
        path.lineTo(4, 15)
        path.closeSubpath()
        path.moveTo(16.5, 9.5)
        path.lineTo(21, 14.5)
        path.moveTo(21, 9.5)
        path.lineTo(16.5, 14.5)
    elif name == 'headphones':
        path.moveTo(4, 16)
        path.lineTo(4, 12)
        path.arcTo(QRectF(4, 4, 16, 16), 180, -180)
        path.lineTo(20, 16)
        path.addRoundedRect(QRectF(3, 14, 5, 7), 2, 2)
        path.addRoundedRect(QRectF(16, 14, 5, 7), 2, 2)
    elif name == 'user':
        path.addEllipse(QPointF(12, 8.5), 4, 4)
        path.moveTo(4.5, 20.5)
        path.quadTo(5, 14.5, 12, 14.5)
        path.quadTo(19, 14.5, 19.5, 20.5)
    elif name == 'chat':
        path.moveTo(7, 18.5)
        path.lineTo(4, 21)
        path.lineTo(4.6, 16.6)
        path.quadTo(3, 14.6, 3, 11.5)
        path.quadTo(3, 4, 12, 4)
        path.quadTo(21, 4, 21, 11.5)
        path.quadTo(21, 19, 12, 19)
        path.quadTo(9.2, 19, 7, 18.5)
    elif name == 'send':
        path.moveTo(4, 11.5)
        path.lineTo(20, 4)
        path.lineTo(13.5, 20)
        path.lineTo(11, 13)
        path.closeSubpath()
        path.moveTo(11, 13)
        path.lineTo(20, 4)
    elif name == 'search':
        path.addEllipse(QPointF(10.5, 10.5), 6, 6)
        path.moveTo(15, 15)
        path.lineTo(20, 20)
    elif name == 'add-user':
        path.addEllipse(QPointF(10, 8.5), 3.8, 3.8)
        path.moveTo(3, 20.5)
        path.quadTo(3.5, 14.5, 10, 14.5)
        path.quadTo(13, 14.5, 14.6, 15.8)
        path.moveTo(18.5, 13)
        path.lineTo(18.5, 20)
        path.moveTo(15, 16.5)
        path.lineTo(22, 16.5)
    elif name == 'check':
        path.moveTo(5, 12.5)
        path.lineTo(10, 17.5)
        path.lineTo(19, 7)
    elif name == 'close':
        path.moveTo(6, 6)
        path.lineTo(18, 18)
        path.moveTo(18, 6)
        path.lineTo(6, 18)
    elif name == 'join':
        path.moveTo(12, 5)
        path.lineTo(12, 19)
        path.moveTo(5, 12)
        path.lineTo(19, 12)
    elif name == 'back':
        path.moveTo(15, 5)
        path.lineTo(8, 12)
        path.lineTo(15, 19)
    elif name == 'trophy':
        path.moveTo(7, 4)
        path.lineTo(17, 4)
        path.lineTo(17, 9)
        path.quadTo(17, 14, 12, 14)
        path.quadTo(7, 14, 7, 9)
        path.closeSubpath()
        path.moveTo(17, 6)
        path.quadTo(21, 6, 20, 9)
        path.quadTo(19, 11, 17, 11)
        path.moveTo(7, 6)
        path.quadTo(3, 6, 4, 9)
        path.quadTo(5, 11, 7, 11)
        path.moveTo(12, 14)
        path.lineTo(12, 18)
        path.moveTo(8, 20)
        path.lineTo(16, 20)
    elif name == 'gamepad':
        path.addRoundedRect(QRectF(2.5, 7, 19, 11), 5, 5)
        path.moveTo(6, 12.5)
        path.lineTo(10, 12.5)
        path.moveTo(8, 10.5)
        path.lineTo(8, 14.5)
        path.addEllipse(QPointF(15.5, 11.5), .8, .8)
        path.addEllipse(QPointF(17.5, 13.5), .8, .8)
    elif name == 'screen':
        path.addRoundedRect(QRectF(3, 4.5, 18, 12), 2, 2)
        path.moveTo(9, 20)
        path.lineTo(15, 20)
        path.moveTo(12, 16.5)
        path.lineTo(12, 20)
    elif name == 'stop':
        path.addRoundedRect(QRectF(6.5, 6.5, 11, 11), 2, 2)
    elif name == 'pip':
        path.addRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        path.addRoundedRect(QRectF(12, 12, 6.5, 4.5), 1, 1)
    elif name == 'download':
        path.moveTo(12, 4)
        path.lineTo(12, 15)
        path.moveTo(7, 10)
        path.lineTo(12, 15)
        path.lineTo(17, 10)
        path.moveTo(5, 19.5)
        path.lineTo(19, 19.5)
    elif name == 'expand':
        for x, y, dx, dy in ((4, 9, 0, -5), (15, 4, 5, 0), (20, 15, 0, 5), (9, 20, -5, 0)):
            path.moveTo(x, y)
            path.lineTo(x + dx, y + dy)
            path.lineTo(x + dx + (5 if dy < 0 else -5 if dy > 0 else 0), y + dy + (5 if dx > 0 else -5 if dx < 0 else 0))
    painter.drawPath(path)
    painter.restore()


def blend(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class Button(QAbstractButton):
    """Painted button: hover tint and a short press scale for feedback."""
    COLORS = {'ghost': ('#00' + SURFACE2[1:], SURFACE2, TEXT2), 'soft': (SURFACE2, '#1d2742', TEXT),
              'accent': (ACCENT, '#6390ff', '#ffffff'), 'danger': (DANGER, '#ff6a80', '#ffffff')}

    def __init__(self, icon=None, text='', variant='soft', size=44, round_=False, tip=''):
        super().__init__()
        self.icon, self.variant, self.size, self.round = icon, variant, size, round_
        self.setText(text)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hover = self.press = 0.0
        self._hover = self._anim(150, 'hover')
        self._press = self._anim(110, 'press')

    def _anim(self, ms, attr):
        anim = QVariantAnimation(self)
        anim.setDuration(ms)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: (setattr(self, attr, v), self.update()))
        return anim

    def _go(self, anim, value, current):
        anim.stop()
        anim.setStartValue(current)
        anim.setEndValue(value)
        anim.start()

    def enterEvent(self, event):
        self._go(self._hover, 1.0, self.hover)

    def leaveEvent(self, event):
        self._go(self._hover, 0.0, self.hover)

    def mousePressEvent(self, event):
        self._go(self._press, 1.0, self.press)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._go(self._press, 0.0, self.press)
        super().mouseReleaseEvent(event)

    def sizeHint(self):
        if self.round or not self.text():
            return QSize(self.size, self.size)
        width = self.fontMetrics().horizontalAdvance(self.text()) + (60 if self.icon else 40)
        return QSize(width, self.size)

    def colors(self):
        base, hover, fg = self.COLORS[self.variant]
        if self.isCheckable() and self.isChecked():
            base, hover, fg = ('#18234a', '#1e2b58', TEXT) if self.variant == 'ghost' else (TEXT, '#ffffff', BG)
        elif self.isCheckable() and self.variant == 'ghost':
            base, hover = '#00' + BG[1:], BG  # tab bars sit on SURFACE2, so hover darkens instead
        if not self.isEnabled():
            return QColor(SURFACE2 if self.variant != 'ghost' else '#00000000'), QColor(TEXT3)
        return blend(base, hover, self.hover), QColor(fg)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        scale = 1 - .04 * self.press
        painter.translate(rect.center())
        painter.scale(scale, scale)
        painter.translate(-rect.center())
        fill, fg = self.colors()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        radius = rect.height() / 2
        painter.drawRoundedRect(rect, radius, radius)
        icon = self.icon
        if icon == 'mic' and self.isCheckable() and self.isChecked():
            icon = 'mic-off'
        side = min(22, rect.height() * .46)
        if self.text() and not self.round:
            painter.setPen(fg)
            font = painter.font()
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            text_w = self.fontMetrics().horizontalAdvance(self.text())
            total = text_w + (side + 8 if icon else 0)
            x = rect.center().x() - total / 2
            if icon:
                draw_icon(painter, icon, QRectF(x, rect.center().y() - side / 2, side, side), fg)
                x += side + 8
            painter.drawText(QRectF(x, rect.top(), text_w + 2, rect.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        elif icon:
            draw_icon(painter, icon, QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side), fg)


FACES = {}  # onlineId -> profile picture QPixmap, shared by every Avatar


class Avatar(QWidget):
    """Profile picture (or gradient initial) avatar with a level-driven speaking ring."""

    def __init__(self, name, size, ring=True):
        super().__init__()
        self.name, self.diameter, self.ring = name or '?', size, ring
        self.level = self.target = 0.0
        self.muted = False
        self.silenced = False  # muted for this listener only
        self.state = None      # None/'connected', 'connecting' or 'disconnected'
        self.spin = 0.0
        self.presence = None
        pad = 16 if ring else 0
        self.setFixedSize(size + pad * 2, size + pad * 2)
        self.on_click = None

    def mousePressEvent(self, event):
        if self.on_click:
            self.on_click()

    def set_level(self, value):
        # RMS speech sits around 0.02-0.15; map that onto 0..1.
        self.target = max(0.0, min(1.0, (value - .008) / .07))

    def set_flags(self, state, muted, silenced):
        if (state, muted, silenced) != (self.state, self.muted, self.silenced):
            self.state, self.muted, self.silenced = state, muted, silenced
            self.update()

    def tick(self):
        rate = .55 if self.target > self.level else .12  # fast attack, soft release
        previous = self.level
        self.level += (self.target - self.level) * rate
        if self.state == 'connecting':
            self.spin = (self.spin + 6) % 360
            self.update()
        elif abs(self.level - previous) > .002:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = self.diameter / 2
        if self.ring and self.level > .02:
            glow = QRadialGradient(center, radius + 16)
            color = QColor(LIVE)
            color.setAlphaF(.28 * self.level)
            glow.setColorAt(radius / (radius + 16), color)
            color.setAlphaF(0)
            glow.setColorAt(1, color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, radius + 16, radius + 16)
            ring = QColor(LIVE)
            ring.setAlphaF(.35 + .65 * self.level)
            painter.setPen(QPen(ring, 2.5 + 1.5 * self.level))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            gap = 4 + 2 * self.level
            painter.drawEllipse(center, radius + gap, radius + gap)
        if self.state == 'connecting':
            arc = QColor(TEXT2)
            painter.setPen(QPen(arc, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            box = QRectF(center.x() - radius - 5, center.y() - radius - 5, radius * 2 + 10, radius * 2 + 10)
            painter.drawArc(box, int(-self.spin * 16), 100 * 16)
        elif self.state == 'disconnected':
            painter.setPen(QPen(QColor(DANGER), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius + 5, radius + 5)
        if self.state in ('connecting', 'disconnected'):
            painter.setOpacity(.45)
        box = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        face = FACES.get(self.name)
        if face is not None:
            clip = QPainterPath()
            clip.addEllipse(box)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setClipPath(clip)
            painter.drawPixmap(box, face, QRectF(face.rect()))
            painter.restore()
        else:
            top, bottom = hue_pair(self.name)
            fill = QLinearGradient(box.topLeft(), box.bottomRight())
            fill.setColorAt(0, top)
            fill.setColorAt(1, bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawEllipse(center, radius, radius)
            font = QFont(self.font())
            font.setPixelSize(int(self.diameter * .4))
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor('#ffffff'))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.name[:1].upper())
        painter.setOpacity(1)
        if self.silenced:
            badge = max(18, self.diameter * .3)
            spot = QPointF(center.x() - radius * .7, center.y() + radius * .7)
            painter.setBrush(QColor(SURFACE))
            painter.setPen(QPen(QColor(BG), 3))
            painter.drawEllipse(spot, badge / 2, badge / 2)
            side = badge * .62
            draw_icon(painter, 'speaker-off', QRectF(spot.x() - side / 2, spot.y() - side / 2, side, side), TEXT, 2.2)
        if self.presence and not self.muted:
            spot = QPointF(center.x() + radius * .72, center.y() + radius * .72)
            painter.setBrush(QColor(LIVE if self.presence == 'online' else TEXT3))
            painter.setPen(QPen(QColor(SURFACE), 3))
            painter.drawEllipse(spot, radius * .24, radius * .24)
        if self.muted:
            badge = max(18, self.diameter * .3)
            spot = QPointF(center.x() + radius * .7, center.y() + radius * .7)
            painter.setBrush(QColor(DANGER))
            painter.setPen(QPen(QColor(BG), 3))
            painter.drawEllipse(spot, badge / 2, badge / 2)
            side = badge * .62
            draw_icon(painter, 'mic-off', QRectF(spot.x() - side / 2, spot.y() - side / 2, side, side), '#ffffff', 2.4)


ASSETS = Path(__file__).resolve().parent / 'assets'
IMAGES = {}  # image URL -> QPixmap (game and trophy art), memory only like FACES
TROPHY_COLORS = {'platinum': '#b9c8e6', 'gold': '#e8b64c', 'silver': '#b4bcc9', 'bronze': '#c7834f'}


def tier_name(tier):
    """Trophy level tiers 1-10: three each of bronze, silver, gold, then platinum."""
    return 'Platinum' if tier >= 10 else ('Bronze', 'Silver', 'Gold')[min(2, (max(1, tier) - 1) // 3)]


def ago(stamp):
    """'3 days ago' style text for an ISO 8601 time from PSN; '' when absent or unreadable."""
    try:
        when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return ''
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 90:
        return 'just now'
    for limit, size, unit in ((3600, 60, 'min'), (86400, 3600, 'h')):
        if seconds < limit:
            return f'{int(seconds // size)} {unit} ago'
    days = int(seconds // 86400)
    return 'yesterday' if days == 1 else f'{days} days ago' if days < 30 else when.strftime('%b %-d, %Y')


class Picture(QWidget):
    """Rounded image from IMAGES, cropped to fill; a plain tile until it arrives."""

    def __init__(self, url, width, height=None, radius=10):
        super().__init__()
        self.url, self.radius = url, radius
        self.setFixedSize(width, height or width)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        shape = QPainterPath()
        shape.addRoundedRect(box, self.radius, self.radius)
        image = IMAGES.get(self.url)
        if image is None:
            painter.fillPath(shape, QColor(SURFACE2))
            return
        painter.setClipPath(shape)
        scale = max(box.width() / image.width(), box.height() / image.height())
        width, height = box.width() / scale, box.height() / scale
        painter.drawPixmap(box, image, QRectF((image.width() - width) / 2, (image.height() - height) / 2, width, height))


class Bar(QWidget):
    """Thin rounded progress bar."""

    def __init__(self, color=ACCENT, height=6):
        super().__init__()
        self.value, self.color = 0.0, color
        self.setFixedHeight(height)

    def set_value(self, value):
        self.value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = self.height() / 2
        painter.setBrush(QColor(LINE))
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
        if self.value:
            painter.setBrush(QColor(self.color))
            painter.drawRoundedRect(QRectF(0, 0, max(self.height(), self.width() * self.value), self.height()),
                                    radius, radius)


class Trophy(QWidget):
    """Trophy picture for its type; the tooltip names the type."""
    PICTURES = {}  # type -> QPixmap, loaded once

    def __init__(self, kind, size=16):
        super().__init__()
        self.kind = kind if kind in TROPHY_COLORS else 'bronze'
        self.setFixedSize(size, size)
        self.setToolTip(self.kind.title())

    def paintEvent(self, event):
        picture = self.PICTURES.get(self.kind)
        if picture is None:
            picture = self.PICTURES[self.kind] = QPixmap(str(ASSETS / f'trophy-{self.kind}.png'))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), picture)


class SmoothScroll(QObject):
    """Mouse-wheel notches glide a short distance instead of jumping three rows. Touchpads keep their own scrolling."""
    STEP = 90  # pixels per wheel notch

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel or not isinstance(obj, QWidget):
            return False
        if not event.pixelDelta().isNull() or not event.angleDelta().y() or event.modifiers():
            return False
        area = obj
        while area is not None and not isinstance(area, QAbstractScrollArea):
            if isinstance(area, QComboBox):
                return False  # the wheel picks a value there
            area = area.parentWidget()
        if area is None:
            return False
        bar = area.verticalScrollBar()
        if bar.maximum() == bar.minimum():
            return False
        glide = getattr(bar, 'glide', None)
        if glide is None:
            glide = bar.glide = QVariantAnimation(bar)
            glide.setDuration(220)
            glide.setEasingCurve(QEasingCurve.Type.OutCubic)
            glide.valueChanged.connect(lambda value, bar=bar: bar.setValue(round(value)))
        aim = glide.endValue() if glide.state() == QVariantAnimation.State.Running else bar.value()
        aim = max(bar.minimum(), min(bar.maximum(), aim - event.angleDelta().y() / 120 * self.STEP))
        glide.stop()
        glide.setStartValue(float(bar.value()))
        glide.setEndValue(float(aim))
        glide.start()
        return True


def trophy_counts(counts, total=None, size=12):
    """Trophy icon and number per trophy type, e.g. '● 2  ● 6'; 'earned/defined' when total is given."""
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    for kind in TROPHY_COLORS:
        if total is not None and not total.get(kind):
            continue  # a game without a platinum shows no platinum slot
        row.addWidget(Trophy(kind, size + 8))
        text = str(counts.get(kind, 0)) + (f"/{total[kind]}" if total is not None else '')
        number = label(text, 'small' if size < 14 else 'h3')
        row.addWidget(number)
    row.addStretch()
    return widget


class Dot(QWidget):
    """Status dot; pulses while work is in progress."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(10, 10)
        self.color, self.alpha = QColor(TEXT3), 1.0
        self.pulse = QVariantAnimation(self)
        self.pulse.setDuration(1100)
        self.pulse.setKeyValueAt(0, 1.0)
        self.pulse.setKeyValueAt(.5, .25)
        self.pulse.setKeyValueAt(1, 1.0)
        self.pulse.setLoopCount(-1)
        self.pulse.valueChanged.connect(lambda v: (setattr(self, 'alpha', v), self.update()))

    def set_state(self, color, pulsing):
        self.color = QColor(color)
        if pulsing:
            self.pulse.start()
        else:
            self.pulse.stop()
            self.alpha = 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.color)
        color.setAlphaF(self.alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(1, 1, 8, 8))


class Meter(QWidget):
    """Horizontal input meter made of rounded segments."""

    def __init__(self):
        super().__init__()
        self.level = 0.0
        self.setFixedHeight(10)

    def set_level(self, value):
        value = max(0.0, min(1.0, value / .12))
        self.level += (value - self.level) * (.6 if value > self.level else .15)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count, gap = 28, 3
        width = (self.width() - gap * (count - 1)) / count
        lit = round(self.level * count)
        for index in range(count):
            color = QColor(LIVE if index < count * .7 else '#f5c451' if index < count * .9 else DANGER)
            if index >= lit:
                color = QColor(LINE)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(index * (width + gap), 0, width, 10), 3, 3)


class Rail(QFrame):
    """Navigation rail with a sliding active indicator."""

    def __init__(self, on_select):
        super().__init__()
        self.setObjectName('rail')
        self.setFixedWidth(76)
        self.on_select = on_select
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(14, 20, 14, 20)
        self.column.setSpacing(10)
        self.buttons = []
        self.indicator = QWidget(self)
        self.indicator.setStyleSheet(f'background: {ACCENT}; border-radius: 2px;')
        self.indicator.resize(4, 22)
        self.slide = QVariantAnimation(self)
        self.slide.setDuration(220)
        self.slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.slide.valueChanged.connect(lambda y: self.indicator.move(0, int(y)))

    def add(self, icon, tip):
        button = Button(icon, variant='ghost', size=48, round_=True, tip=tip)
        button.setCheckable(True)
        index = len(self.buttons)
        button.clicked.connect(lambda: self.select(index))
        self.buttons.append(button)
        self.column.addWidget(button)
        return button

    def select(self, index, animate=True):
        for position, button in enumerate(self.buttons):
            button.setChecked(position == index)
        self.indicator.setVisible(index < len(self.buttons))
        if index >= len(self.buttons):  # page reached from elsewhere (Accounts via the avatar)
            self.on_select(index)
            return
        target = self.buttons[index]
        y = target.y() + (target.height() - self.indicator.height()) / 2
        if animate and self.isVisible():
            self.slide.stop()
            self.slide.setStartValue(float(self.indicator.y()))
            self.slide.setEndValue(float(y))
            self.slide.start()
        else:
            self.indicator.move(0, int(y))
        self.on_select(index)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        current = next((i for i, b in enumerate(self.buttons) if b.isChecked()), None)
        if current is not None:
            target = self.buttons[current]
            self.indicator.move(0, int(target.y() + (target.height() - self.indicator.height()) / 2))


class Stage(QWidget):
    """Main canvas: soft aurora wash behind the party view."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BG))
        for x, y, r, color, alpha in ((.15, .0, .75, ACCENT, .20), (.95, .9, .65, VIOLET, .13)):
            glow = QRadialGradient(QPointF(self.width() * x, self.height() * y), max(self.width(), self.height()) * r)
            tint = QColor(color)
            tint.setAlphaF(alpha)
            glow.setColorAt(0, tint)
            tint.setAlphaF(0)
            glow.setColorAt(1, tint)
            painter.fillRect(self.rect(), glow)


class EmptyArt(QWidget):
    """Rings ripple out from an icon while the empty state is on screen (it is listening)."""
    PERIOD = 3.6  # seconds for one ring to travel out; three rings are a third apart

    def __init__(self, icon):
        super().__init__()
        self.icon = icon
        self.setFixedSize(180, 180)
        self.clock = QElapsedTimer()
        self.clock.start()
        self.frames = QTimer(self)
        self.frames.timeout.connect(self.update)

    def showEvent(self, event):
        self.frames.start(33)

    def hideEvent(self, event):
        self.frames.stop()  # no repaint work while another page is shown

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(90, 90)
        phase = self.clock.elapsed() / 1000 / self.PERIOD
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for ring in range(3):
            t = (phase + ring / 3) % 1
            eased = 1 - (1 - t) ** 3  # ease-out: leaves the icon quickly, settles at the edge
            color = QColor(ACCENT)
            color.setAlphaF(.5 * (1 - t) ** 1.5)
            painter.setPen(QPen(color, 1.5))
            radius = 36 + 52 * eased
            painter.drawEllipse(center, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SURFACE2))
        painter.drawEllipse(center, 32, 32)
        draw_icon(painter, self.icon, QRectF(76, 76, 28, 28), TEXT2)


def keep_above(window, size):
    """Pin a window above every other one, in the bottom-right corner of its screen.

    Wayland ignores Qt's stay-on-top flag and lets no client place itself, so on KDE ask
    KWin to do both through a one-shot script."""
    if QGuiApplication.platformName() != 'wayland':
        area = window.screen().availableGeometry()  # the stay-on-top flag already works here
        window.move(area.right() - size.width() - 24, area.bottom() - size.height() - 24)
        return
    if 'KDE' not in os.environ.get('XDG_CURRENT_DESKTOP', ''):
        return  # shortcut: other Wayland desktops need the user's own "keep above" window rule
    import tempfile
    from PySide6.QtDBus import QDBusConnection, QDBusInterface
    script = (
        'const all = workspace.windowList ? workspace.windowList() : workspace.clientList();\n'
        f'for (const w of all) if (w.pid === {os.getpid()} && w.caption === {window.windowTitle()!r}) {{\n'
        '  w.keepAbove = true;\n'
        '  const a = workspace.clientArea(KWin.PlacementArea, w);\n'
        f'  w.frameGeometry = {{x: a.x + a.width - {size.width() + 24}, y: a.y + a.height - {size.height() + 24},'
        f' width: {size.width()}, height: {size.height()}}};\n'
        '}\n')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as file:
        file.write(script)
    bus = QDBusConnection.sessionBus()
    scripting = QDBusInterface('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', bus)
    name = f'voxchat-keep-above-{os.getpid()}'
    scripting.call('unloadScript', name)
    reply = scripting.call('loadScript', file.name, name)
    if reply.arguments():
        QDBusInterface('org.kde.KWin', f'/Scripting/Script{reply.arguments()[0]}', 'org.kde.kwin.Script', bus).call('run')
    QTimer.singleShot(1000, lambda: (scripting.call('unloadScript', name), os.unlink(file.name)))


class ScreenView(QWidget):
    """The latest shared-screen frame, letterboxed on black."""

    def __init__(self):
        super().__init__()
        self.image = None

    def set_frame(self, frame):
        if frame is None:
            self.image = None
        else:
            # Scale in the decoder's converter, not per paint: a 1080p frame is ~6 MB of RGB.
            ratio = self.devicePixelRatioF()
            size = QSize(frame.width, frame.height).scaled(
                QSize(round(self.width() * ratio), round(self.height() * ratio)), Qt.AspectRatioMode.KeepAspectRatio)
            width, height = max(2, size.width() // 2 * 2), max(2, size.height() // 2 * 2)
            rgb = frame.to_ndarray(width=width, height=height, format='rgb24')
            self.image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self.update()

    def paint_frame(self, painter):
        painter.fillRect(self.rect(), QColor('#000000'))
        if self.image is not None:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                                     size.width(), size.height()), self.image)

    def paintEvent(self, event):
        self.paint_frame(QPainter(self))


class FloatingScreen(ScreenView):
    """Borderless picture-in-picture that stays above other windows, or full screen.

    Drag to move, drag an edge to resize. Double-click or Esc goes back into the app;
    the hover × stops watching."""
    TITLE = 'VoxChat picture-in-picture'
    EDGE = 10

    def __init__(self, on_back, on_stop, full=False):
        super().__init__()
        self.on_back, self.on_stop, self.full = on_back, on_stop, full
        self.hover, self.pressed = False, None
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags if full else flags | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(self.TITLE)
        self.setMinimumSize(256, 144)
        self.setMouseTracking(True)
        self.resize(480, 270)

    def open(self):
        if self.full:
            self.showFullScreen()
        else:
            self.show()
            keep_above(self, self.size())

    def close_rect(self):
        return QRectF(self.width() - 40, 10, 30, 30)

    def edges(self, pos):
        if self.full:
            return Qt.Edge(0)
        edges = Qt.Edge(0)
        if pos.x() < self.EDGE:
            edges |= Qt.Edge.LeftEdge
        if pos.x() > self.width() - self.EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() < self.EDGE:
            edges |= Qt.Edge.TopEdge
        if pos.y() > self.height() - self.EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def enterEvent(self, event):
        self.hover = True
        self.update()

    def leaveEvent(self, event):
        self.hover = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.hover and self.close_rect().contains(event.position()):
            self.on_stop()
            return
        edges = self.edges(event.position())
        if edges:
            self.windowHandle().startSystemResize(edges)
        else:
            self.pressed = event.position()  # move starts once the mouse travels, so double-clicks still arrive

    def mouseMoveEvent(self, event):
        if self.pressed is not None and not self.full:
            if (event.position() - self.pressed).manhattanLength() > 4:
                self.pressed = None
                self.windowHandle().startSystemMove()
            return
        cursors = {Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
                   Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
                   Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
                   Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
                   Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor, Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
                   Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor, Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor}
        self.setCursor(cursors.get(self.edges(event.position()), Qt.CursorShape.ArrowCursor))

    def mouseReleaseEvent(self, event):
        self.pressed = None

    def mouseDoubleClickEvent(self, event):
        self.on_back()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.on_back()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        event.accept()
        self.on_back()  # e.g. closed from the taskbar

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_frame(painter)
        if self.hover:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.close_rect()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(rect)
            draw_icon(painter, 'close', rect.adjusted(7, 7, -7, -7), '#ffffff', 2.4)


class ShareCard(ScreenView):
    """The inline shared-screen tile: a live badge and play button, then the stream itself."""

    def __init__(self, on_click, on_double_click):
        super().__init__()
        self.on_click, self.on_double_click = on_click, on_double_click
        self.name, self.note, self.playing, self.bar = '', '', False, None
        self.setFixedSize(448, 252)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def fit(self, height):
        """Fill the given height at 16:9, between 144 and 252 pixels tall."""
        height = max(144, min(252, height))
        self.setFixedSize(height * 16 // 9, height)

    def resizeEvent(self, event):
        if self.bar:
            self.bar.setGeometry(10, self.height() - 46, self.width() - 20, 36)

    def enterEvent(self, event):
        if self.bar:
            self.bar.setVisible(self.playing)

    def leaveEvent(self, event):
        if self.bar:
            self.bar.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.on_click()

    def mouseDoubleClickEvent(self, event):
        self.on_double_click()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 16, 16)
        painter.setClipPath(clip)
        if self.image is not None:
            self.paint_frame(painter)
        else:
            glow = QRadialGradient(QPointF(self.width() / 2, self.height() * .4), self.width() * .7)
            glow.setColorAt(0, QColor('#1a2547'))
            glow.setColorAt(1, QColor(SURFACE))
            painter.fillRect(self.rect(), glow)
        top = QLinearGradient(0, 0, 0, 64)
        top.setColorAt(0, QColor(0, 0, 0, 150))
        top.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, self.width(), 64), top)
        if self.bar and self.bar.isVisible():
            bottom = QLinearGradient(0, self.height() - 72, 0, self.height())
            bottom.setColorAt(0, QColor(0, 0, 0, 0))
            bottom.setColorAt(1, QColor(0, 0, 0, 170))
            painter.fillRect(QRectF(0, self.height() - 72, self.width(), 72), bottom)
        bold = painter.font()
        bold.setPixelSize(14)
        bold.setWeight(QFont.Weight.DemiBold)
        painter.setFont(bold)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(QRectF(16, 14, self.width() - 110, 22), Qt.AlignmentFlag.AlignVCenter, self.name)
        badge = QRectF(self.width() - 66, 14, 52, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DANGER))
        painter.drawRoundedRect(badge, 6, 6)
        small = QFont(bold)
        small.setPixelSize(11)
        small.setWeight(QFont.Weight.Bold)
        painter.setFont(small)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, 'LIVE')
        center = QPointF(self.width() / 2, self.height() / 2)
        if not self.playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 40))
            painter.drawEllipse(center, 30, 30)
            triangle = QPainterPath()
            triangle.moveTo(center + QPointF(-8, -12))
            triangle.lineTo(center + QPointF(13, 0))
            triangle.lineTo(center + QPointF(-8, 12))
            triangle.closeSubpath()
            painter.setBrush(QColor('#ffffff'))
            painter.drawPath(triangle)
        if self.note:
            painter.setFont(bold)
            painter.setPen(QColor(TEXT2 if self.image is None else '#ffffff'))
            area = QRectF(0, center.y() + (44 if not self.playing else -12), self.width(), 24)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, self.note)


class SharePanel(QWidget):
    """One card per member sharing their screen. One plays at a time (one stream connection):
    inline, in picture-in-picture or full screen. Picking a quality reconnects at that layer."""
    STATES = {'connecting': 'Connecting…', 'ended': 'Sharing ended', 'failed': 'Could not connect'}

    def __init__(self):
        super().__init__()
        self.control, self.name, self.floating, self.shown = None, None, None, None  # name: who we watch
        self.cards, self.space = {}, (252, 448)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(16)
        self.bar = QWidget()  # hover controls, moved onto the card that plays
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.stop_button = Button('stop', variant='soft', size=36, round_=True, tip='Stop watching')
        self.stop_button.clicked.connect(lambda: self.stop())
        self.status = label('', 'status')
        self.status.setStyleSheet('color: #ffffff;')
        self.quality = QComboBox()
        self.quality.setStyleSheet('padding: 4px 10px; border-radius: 10px;')
        self.quality.addItems(list(QUALITIES))
        self.quality.setCurrentText(DEFAULT_QUALITY)
        self.quality.setToolTip('Higher quality needs a faster connection')
        self.quality.currentTextChanged.connect(self.change_quality)
        self.pip_button = Button('pip', variant='soft', size=36, round_=True, tip='Picture-in-picture (stays on top)')
        self.pip_button.clicked.connect(lambda: self.float_out(full=False))
        self.full_button = Button('expand', variant='soft', size=36, round_=True, tip='Full screen')
        self.full_button.clicked.connect(lambda: self.float_out(full=True))
        row.addWidget(self.stop_button)
        row.addWidget(self.status, 1)
        for widget in (self.quality, self.pip_button, self.full_button):
            row.addWidget(widget)
        self.bar.hide()
        self.setVisible(False)

    def fit(self, height, width):
        """Size every card to share the given space at 16:9."""
        self.space = (height, width)
        count = max(1, len(self.cards))
        for card in self.cards.values():
            card.fit(min(height, (width - 16 * (count - 1)) // count * 9 // 16))

    def set_sharers(self, names):
        if self.name is not None and self.name not in names:
            self.stop()
        for name in set(self.cards) - set(names):
            self.cards.pop(name).deleteLater()
        for name in names:
            if name not in self.cards:
                card = self.cards[name] = ShareCard(lambda n=name: self.clicked(n), lambda n=name: self.go_full(n))
                card.name = name
                self.row.addWidget(card)
        self.fit(*self.space)
        self.setVisible(bool(names))

    def sync(self, control):
        """Follow the call: who shares, and the viewer's newest frame and state. Runs on the call ticker."""
        self.control = control
        names = sorted(control.sharing) if control else []
        if names != sorted(self.cards):
            self.set_sharers(names)
        if self.name is None:
            return
        card, viewer = self.cards[self.name], control.viewer
        frame = viewer.frame if viewer else None
        if frame is not None and frame is not self.shown:
            self.shown = frame
            (self.floating or card).set_frame(frame)
        if self.floating:
            note = 'Playing in full screen' if self.floating.full else 'Playing in picture-in-picture'
        else:
            note = '' if viewer and viewer.state == 'live' else self.STATES.get(viewer.state if viewer else '', 'Connecting…')
        status = (f'Live · {viewer.size[1]}p' if viewer and viewer.state == 'live' and viewer.size else
                  self.STATES.get(viewer.state, '') if viewer else 'Connecting…')
        if (note, status) != (card.note, self.status.text()):
            card.note = note
            self.status.setText(status)
            card.update()

    def clicked(self, name):
        if name != self.name:
            self.watch(name)
        elif self.floating:
            self.bring_back()

    def watch(self, name):
        if self.control is None:
            return
        self.stop(quiet=True)  # the worker drops the old stream when it starts the new one
        self.name, self.shown = name, None
        card = self.cards[name]
        card.playing, card.bar = True, self.bar
        self.bar.setParent(card)
        card.resizeEvent(None)
        self.bar.setVisible(card.underMouse())
        self.control.watch.put(('watch', name, self.quality.currentText()))
        card.update()

    def change_quality(self, quality):
        if self.name is not None and self.control is not None:
            self.control.watch.put(('watch', self.name, quality))

    def float_out(self, full):
        if self.name is None:
            return
        if self.floating:
            self.floating.on_back = lambda: None
            self.floating.close()
        self.floating = FloatingScreen(self.bring_back, self.stop, full)
        self.floating.open()
        self.shown = None  # repaint the newest frame in the new view
        self.cards[self.name].set_frame(None)

    def go_full(self, name):
        if name == self.name:
            self.float_out(full=True)

    def bring_back(self):
        floating, self.floating = self.floating, None
        if floating:
            floating.on_back = lambda: None
            floating.close()
            floating.deleteLater()
        self.shown = None
        if self.name in self.cards:
            self.cards[self.name].update()

    def stop(self, quiet=False):
        if self.name is not None and self.control is not None and not quiet:
            self.control.watch.put(('stop',))
        self.bring_back()
        card = self.cards.get(self.name)
        if card:
            card.playing, card.note, card.bar = False, '', None
            card.set_frame(None)
        self.bar.hide()
        self.bar.setParent(None)
        self.name = None

    def reset(self):
        self.stop()
        self.sync(None)


class PartyWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.events = queue.Queue()
        self.worker = None
        self.control = None
        self.parties = []
        self.closing = False
        self.busy = False
        self.polling = False  # a background invitation reload is in flight
        self.connected_at = None
        self.avatars = {}
        self.member_status = {}
        self.silenced = set()  # onlineIds muted for this listener only, during a call
        self.roster = []
        self.faces_wanted = set()  # onlineIds whose profile picture was requested
        self.images_wanted = set()  # game and trophy art URLs requested
        self.profile_target, self.profile_return, self.profile_title = None, FRIENDS, None
        self.profile_hints = ['', '', '']  # per profile tab: games, trophy titles, one game's trophies
        self.me = None
        self.setWindowTitle('VoxChat')
        self.setObjectName('root')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.resize(1180, 740)
        self.setMinimumSize(960, 620)
        self.setStyleSheet(STYLE)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.pages = QStackedWidget()
        self.rail = Rail(self.pages.setCurrentIndex)
        self.rail_avatar = Avatar('?', 40, ring=False)
        self.rail_avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rail_avatar.on_click = lambda: self.rail.select(ACCOUNTS)
        self.rail.column.addWidget(self.rail_avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        self.rail.column.addSpacing(14)
        self.rail.add('headphones', 'Parties')
        self.rail.add('chat', 'Messages')
        self.rail.add('add-user', 'Friends')
        self.rail.column.addStretch()
        self.rail.add('gear', 'Settings')
        root.addWidget(self.rail)
        content = QWidget()
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        self.update_bar = QFrame()
        self.update_bar.setObjectName('updateBar')
        bar_row = QHBoxLayout(self.update_bar)
        bar_row.setContentsMargins(24, 8, 12, 8)
        bar_row.setSpacing(10)
        self.update_text = label('', 'updateText')
        bar_row.addWidget(self.update_text, 1)
        download = Button('download', 'Download', variant='accent', size=34)
        download.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.update_url)))
        bar_row.addWidget(download)
        hide = Button('close', variant='ghost', size=34, round_=True, tip='Hide until VoxChat restarts')
        hide.clicked.connect(self.update_bar.hide)
        bar_row.addWidget(hide)
        self.update_bar.hide()
        self.update_tag = self.update_url = None
        stack.addWidget(self.update_bar)
        stack.addWidget(self.pages, 1)
        root.addWidget(content, 1)
        self.hosting, self.invited = False, set()
        self.conversations, self.thread, self.friends, self.requests, self.results = [], None, [], [], []
        self.social_client, self.loaded, self.generation = None, set(), 0
        self.offline = False  # smoke previews never call PSN
        for build in (self.build_parties, self.build_messages, self.build_friends,
                      self.build_settings, self.build_accounts, self.build_profile):
            self.pages.addWidget(build())
        self.rail.on_select = self.page_changed
        self.reload_account()
        self.rail.select(PARTIES if self.me else ACCOUNTS, animate=False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        QTimer.singleShot(5000, self.check_updates)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.check_updates)
        self.update_timer.start(6 * 3600 * 1000)  # also catch releases while VoxChat sits in the tray
        self.ticker = QTimer(self)  # runs only during a call
        self.ticker.timeout.connect(self.tick)
        self.invite_poll = QTimer(self)
        self.invite_poll.timeout.connect(self.poll_invitations)
        self.invite_poll.start(INVITE_POLL_MS)
        self.request_poll = QTimer(self)
        self.request_poll.timeout.connect(lambda: self.social(
            'friends', lambda: (self.client().friends(), self.client().requests())))
        self.request_poll.start(REQUEST_POLL_MS)
        self.setup_tray()

    # Layout

    def build_parties(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(310)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(8)
        head = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(2)
        title.addWidget(label('Parties', 'h2'))
        self.signed_in = label('', 'small')
        title.addWidget(self.signed_in)
        head.addLayout(title, 1)
        self.refresh_button = Button('refresh', variant='ghost', size=38, round_=True, tip='Refresh invitations')
        self.refresh_button.clicked.connect(self.refresh)
        head.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignTop)
        side.addLayout(head)
        side.addSpacing(10)
        self.start_button = Button('join', 'Start a party', variant='accent', size=44)
        self.start_button.clicked.connect(self.start_party)
        side.addWidget(self.start_button)
        side.addSpacing(10)
        # Invitations when idle; friends to invite while in a party.
        self.side_pages = QStackedWidget()
        invitations = QWidget()
        column = QVBoxLayout(invitations)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(label('INVITATIONS', 'section'))
        self.party_list = QListWidget()
        self.party_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.party_list.itemSelectionChanged.connect(self.show_party)
        column.addWidget(self.party_list, 1)
        self.list_hint = label('Nothing here yet. Invitations show up after a refresh.', 'small')
        self.list_hint.setWordWrap(True)
        column.addWidget(self.list_hint)
        self.side_pages.addWidget(invitations)
        inviting = QWidget()
        column = QVBoxLayout(inviting)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(label('INVITE FRIENDS', 'section'))
        self.invite_list = QListWidget()
        self.invite_list.itemClicked.connect(lambda item: self.row_profile(self.invite_list, item))
        self.invite_list.setObjectName('thread')  # rows hold buttons; no selection highlight
        self.invite_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.invite_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        column.addWidget(self.invite_list, 1)
        self.invite_hint = label('', 'small')
        self.invite_hint.setWordWrap(True)
        column.addWidget(self.invite_hint)
        self.side_pages.addWidget(inviting)
        side.addWidget(self.side_pages, 1)
        row.addWidget(sidebar)

        stage = Stage()
        body = QVBoxLayout(stage)
        body.setContentsMargins(40, 28, 40, 28)
        body.setSpacing(0)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.party_title = label('', 'h1')
        self.party_sub = label('', 'muted')
        heading.addWidget(self.party_title)
        heading.addWidget(self.party_sub)
        top.addLayout(heading, 1)
        chip = QFrame()
        chip.setObjectName('chip')
        chip_row = QHBoxLayout(chip)
        chip_row.setContentsMargins(12, 6, 14, 6)
        chip_row.setSpacing(8)
        self.dot = Dot()
        self.status = label('Ready', 'status')
        chip_row.addWidget(self.dot)
        chip_row.addWidget(self.status)
        top.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(top)
        self.blocked = QFrame()
        self.blocked.setObjectName('blocked')
        blocked_box = QVBoxLayout(self.blocked)
        blocked_box.setContentsMargins(16, 12, 16, 12)
        blocked_text = label('<b>Parties are blocked for this account.</b> New accounts must sign in once on a real '
                             'console before they can use voice chat. If parties work for this account in the '
                             'official phone app, they work here too.', 'blockedText')
        blocked_text.setWordWrap(True)
        blocked_box.addWidget(blocked_text)
        self.blocked.hide()
        body.addWidget(self.blocked)

        self.stage_pages = QStackedWidget()
        empty = QWidget()
        center = QVBoxLayout(empty)
        center.addStretch()
        center.addWidget(EmptyArt('headphones'), 0, Qt.AlignmentFlag.AlignHCenter)
        center.addSpacing(18)
        self.empty_title, self.empty_text = label('', 'h2'), label('', 'muted')
        for line in (self.empty_title, self.empty_text):
            line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            center.addWidget(line)
        center.addSpacing(18)
        self.empty_refresh = Button('refresh', 'Check for invitations', variant='soft', size=44)
        self.empty_refresh.clicked.connect(lambda: self.refresh() if self.me else self.rail.select(ACCOUNTS))
        center.addWidget(self.empty_refresh, 0, Qt.AlignmentFlag.AlignHCenter)
        center.addStretch()
        self.stage_pages.addWidget(empty)
        members = QWidget()
        stack = QVBoxLayout(members)
        stack.setContentsMargins(0, 8, 0, 0)
        stack.setSpacing(20)
        self.share_panel = SharePanel()
        stack.addWidget(self.share_panel, 0, Qt.AlignmentFlag.AlignHCenter)
        self.members_grid = QGridLayout()
        stack.addLayout(self.members_grid, 1)
        self.members_grid.setContentsMargins(0, 0, 0, 12)
        self.members_grid.setHorizontalSpacing(28)
        self.members_grid.setVerticalSpacing(22)
        self.members_grid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage_pages.addWidget(members)
        body.addWidget(self.stage_pages, 1)

        dock = QFrame()
        dock.setObjectName('dock')
        controls = QHBoxLayout(dock)
        controls.setContentsMargins(10, 10, 10, 10)
        controls.setSpacing(10)
        self.join_button = Button('join', 'Join party', variant='accent', size=48)
        self.join_button.clicked.connect(self.join)
        self.mute_button = Button('mic', variant='soft', size=48, round_=True, tip='Mute microphone')
        self.mute_button.setCheckable(True)
        self.mute_button.clicked.connect(self.mute)
        self.leave_button = Button('leave', 'Leave', variant='danger', size=48)
        self.leave_button.clicked.connect(self.leave)
        settings = Button('sliders', variant='soft', size=48, round_=True, tip='Audio settings')
        settings.clicked.connect(lambda: self.rail.select(SETTINGS))
        for button in (self.mute_button, self.join_button, self.leave_button, settings):
            controls.addWidget(button)
        body.addWidget(dock, 0, Qt.AlignmentFlag.AlignHCenter)
        row.addWidget(stage, 1)
        return page

    def build_messages(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(310)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label('Messages', 'h2'), 1)
        self.chat_refresh = Button('refresh', variant='ghost', size=38, round_=True, tip='Refresh messages')
        self.chat_refresh.clicked.connect(self.load_conversations)
        head.addWidget(self.chat_refresh)
        side.addLayout(head)
        side.addSpacing(14)
        side.addWidget(label('CONVERSATIONS', 'section'))
        self.chat_list = QListWidget()
        self.chat_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.chat_list.itemSelectionChanged.connect(self.open_conversation)
        side.addWidget(self.chat_list, 1)
        self.chat_hint = label('', 'small')
        self.chat_hint.setWordWrap(True)
        side.addWidget(self.chat_hint)
        row.addWidget(sidebar)

        stage = Stage()
        body = QVBoxLayout(stage)
        body.setContentsMargins(40, 28, 40, 24)
        body.setSpacing(4)
        self.chat_title, self.chat_sub = label('Messages', 'h1'), label('', 'muted')
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(self.chat_title)
        titles.addWidget(self.chat_sub)
        header.addLayout(titles, 1)
        self.chat_party = Button('headphones', 'Start party', 'soft', 40,  # not accent: reads as a sent bubble
                                 tip='Start a party and invite everyone in this chat')
        self.chat_party.clicked.connect(self.party_from_chat)
        header.addWidget(self.chat_party, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(header)
        body.addSpacing(10)
        self.thread_list = QListWidget()
        self.thread_list.setObjectName('thread')
        self.thread_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.thread_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        body.addWidget(self.thread_list, 1)
        body.addSpacing(10)
        composer = QFrame()
        composer.setObjectName('composerBox')
        line = QHBoxLayout(composer)
        line.setContentsMargins(18, 6, 6, 6)
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName('composer')
        self.chat_input.setPlaceholderText('Write a message')
        self.chat_input.setMaxLength(2000)
        self.chat_input.returnPressed.connect(self.send_message)
        self.chat_input.textChanged.connect(self.update_social)
        self.send_button = Button('send', variant='accent', size=44, round_=True, tip='Send')
        self.send_button.clicked.connect(self.send_message)
        line.addWidget(self.chat_input, 1)
        line.addWidget(self.send_button)
        body.addWidget(composer)
        row.addWidget(stage, 1)
        return page

    def build_friends(self):
        page = Stage()
        body = QVBoxLayout(page)
        body.setContentsMargins(48, 32, 48, 28)
        body.setSpacing(6)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('Friends', 'h1'))
        self.friends_sub = label('', 'muted')
        heading.addWidget(self.friends_sub)
        top.addLayout(heading, 1)
        tabs = QFrame()
        tabs.setObjectName('chip')
        tab_row = QHBoxLayout(tabs)
        tab_row.setContentsMargins(4, 4, 4, 4)
        tab_row.setSpacing(4)
        self.friend_tabs = []
        for index, (icon, text) in enumerate((('people', 'Friends'), ('add-user', 'Add friend'))):
            tab = Button(icon, text, variant='ghost', size=36)
            tab.setCheckable(True)
            tab.clicked.connect(lambda _=False, i=index: self.friends_tab(i))
            self.friend_tabs.append(tab)
            tab_row.addWidget(tab)
        top.addWidget(tabs, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(top)
        body.addSpacing(16)
        self.friend_pages = QStackedWidget()

        mine = QWidget()
        column = QVBoxLayout(mine)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        self.requests_card = QFrame()
        self.requests_card.setObjectName('card')
        requests = QVBoxLayout(self.requests_card)
        requests.setContentsMargins(20, 16, 20, 14)
        requests.setSpacing(6)
        requests.addWidget(label('FRIEND REQUESTS', 'section'))
        self.request_rows = QVBoxLayout()
        self.request_rows.setSpacing(4)
        requests.addLayout(self.request_rows)
        column.addWidget(self.requests_card)
        card = QFrame()
        card.setObjectName('card')
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 12)
        inner.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label('ALL FRIENDS', 'section'), 1)
        self.friends_refresh = Button('refresh', variant='ghost', size=34, round_=True, tip='Refresh friends')
        self.friends_refresh.clicked.connect(self.load_friends)
        head.addWidget(self.friends_refresh)
        inner.addLayout(head)
        self.friend_filter = QLineEdit()
        self.friend_filter.setPlaceholderText('Filter by Online ID')
        self.friend_filter.textChanged.connect(self.show_friends)
        inner.addWidget(self.friend_filter)
        self.friend_list = QListWidget()
        self.friend_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.friend_list.itemClicked.connect(lambda item: self.row_profile(self.friend_list, item))
        self.friend_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.friend_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        inner.addWidget(self.friend_list, 1)
        self.friend_hint = label('', 'small')
        inner.addWidget(self.friend_hint)
        column.addWidget(card, 1)
        self.friend_pages.addWidget(mine)

        add = QWidget()
        column = QVBoxLayout(add)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        card = QFrame()
        card.setObjectName('card')
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 12)
        inner.setSpacing(10)
        inner.addWidget(label('FIND PLAYERS', 'section'))
        line = QHBoxLayout()
        line.setSpacing(10)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Search by Online ID')
        self.search_input.setMaxLength(32)
        self.search_input.returnPressed.connect(self.search_players)
        self.search_input.textChanged.connect(self.update_social)
        self.search_button = Button('search', 'Search', variant='accent', size=44)
        self.search_button.clicked.connect(self.search_players)
        line.addWidget(self.search_input, 1)
        line.addWidget(self.search_button)
        inner.addLayout(line)
        self.result_list = QListWidget()
        self.result_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.result_list.itemClicked.connect(lambda item: self.row_profile(self.result_list, item))
        self.result_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.result_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        inner.addWidget(self.result_list, 1)
        self.result_hint = label('Type an Online ID and press Search.', 'small')
        inner.addWidget(self.result_hint)
        column.addWidget(card, 1)
        note = label('Requests are sent from the signed-in account. Many requests in a short time '
                     'can get an account flagged.', 'small')
        note.setWordWrap(True)
        column.addWidget(note)
        self.friend_pages.addWidget(add)
        body.addWidget(self.friend_pages, 1)
        self.friends_tab(0)
        return page

    def build_profile(self):
        page = Stage()
        body = QVBoxLayout(page)
        body.setContentsMargins(48, 24, 48, 24)
        body.setSpacing(14)
        top = QHBoxLayout()
        back = Button('back', 'Back', variant='ghost', size=36)
        back.clicked.connect(lambda: self.rail.select(self.profile_return))
        top.addWidget(back)
        top.addStretch()
        self.profile_refresh = Button('refresh', variant='ghost', size=34, round_=True, tip='Refresh profile')
        self.profile_refresh.clicked.connect(lambda: self.open_profile(*self.profile_target))
        top.addWidget(self.profile_refresh)
        body.addLayout(top)

        card = QFrame()
        card.setObjectName('card')
        header = QHBoxLayout(card)
        header.setContentsMargins(24, 20, 24, 20)
        header.setSpacing(20)
        self.profile_face = Avatar('?', 104, ring=False)
        header.addWidget(self.profile_face, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(4)
        line = QHBoxLayout()
        line.setSpacing(8)
        self.profile_name = label('', 'h1')
        line.addWidget(self.profile_name)
        self.profile_badges = QHBoxLayout()
        self.profile_badges.setSpacing(6)
        line.addLayout(self.profile_badges)
        line.addStretch()
        info.addLayout(line)
        self.profile_real = label('', 'muted')
        self.profile_status = label('', 'status')
        self.profile_about = label('', 'muted')
        self.profile_about.setWordWrap(True)
        self.profile_about.setTextFormat(Qt.TextFormat.PlainText)  # written by the profile owner
        for widget in (self.profile_real, self.profile_status, self.profile_about):
            info.addWidget(widget)
        info.addStretch()
        header.addLayout(info, 1)
        self.profile_actions = QVBoxLayout()
        self.profile_actions.setSpacing(8)
        header.addLayout(self.profile_actions)
        body.addWidget(card)

        stats = QHBoxLayout()
        stats.setSpacing(14)

        def stat(title):
            box = QFrame()
            box.setObjectName('card')
            column = QVBoxLayout(box)
            column.setContentsMargins(20, 14, 20, 14)
            column.setSpacing(6)
            column.addWidget(label(title, 'section'))
            return box, column

        box, column = stat('TROPHY LEVEL')
        self.level_label = label('', 'h2')
        self.level_bar = Bar()
        self.level_detail = label('', 'small')
        for widget in (self.level_label, self.level_bar, self.level_detail):
            column.addWidget(widget)
        stats.addWidget(box, 2)
        box, column = stat('TROPHIES')
        self.trophy_box = column
        stats.addWidget(box, 3)
        box, column = stat('FRIENDS')
        self.profile_friends = label('', 'h2')
        column.addWidget(self.profile_friends)
        column.addStretch()
        stats.addWidget(box, 1)
        box, column = stat('GAMES')
        self.profile_games = label('', 'h2')
        column.addWidget(self.profile_games)
        column.addStretch()
        stats.addWidget(box, 1)
        body.addLayout(stats)

        tabs = QFrame()
        tabs.setObjectName('chip')
        tab_row = QHBoxLayout(tabs)
        tab_row.setContentsMargins(4, 4, 4, 4)
        tab_row.setSpacing(4)
        self.profile_tabs = []
        for index, (icon, text) in enumerate((('gamepad', 'Games'), ('trophy', 'Trophies'))):
            tab = Button(icon, text, variant='ghost', size=36)
            tab.setCheckable(True)
            tab.clicked.connect(lambda _=False, i=index: self.profile_tab(i))
            self.profile_tabs.append(tab)
            tab_row.addWidget(tab)
        tab_line = QHBoxLayout()
        tab_line.addWidget(tabs)
        tab_line.addStretch()
        self.profile_hint = label('', 'small')
        tab_line.addWidget(self.profile_hint)
        body.addLayout(tab_line)

        self.profile_pages = QStackedWidget()
        self.games_list, self.titles_list, self.trophy_list = QListWidget(), QListWidget(), QListWidget()
        for view in (self.games_list, self.titles_list, self.trophy_list):
            view.setSelectionMode(QListWidget.SelectionMode.NoSelection)
            view.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.titles_list.itemClicked.connect(self.title_clicked)
        self.titles_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_pages.addWidget(self.games_list)
        self.profile_pages.addWidget(self.titles_list)
        detail = QWidget()
        column = QVBoxLayout(detail)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        head = QHBoxLayout()
        to_titles = Button('back', 'All trophies', variant='ghost', size=34)
        to_titles.clicked.connect(lambda: self.profile_tab(1))
        head.addWidget(to_titles)
        self.trophy_title = label('', 'h3')
        head.addWidget(self.trophy_title, 1)
        column.addLayout(head)
        column.addWidget(self.trophy_list, 1)
        self.profile_pages.addWidget(detail)
        body.addWidget(self.profile_pages, 1)
        self.profile_tab(0)
        return page

    def build_settings(self):
        page = Stage()
        body = QVBoxLayout(page)
        body.setContentsMargins(48, 32, 48, 28)
        body.setSpacing(6)
        body.addWidget(label('Settings', 'h1'))
        body.addWidget(label('Audio devices and connection details. Devices lock while a call is active.', 'muted'))
        body.addSpacing(18)
        body.addWidget(label('AUDIO', 'section'))
        body.addSpacing(6)
        cards = QHBoxLayout()
        cards.setSpacing(16)
        self.inputs, self.outputs = self.audio_devices()
        self.input_box, self.output_box = QComboBox(), QComboBox()
        self.meter = Meter()
        for box, items, icon_title, extra in ((self.input_box, self.inputs, 'Microphone', self.meter),
                                              (self.output_box, self.outputs, 'Speakers or headphones', None)):
            for text, index in items:
                box.addItem(text, index)
            card = QFrame()
            card.setObjectName('card')
            column = QVBoxLayout(card)
            column.setContentsMargins(20, 18, 20, 20)
            column.setSpacing(10)
            column.addWidget(label(icon_title, 'h3'))
            column.addWidget(box)
            if extra:
                column.addWidget(extra)
                self.meter_hint = label('Input level shows during a call.', 'small')
                column.addWidget(self.meter_hint)
            column.addStretch()
            cards.addWidget(card, 1)
        body.addLayout(cards)
        body.addSpacing(22)
        body.addWidget(label('CONNECTION LOG', 'section'))
        body.addSpacing(6)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(250)
        self.log_box.setPlaceholderText('Signaling and WebRTC events appear here during a call.')
        body.addWidget(self.log_box, 1)
        body.addSpacing(10)
        body.addWidget(label('Unofficial app · Not affiliated with or endorsed by any console maker', 'small'))
        return page

    @staticmethod
    def audio_devices():
        inputs, outputs = [('System default', None)], [('System default', None)]
        try:
            import sounddevice as sd
            for index, device in enumerate(sd.query_devices()):
                value = (f"{device['name']}", index)
                if device['max_input_channels']:
                    inputs.append(value)
                if device['max_output_channels']:
                    outputs.append(value)
        except Exception:
            pass
        return inputs, outputs

    # View updates

    def show_blocked(self, blocked):
        self.blocked.setVisible(blocked)
        if blocked:
            self.set_status('Parties blocked', 'error')

    def set_status(self, text, state='idle'):
        self.status.setText(text)
        color = {'idle': TEXT3, 'busy': ACCENT, 'live': LIVE, 'error': DANGER}[state]
        self.dot.set_state(color, state == 'busy')

    def party_row(self, party):
        host = party.get('owner', {}).get('onlineId', '?')
        count = len(party.get('members', []))
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(12)
        row.addWidget(Avatar(host, 40, ring=False))
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(host, 'h3'))
        text.addWidget(label(f'{count} in party · Invited you', 'small'))
        row.addLayout(text, 1)
        for child in widget.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return widget

    def selected_party(self):
        index = self.party_list.currentRow()
        return self.parties[index] if 0 <= index < len(self.parties) else None

    def show_party(self):
        party = self.selected_party()
        if self.hosting:
            self.party_title.setText('Your party')
            names = (self.control.members if self.control and self.control.members else None) or [self.me]
            self.update_subtitle(len(names))
            self.stage_pages.setCurrentIndex(1)
            self.set_roster(names)
        elif party is None:
            self.party_title.setText('No party selected')
            self.party_sub.setText('Your invitations are on the left.')
            self.stage_pages.setCurrentIndex(0)
            self.set_roster([])
        else:
            host = party.get('owner', {}).get('onlineId', '?')
            self.party_title.setText(host + "'s party")
            names = [m.get('onlineId') or 'Player' for m in party.get('members', []) if isinstance(m, dict)]
            self.update_subtitle(len(names))
            self.stage_pages.setCurrentIndex(1)
            self.set_roster(names)
        self.update_buttons()

    def update_subtitle(self, count):
        text = f'{count} member' + ('' if count == 1 else 's')
        if self.connected_at:
            seconds = int(time.monotonic() - self.connected_at)
            text += f' · Voice live {seconds // 60:02d}:{seconds % 60:02d}'
        self.party_sub.setText(text)

    def set_roster(self, names):
        if names == self.roster:
            return
        self.roster = list(names)
        while self.members_grid.count():
            self.members_grid.takeAt(0).widget().deleteLater()
        self.avatars, self.member_status = {}, {}
        party = self.selected_party() or {}
        host = self.me if self.hosting else party.get('owner', {}).get('onlineId')
        columns = 4 if len(names) > 3 else max(1, len(names))
        for position, name in enumerate(names):
            tile = QWidget()
            column = QVBoxLayout(tile)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(4)
            face = Avatar(name, 96)
            face.muted = name == self.me and self.control is not None and self.control.muted.is_set()
            if name != self.me:
                face.on_click = lambda n=name: self.toggle_silence(n)
            self.avatars[name] = face
            column.addWidget(face, 0, Qt.AlignmentFlag.AlignHCenter)
            caption = label(name, 'h3')
            caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(caption)
            status = label('', 'muted')
            status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            status.setFixedHeight(18)
            self.member_status[name] = status
            column.addWidget(status)
            role = 'Host' if name == host else 'You' if name == self.me else ''
            if role:
                tag = label(role, 'tag')
                column.addWidget(tag, 0, Qt.AlignmentFlag.AlignHCenter)
            else:
                column.addSpacing(18)
            self.members_grid.addWidget(tile, position // columns, position % columns)
        if self.control is not None:
            self.show_invites()  # people who joined drop off the invite list
        self.fit_share()

    def fit_share(self):
        """Give the shared screen what the member tiles leave free."""
        rows = max(1, math.ceil(len(self.roster) / 4))
        self.share_panel.fit(self.stage_pages.height() - 40 - rows * 196 - (rows - 1) * 22, self.stage_pages.width())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_share()

    def tick(self):
        control = self.control
        if control is None:
            return
        if control.members:
            self.want_faces(control.accounts)
            self.set_roster(control.members)
        for name, face in self.avatars.items():
            face.set_level(control.mic_level if name == self.me else control.levels.get(name, 0.0))
            if name == self.me:
                face.set_flags(None, control.muted.is_set(), False)
            else:
                state = control.states.get(name)  # None until the worker publishes states
                face.set_flags(state, name in control.remote_muted, name in self.silenced)
                self.set_member_status(name, state)
            face.tick()
        self.meter.set_level(control.mic_level)
        self.share_panel.sync(control)
        self.update_subtitle(len(self.roster))

    def set_member_status(self, name, state):
        status = self.member_status.get(name)
        if status is None:
            return
        text, color = {'connecting': ('Connecting…', TEXT2), 'disconnected': ('Disconnected · no audio', DANGER)}.get(
            state, ('Muted for you', TEXT2) if name in self.silenced else
            ('Muted', TEXT2) if name in self.control.remote_muted else
            ('Sharing screen', LIVE) if name in self.control.sharing else ('', TEXT2))
        if status.text() != text:
            status.setText(text)
            status.setStyleSheet(f'color: {color}; font-size: 12px;')  # app QSS overrides setFont
        self.avatars[name].setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatars[name].setToolTip(('Unmute' if name in self.silenced else 'Mute') + f' {name} for you')

    def toggle_silence(self, name):
        if self.control is None:
            return
        self.silenced ^= {name}
        self.control.silenced = set(self.silenced)  # replaced whole; the worker only reads it

    def update_buttons(self):
        self.refresh_button.setEnabled(not self.busy and bool(self.me))
        self.empty_refresh.setEnabled(not self.busy)
        self.join_button.setEnabled(not self.busy and bool(self.me) and self.selected_party() is not None)
        self.accounts_body.setEnabled(not self.busy)
        self.update_sign_in()
        active = self.busy and self.control is not None
        self.join_button.setVisible(not active)
        self.leave_button.setVisible(active)
        self.leave_button.setEnabled(active)
        self.mute_button.setVisible(active)
        self.mute_button.setEnabled(active)
        self.party_list.setEnabled(not active)
        self.start_button.setVisible(not active)
        self.start_button.setEnabled(not self.busy and bool(self.me))
        self.side_pages.setCurrentIndex(1 if active else 0)
        self.input_box.setEnabled(not self.busy)
        self.update_social()
        self.output_box.setEnabled(not self.busy)
        self.meter_hint.setVisible(not active)

    # Session control

    def log(self, message):
        self.events.put(('log', message))
        print(message, flush=True)
        write_log_file(message)

    def start_worker(self, action):
        if self.busy:
            return
        self.busy = True
        self.update_buttons()

        def work():
            try:
                action()
            except Exception as exc:
                if isinstance(exc, AuthError):
                    # Auth messages can echo redirect URLs; keep them out of the UI.
                    message = 'Sign-in failed. The npsso value is wrong or expired.'
                else:
                    message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                self.events.put(('error', message))
                print('Session stopped:', message, flush=True)
                if not isinstance(exc, AuthError):
                    write_log_file('Session stopped:\n' + traceback.format_exc())
            finally:
                self.events.put(('done', None))

        self.worker = threading.Thread(target=work, name='party-session', daemon=False)
        self.worker.start()

    def refresh(self, quiet=False):
        # quiet: after a call, keep the call's final status (e.g. an error) on screen.
        self.quiet_refresh = quiet
        if not quiet:
            self.set_status('Loading invitations…', 'busy')

        def action():
            token = sign_in().access_token
            account = account_id_from_token(token)
            if not account_allowed(account):
                raise RuntimeError('This account has not confirmed the ban-risk warning; add it on the Accounts page')
            if active_account() is None and adopt_legacy_npsso(account, self.online_id(token, account)):
                self.events.put(('accounts', None))
            self.events.put(('parties', asyncio.run(discover(token))))

        self.start_worker(action)

    # Accounts

    @staticmethod
    def online_id(token, account):
        profile = get_own_profile(token, account)
        return profile.get('profile', profile).get('onlineId') or account

    def build_accounts(self):
        page = Stage()
        body = QVBoxLayout(page)
        body.setContentsMargins(48, 32, 48, 28)
        body.setSpacing(6)
        body.addWidget(label('Accounts', 'h1'))
        body.addWidget(label('Switch between sign-ins saved on this computer.', 'muted'))
        body.addSpacing(18)
        self.accounts_body = QWidget()
        columns = QHBoxLayout(self.accounts_body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)

        saved = QFrame()
        saved.setObjectName('card')
        left = QVBoxLayout(saved)
        left.setContentsMargins(20, 18, 20, 20)
        left.setSpacing(10)
        left.addWidget(label('SAVED ACCOUNTS', 'section'))
        self.account_rows = QVBoxLayout()
        self.account_rows.setSpacing(6)
        left.addLayout(self.account_rows)
        left.addStretch()
        note = label('Signing out deletes the saved npsso from this computer. It does not sign '
                     'you out on other devices.', 'small')
        note.setWordWrap(True)
        left.addWidget(note)
        columns.addWidget(saved, 1)

        form = QFrame()
        form.setObjectName('card')
        right = QVBoxLayout(form)
        right.setContentsMargins(20, 18, 20, 20)
        right.setSpacing(10)
        right.addWidget(label('ADD AN ACCOUNT', 'section'))
        steps = label(f'1. Sign in to playstation.com in your browser.<br>'
                      f'2. Open <a style="color:{ACCENT}; text-decoration:none" href="{NPSSO_URL}">{NPSSO_URL}</a><br>'
                      f'3. Copy the <b>npsso</b> value and paste it below.', 'muted')
        steps.setOpenExternalLinks(True)
        steps.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        right.addWidget(steps)
        self.npsso_input = QLineEdit()
        self.npsso_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.npsso_input.setPlaceholderText('Paste npsso value')
        self.npsso_input.textChanged.connect(self.update_sign_in)
        right.addWidget(self.npsso_input)
        warn = QFrame()
        warn.setObjectName('warn')
        warn_box = QVBoxLayout(warn)
        warn_box.setContentsMargins(14, 12, 14, 12)
        warn_text = label('This is an unofficial app. Using it breaks the network\'s terms of service, '
                          'and your account can be suspended or banned. Use an account you can afford to lose.',
                          'warnText')
        warn_text.setWordWrap(True)
        warn_box.addWidget(warn_text)
        right.addWidget(warn)
        self.risk_box = QCheckBox('I understand the risk for this account')
        self.risk_box.toggled.connect(self.update_sign_in)
        right.addWidget(self.risk_box)
        self.sign_in_button = Button('user', 'Sign in', variant='accent', size=46)
        self.sign_in_button.clicked.connect(self.add_account)
        right.addWidget(self.sign_in_button, 0, Qt.AlignmentFlag.AlignLeft)
        right.addStretch()
        columns.addWidget(form, 1)
        body.addWidget(self.accounts_body, 1)
        return page

    def update_sign_in(self):
        ready = bool(self.npsso_input.text().strip()) and self.risk_box.isChecked()
        self.sign_in_button.setEnabled(ready and not self.busy)

    def account_row(self, account, active):
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(4, 4, 4, 4)
        line.setSpacing(12)
        line.addWidget(Avatar(account['online_id'], 40, ring=False))
        name = QVBoxLayout()
        name.setSpacing(1)
        name.addWidget(label(account['online_id'], 'h3'))
        name.addWidget(label('Active' if active else 'Saved', 'small'))
        line.addLayout(name, 1)
        if active:
            mine = Button('user', 'Profile', variant='soft', size=36)
            mine.clicked.connect(lambda: self.open_profile(account['account_id'], account['online_id']))
            line.addWidget(mine)
        if not active:
            switch = Button(text='Switch', variant='soft', size=36)
            switch.clicked.connect(lambda: self.switch_account(account))
            line.addWidget(switch)
        out = Button(text='Sign out', variant='ghost', size=36)
        out.clicked.connect(lambda: self.sign_out(account))
        line.addWidget(out)
        return row

    def reload_account(self):
        self.seen_invites = self.seen_requests = None  # the first load per account never notifies
        accounts = list_accounts()
        active = active_account()
        current = next((a for a in accounts if a['account_id'] == active), None)
        legacy = current is None and (os.path.exists(DEFAULT_NPSSO_PATH) or bool(os.environ.get('PSN_NPSSO')))
        self.me = current['online_id'] if current else 'Saved sign-in' if legacy else None
        self.rail_avatar.name = self.me or '?'
        self.rail_avatar.setToolTip(self.me or 'Not signed in')
        self.rail_avatar.update()
        self.signed_in.setText('Signed in as ' + self.me if self.me else 'Not signed in')
        if self.me:
            self.empty_title.setText('Ready when your friends are')
            who = current['online_id'] if current else 'this account'
            self.empty_text.setText(f'Start a party, or invite {who} from a phone or console. New invitations show up here on their own.')
            self.empty_refresh.setText('Check for invitations')
            self.empty_refresh.icon = 'refresh'
        else:
            self.empty_title.setText('Sign in to get started')
            self.empty_text.setText('Add an account to see party invitations.')
            self.empty_refresh.setText('Add an account')
            self.empty_refresh.icon = 'user'
        self.empty_refresh.updateGeometry()
        while self.account_rows.count():
            self.account_rows.takeAt(0).widget().deleteLater()
        for account in accounts:
            self.account_rows.addWidget(self.account_row(account, account['account_id'] == active))
        if not accounts:
            hint = label('Legacy sign-in file found. It moves here on the next refresh.' if legacy
                         else 'No accounts yet.', 'small')
            hint.setWordWrap(True)
            self.account_rows.addWidget(hint)
        self.parties = []
        self.party_list.clear()
        self.list_hint.setVisible(True)
        self.roster = None
        self.show_party()
        self.reset_social()

    def add_account(self):
        npsso = self.npsso_input.text().strip()
        if not npsso or not self.risk_box.isChecked():
            return
        self.set_status('Signing in…', 'busy')

        def action():
            token = sign_in(npsso).access_token
            account = account_id_from_token(token)
            save_account(account, self.online_id(token, account), npsso)
            self.events.put(('signed-in', None))

        self.start_worker(action)

    def switch_account(self, account):
        set_active(account['account_id'])
        self.reload_account()
        self.set_status('Switched to ' + account['online_id'])
        self.rail.select(PARTIES)

    def sign_out(self, account):
        answer = QMessageBox.question(
            self, 'Sign out', f"Sign out of {account['online_id']}? You will need a new npsso to sign in again.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        was_active = account['account_id'] == active_account()
        remove_account(account['account_id'])
        remaining = list_accounts()
        if was_active and remaining:
            set_active(remaining[0]['account_id'])
        self.reload_account()
        self.set_status('Signed out of ' + account['online_id'])

    # Messages and friends

    def set_parties(self, value):
        self.want_faces({str(m['accountId']): m.get('onlineId') for p in value for m in p.get('members', [])
                         if isinstance(m, dict) and m.get('accountId')})
        selected = (self.selected_party() or {}).get('sessionId')
        shape = lambda parties: [(p.get('sessionId'), [m.get('onlineId') for m in p.get('members', [])
                                                       if isinstance(m, dict)]) for p in parties]
        if shape(value) == shape(self.parties) and self.party_list.count() == len(value):
            self.parties = value
            return  # unchanged: no list rebuild, so no flicker every poll
        self.parties = value
        self.party_list.clear()
        for party in value:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 62))
            self.party_list.addItem(item)
            self.party_list.setItemWidget(item, self.party_row(party))
        self.list_hint.setVisible(not value)
        ids = [p.get('sessionId') for p in value]
        if value:
            self.party_list.setCurrentRow(ids.index(selected) if selected in ids else 0)
        self.roster = None
        self.show_party()

    def poll_invitations(self):
        """Quiet background reload while idle, also from the tray, so new invitations notify."""
        if self.polling or self.busy or not self.me or self.offline:
            return
        self.polling = True
        self.want_faces({a['account_id']: a['online_id'] for a in list_accounts()})
        if 'parties-poll' not in self.loaded and self.blocked.isHidden():  # first load for this account: say so
            self.set_status('Checking for invitations…', 'busy')
        self.social('parties-poll', lambda: asyncio.run(discover(self.client().token())))

    def page_changed(self, index):
        self.pages.setCurrentIndex(index)
        if index == PARTIES:
            self.poll_invitations()
        if index == MESSAGES and 'conversations' not in self.loaded:
            self.load_conversations()
        elif index == FRIENDS and 'friends' not in self.loaded:
            self.load_friends()

    def want_faces(self, accounts):
        """Fetch profile pictures for {accountId: onlineId} not requested before."""
        todo = {a: n for a, n in accounts.items() if a and n and n not in self.faces_wanted}
        if todo and self.me and not self.offline:
            self.faces_wanted |= set(todo.values())
            self.social('faces', lambda: self.client().faces(todo))

    def want_images(self, urls):
        """Fetch game and trophy art not requested before, in chunks so the first rows fill in quickly."""
        todo = [u for u in dict.fromkeys(urls) if u and u not in IMAGES and u not in self.images_wanted]
        if not todo or not self.me or self.offline:
            return
        self.images_wanted |= set(todo)
        generation = self.generation

        def work():
            for start in range(0, len(todo), 16):
                self.events.put(('social', (generation, 'images', social.fetch_images(todo[start:start + 16]))))

        threading.Thread(target=work, name='social-images', daemon=True).start()

    # Profiles

    def open_profile(self, account_id, online_id):
        if not account_id:
            return
        if self.pages.currentIndex() != PROFILE:
            self.profile_return = self.pages.currentIndex()
        self.profile_target, self.profile_title = (str(account_id), online_id), None
        self.show_profile({'account_id': str(account_id), 'online_id': online_id, 'loading': True})
        self.rail.select(PROFILE)
        self.profile_tab(0)
        self.social('profile', lambda: self.client().profile(str(account_id)))

    def profile_tab(self, index):
        for position, tab in enumerate(self.profile_tabs):
            tab.setChecked(position == index)
        self.profile_pages.setCurrentIndex(index)
        self.profile_hint.setText(self.profile_hints[index])

    def show_profile(self, data):
        loading = data.get('loading')
        name = data.get('online_id') or '?'
        self.profile_face.name = name
        self.profile_face.presence = None if loading else data.get('presence')
        self.profile_face.update()
        self.want_faces({data['account_id']: name})
        self.profile_name.setText(name)
        while self.profile_badges.count():
            self.profile_badges.takeAt(0).widget().deleteLater()
        for flag, text in (('plus', 'PS Plus'), ('verified', 'Verified')):
            if data.get(flag):
                self.profile_badges.addWidget(label(text, 'tag'))
        self.profile_real.setText(data.get('name') or '')
        self.profile_real.setVisible(bool(data.get('name')))
        if loading:
            status = 'Loading profile…'
        elif data.get('presence') == 'online':
            status = data.get('status') or 'Online'
        else:
            last = ago(data.get('last_online'))
            status = 'Offline' + (f' · last online {last}' if last else '')
        self.profile_status.setText(status)
        self.profile_about.setText(data.get('about') or '')
        self.profile_about.setVisible(bool(data.get('about')))
        while self.profile_actions.count():
            item = self.profile_actions.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        relation = data.get('relation')
        friend = {'account_id': data['account_id'], 'online_id': name}
        if relation == 'none':
            add = Button('add-user', 'Add friend', variant='accent', size=38)
            add.clicked.connect(lambda: self.profile_request(friend, add))
            self.profile_actions.addWidget(add)
        elif relation == 'requested':
            sent = Button('check', 'Request sent', variant='soft', size=38)
            sent.setEnabled(False)
            self.profile_actions.addWidget(sent)
        if relation == 'friend':
            chat = Button('chat', 'Message', variant='soft', size=38)
            chat.clicked.connect(lambda: self.message_friend(friend))
            self.profile_actions.addWidget(chat)
        if relation in ('friend', 'me'):
            self.profile_actions.addWidget(label('Friend' if relation == 'friend' else 'You', 'tag'),
                                           0, Qt.AlignmentFlag.AlignHCenter)
        self.profile_actions.addStretch()

        trophies = data.get('trophies')
        hidden = '' if loading else 'Private'
        self.level_label.setText(f"Level {trophies['level']} · {tier_name(trophies['tier'])}" if trophies else hidden)
        self.level_bar.set_value((trophies or {}).get('progress', 0) / 100)
        self.level_detail.setText(f"{trophies['progress']}% to level {trophies['level'] + 1}" if trophies else '')
        while self.trophy_box.count() > 1:
            item = self.trophy_box.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        if trophies:
            self.trophy_box.addWidget(trophy_counts(trophies, size=16))
            self.trophy_box.addWidget(label(f"{sum(trophies[k] for k in TROPHY_COLORS)} trophies earned", 'small'))
        else:
            self.trophy_box.addWidget(label(hidden, 'h2'))
        self.trophy_box.addStretch()
        friends_count, games_total = data.get('friends_count'), data.get('games_total')
        self.profile_friends.setText(hidden if friends_count is None else str(friends_count))
        self.profile_games.setText(hidden if games_total is None else str(games_total))

        games, titles = data.get('games'), data.get('titles')
        self.fill(self.games_list, [self.game_row(g) for g in games or []], height=68)
        self.fill(self.titles_list, [self.title_row(t) for t in titles or []], height=68)
        for index, title in enumerate(titles or []):
            self.titles_list.item(index).setData(Qt.ItemDataRole.UserRole, title)
        titles_total = data.get('titles_total')
        self.profile_hints = ['' if loading else 'Game list is private.' if games is None else
                              f'{len(games)} of {games_total} games' if games_total and games_total > len(games)
                              else '' if games else 'No games played yet.',
                              '' if loading else 'Trophies are private.' if titles is None else
                              f'{len(titles)} of {titles_total} games' if titles_total and titles_total > len(titles)
                              else '' if titles else 'No trophies yet.', '']
        self.profile_hint.setText(self.profile_hints[self.profile_pages.currentIndex()])
        games_art, titles_art = [g['icon'] for g in games or []], [t['icon'] for t in titles or []]
        # First screenful of both tabs before the rest, so switching tabs shows art quickly.
        self.want_images(games_art[:12] + titles_art[:12] + games_art[12:] + titles_art[12:])

    def game_row(self, game):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(6, 6, 10, 6)
        row.setSpacing(14)
        row.addWidget(Picture(game['icon'], 52))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(game['name'], 'h3'))
        played = ago(game.get('last_played'))
        text.addWidget(label(' · '.join(p for p in (game.get('platform'), played and 'Last played ' + played) if p),
                             'small'))
        row.addLayout(text, 1)
        if game.get('play_time'):
            row.addWidget(label(game['play_time'], 'muted'))
        return widget

    def title_row(self, title):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(6, 6, 10, 6)
        row.setSpacing(14)
        row.addWidget(Picture(title['icon'], 92, 52))
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(label(title['name'], 'h3'))
        text.addWidget(trophy_counts(title['earned'], title['defined']))
        row.addLayout(text, 1)
        progress = QVBoxLayout()
        progress.setSpacing(4)
        progress.addWidget(label(f"{title['progress']}%", 'h3'), 0, Qt.AlignmentFlag.AlignRight)
        bar = Bar(LIVE if title['progress'] == 100 else ACCENT)
        bar.setFixedWidth(140)
        bar.set_value(title['progress'] / 100)
        progress.addWidget(bar)
        progress.addWidget(label(' · '.join(p for p in (title.get('platform'), ago(title.get('updated'))) if p),
                                 'small'), 0, Qt.AlignmentFlag.AlignRight)
        row.addLayout(progress)
        return widget

    def trophy_row(self, trophy):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(6, 6, 10, 6)
        row.setSpacing(14)
        art = Picture(trophy['icon'], 52)
        if not trophy['earned']:
            art.setToolTip('Not earned yet')
        row.addWidget(art)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = label(trophy['name'], 'h3' if trophy['earned'] else 'muted')
        text.addWidget(name)
        detail = label(trophy['detail'], 'small')
        detail.setTextFormat(Qt.TextFormat.PlainText)
        text.addWidget(detail)
        row.addLayout(text, 1)
        side = QVBoxLayout()
        side.setSpacing(2)
        kind = QHBoxLayout()
        kind.setSpacing(6)
        kind.addStretch()
        kind.addWidget(Trophy(trophy['type'], 22))
        kind.addWidget(label((trophy['type'] or '').title(), 'small'))
        side.addLayout(kind)
        when = ago(trophy.get('earned_at'))
        side.addWidget(label(('Earned ' + when) if trophy['earned'] and when else 'Not earned', 'small'),
                       0, Qt.AlignmentFlag.AlignRight)
        if trophy.get('rate'):
            side.addWidget(label(f"{trophy['rate']}% of players", 'small'), 0, Qt.AlignmentFlag.AlignRight)
        row.addLayout(side)
        return widget

    def title_clicked(self, item):
        title = item.data(Qt.ItemDataRole.UserRole)
        if not title or not self.profile_target:
            return
        account_id = self.profile_target[0]
        self.profile_title = title['id']
        self.trophy_title.setText(title['name'])
        self.trophy_list.clear()
        self.profile_hint.setText('Loading trophies…')
        self.profile_pages.setCurrentIndex(2)
        self.social('title-trophies', lambda: (account_id, title['id'], self.client().trophies(account_id, title)))

    def profile_request(self, friend, button):
        button.setEnabled(False)
        button.setText('Sending…')
        self.social('profile-request', lambda: (friend['account_id'], self.client().send_request(friend['account_id'])))

    def row_profile(self, view, item):
        target = getattr(view.itemWidget(item), 'profile', None)
        if target:
            self.open_profile(*target)

    def client(self):
        if self.social_client is None:
            self.social_client = social.Social()
        return self.social_client

    def social(self, kind, action):
        """Run one social API call off the UI thread; the result arrives in poll()."""
        if not self.me or self.offline:
            return
        self.loaded.add(kind)
        generation = self.generation

        def work():
            try:
                value = action()
            except Exception as exc:
                if isinstance(exc, AuthError):
                    message = 'Sign-in failed. The npsso value is wrong or expired.'
                elif isinstance(exc, urllib.error.HTTPError):
                    message = f'The server refused the request (HTTP {exc.code}).'
                elif isinstance(exc, (RuntimeError, NotImplementedError)):
                    message = str(exc)
                else:
                    message = type(exc).__name__
                self.events.put(('social-error', (generation, kind, message)))
                print('Social call failed:', kind, message, flush=True)
            else:
                self.events.put(('social', (generation, kind, value)))

        threading.Thread(target=work, name='social-' + kind, daemon=True).start()

    def update_social(self):
        signed = bool(self.me)
        self.chat_refresh.setEnabled(signed)
        self.friends_refresh.setEnabled(signed)
        self.chat_input.setEnabled(signed and self.thread is not None)
        self.send_button.setEnabled(signed and self.thread is not None and bool(self.chat_input.text().strip()))
        self.search_button.setEnabled(signed and len(self.search_input.text().strip()) >= 3)
        self.chat_party.setVisible(self.thread is not None)
        self.chat_party.setEnabled(signed and not self.busy and self.thread is not None)

    @staticmethod
    def person_row(name, detail, presence=None, buttons=(), profile=None, open_profile=None):
        widget = QWidget()
        widget.profile = profile  # (accountId, onlineId): clicking the row in a list opens the profile
        row = QHBoxLayout(widget)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(12)
        face = Avatar(name, 40, ring=False)
        face.presence = presence
        if profile and open_profile:
            face.setCursor(Qt.CursorShape.PointingHandCursor)
            face.setToolTip('View profile')
            face.on_click = lambda: open_profile(*profile)
        row.addWidget(face)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(name, 'h3'))
        text.addWidget(label(detail, 'small'))
        row.addLayout(text, 1)
        for button in buttons:
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    @staticmethod
    def fill(view, widgets, height=62):
        view.clear()
        for widget in widgets:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, height or widget.sizeHint().height()))
            view.addItem(item)
            view.setItemWidget(item, widget)

    def conversation_row(self, conversation):
        widget = self.person_row(conversation['name'], conversation.get('last') or 'No messages yet')
        if conversation.get('unread'):
            badge = label(str(conversation['unread']), 'unread')
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedHeight(20)
            badge.setMinimumWidth(20)
            widget.layout().addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        for child in widget.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return widget

    def bubble(self, message):
        sender = message.get('sender') or '?'
        mine = sender == self.me
        widget = QWidget()
        if message.get('system'):
            # Automatic group events (created, member added): centred note, not a chat bubble.
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 6, 6, 6)
            note = QLabel(message.get('body', ''))
            note.setTextFormat(Qt.TextFormat.PlainText)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note.setStyleSheet(f'background: transparent; color: {TEXT2}; border: 1px solid {LINE}; '
                               f'border-radius: 12px; padding: 5px 12px; font-size: 12px;')  # app QSS overrides setFont
            font = QFont(self.font())
            font.setPixelSize(12)
            note.setFont(font)
            box = QFontMetrics(font).boundingRect(0, 0, 500, 100000, Qt.TextFlag.TextWordWrap, note.text())
            note.setWordWrap(True)
            note.setFixedSize(box.width() + 32, box.height() + 14)  # padding 24 + border 2, plus wrap slack
            row.addStretch()
            row.addWidget(note)
            row.addStretch()
            return widget
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 3, 6, 3)
        row.setSpacing(10)
        column = QVBoxLayout()
        column.setSpacing(3)
        if not mine:
            column.addWidget(label(sender, 'small'))
        text = QLabel(message.get('body', ''))
        text.setWordWrap(True)
        text.setMaximumWidth(460)
        text.setTextFormat(Qt.TextFormat.PlainText)  # message bodies are untrusted text
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setStyleSheet(f'background: {ACCENT if mine else THEIRS}; color: {TEXT}; '
                           f'border-radius: 16px; padding: 9px 14px;')
        # Word-wrapped labels report a wrong height inside list items; size the bubble explicitly.
        font = QFont(self.font())
        font.setPixelSize(14)
        text.setFont(font)
        box = QFontMetrics(font).boundingRect(0, 0, 428, 100000, Qt.TextFlag.TextWordWrap, text.text())
        text.setFixedSize(box.width() + 32, box.height() + 20)
        column.addWidget(text, 0, Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft)
        if mine:
            row.addStretch()
            row.addLayout(column)
        else:
            face = Avatar(sender, 32, ring=False)
            account = next((a for a, n in (self.thread or {}).get('names', {}).items() if n == sender), None)
            if account:
                face.setCursor(Qt.CursorShape.PointingHandCursor)
                face.setToolTip('View profile')
                face.on_click = lambda: self.open_profile(account, sender)
            row.addWidget(face, 0, Qt.AlignmentFlag.AlignBottom)
            row.addLayout(column)
            row.addStretch()
        return widget

    def show_conversations(self):
        self.fill(self.chat_list, [self.conversation_row(c) for c in self.conversations])
        self.chat_hint.setText('' if self.conversations else
                               'No conversations yet. Start one from a friend on the Friends page.')

    def load_conversations(self):
        self.chat_hint.setText('Loading conversations…')
        self.social('conversations', lambda: self.client().conversations())

    def open_conversation(self):
        index = self.chat_list.currentRow()
        if not 0 <= index < len(self.conversations):
            return
        conversation = self.conversations[index]
        self.thread = conversation
        self.chat_title.setText(conversation['name'])
        self.chat_sub.setText('Loading messages…')
        self.thread_list.clear()
        self.update_social()
        self.social('thread', lambda: (conversation['id'], self.client().messages(conversation)))

    def show_thread(self, messages):
        self.fill(self.thread_list, [self.bubble(m) for m in messages], height=None)
        self.thread_list.scrollToBottom()
        members = len(self.thread.get('members', [])) if self.thread else 0
        self.chat_sub.setText(f'{members} member' + ('' if members == 1 else 's') if members else '')

    def send_message(self):
        text = self.chat_input.text().strip()
        conversation = self.thread
        if not text or conversation is None or not self.send_button.isEnabled():
            return
        self.send_button.setEnabled(False)
        self.chat_input.setEnabled(False)
        self.chat_sub.setText('Sending…')
        self.social('sent', lambda: (conversation['id'], self.client().send(conversation, text), text))

    def message_friend(self, friend):
        self.rail.select(MESSAGES)
        name = friend['online_id']
        for index, conversation in enumerate(self.conversations):
            if conversation.get('members') == [name]:
                self.chat_list.setCurrentRow(index)
                return
        self.chat_title.setText(name)
        self.chat_sub.setText('Starting conversation…')
        self.social('opened', lambda: self.client().open_direct(friend))

    def friends_tab(self, index):
        for position, tab in enumerate(self.friend_tabs):
            tab.setChecked(position == index)
        self.friend_pages.setCurrentIndex(index)
        if index == 1:
            self.search_input.setFocus()

    def load_friends(self):
        self.friend_hint.setText('Loading friends…')
        self.social('friends', lambda: (self.client().friends(), self.client().requests()))

    def show_friends(self):
        needle = self.friend_filter.text().strip().lower()
        shown = [f for f in self.friends if needle in f['online_id'].lower()]
        rows = []
        for friend in shown:
            chat = Button('chat', 'Message', variant='soft', size=36)
            chat.clicked.connect(lambda _=False, f=friend: self.message_friend(f))
            presence = friend.get('presence', 'offline')
            rows.append(self.person_row(friend['online_id'], friend.get('status') or presence.title(),
                                        presence, [chat], (friend['account_id'], friend['online_id'])))
        self.fill(self.friend_list, rows)
        online = sum(f.get('presence') == 'online' for f in self.friends)
        self.friends_sub.setText(f'{len(self.friends)} friends · {online} online' if self.friends else
                                 'Your friends list' if self.me else 'Sign in to see your friends.')
        self.friend_hint.setText('' if shown else 'No friend matches that filter.' if self.friends else
                                 'No friends loaded yet.')
        while self.request_rows.count():
            self.request_rows.takeAt(0).widget().deleteLater()
        for request in self.requests:
            accept = Button('check', 'Accept', variant='accent', size=36)
            accept.clicked.connect(lambda _=False, r=request: self.answer_request(r, True))
            decline = Button('close', 'Decline', variant='ghost', size=36)
            decline.clicked.connect(lambda _=False, r=request: self.answer_request(r, False))
            self.request_rows.addWidget(self.person_row(request['online_id'], 'Wants to be friends',
                                                        buttons=[accept, decline],
                                                        profile=(request['account_id'], request['online_id']),
                                                        open_profile=self.open_profile))
        self.requests_card.setVisible(bool(self.requests))

    def answer_request(self, request, accept):
        for button in self.requests_card.findChildren(Button):
            button.setEnabled(False)
        self.social('answered', lambda: (request['account_id'], accept,
                                         self.client().answer_request(request['account_id'], accept)))

    def search_players(self):
        query = self.search_input.text().strip()
        if not self.search_button.isEnabled() or not query:
            return
        self.result_hint.setText('Searching…')
        self.result_list.clear()
        self.social('results', lambda: self.client().search(query))

    def show_results(self):
        rows = []
        for result in self.results:
            relation = result.get('relation', 'none')
            if relation == 'friend':
                rows.append(self.person_row(result['online_id'], 'Already friends', buttons=[label('Friend', 'tag')],
                                            profile=(result['account_id'], result['online_id'])))
                continue
            button = Button('add-user', 'Add friend', variant='accent', size=36)
            if relation == 'requested':
                button.setText('Request sent')
                button.icon = 'check'
                button.variant = 'soft'
                button.setEnabled(False)
            else:
                button.clicked.connect(lambda _=False, r=result, b=button: self.send_friend_request(r, b))
            rows.append(self.person_row(result['online_id'], result.get('detail') or 'Player',
                                        buttons=[button], profile=(result['account_id'], result['online_id'])))
        self.fill(self.result_list, rows)
        self.result_hint.setText(f'{len(self.results)} result' + ('' if len(self.results) == 1 else 's')
                                 if self.results else 'No player found with that Online ID.')

    def send_friend_request(self, result, button):
        button.setEnabled(False)
        button.setText('Sending…')
        self.social('request-sent', lambda: (result['account_id'], self.client().send_request(result['account_id'])))

    def social_result(self, kind, value):
        if kind == 'images':
            for url, data in value.items():
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    IMAGES[url] = pixmap
            for picture in self.findChildren(Picture):
                if picture.url in value:
                    picture.update()
            return
        if kind == 'profile':
            if self.profile_target and value['account_id'] == self.profile_target[0]:
                self.show_profile(value)
            return
        if kind == 'title-trophies':
            account_id, title_id, trophies = value
            if self.profile_target and (account_id, title_id) == (self.profile_target[0], self.profile_title):
                self.fill(self.trophy_list, [self.trophy_row(t) for t in trophies], height=72)
                earned = sum(t['earned'] for t in trophies)
                self.profile_hints[2] = f'{earned} of {len(trophies)} trophies earned'
                self.profile_hint.setText(self.profile_hints[2])
                self.want_images([t['icon'] for t in trophies])
            return
        if kind == 'profile-request':
            if self.profile_target and value[0] == self.profile_target[0]:
                self.open_profile(*self.profile_target)
            return
        if kind == 'faces':
            for name, data in value.items():
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    FACES[name] = pixmap
            for face in self.findChildren(Avatar):
                if face.name in value:
                    face.update()
            return
        if kind == 'parties-poll':
            self.polling = False
            self.show_blocked(False)
            self.notify_invites(value)
            if not self.busy:  # a call or manual refresh owns the list meanwhile
                if self.status.text() == 'Checking for invitations…':
                    self.set_status(f'{len(value)} invitation' + ('' if len(value) == 1 else 's') if value
                                    else 'No active invitations')
                self.set_parties(value)
            return
        if kind == 'conversations':
            current = self.thread['id'] if self.thread else None
            self.conversations = value
            self.want_faces({a: n for c in value for a, n in c.get('names', {}).items()})
            self.show_conversations()
            for index, conversation in enumerate(value):
                if conversation['id'] == current:
                    self.chat_list.setCurrentRow(index)
        elif kind == 'thread':
            conversation_id, messages = value
            if self.thread and self.thread['id'] == conversation_id:
                self.show_thread(messages)
        elif kind == 'sent':
            conversation_id, message, text = value
            if self.thread and self.thread['id'] == conversation_id:
                self.chat_input.clear()
                row = self.bubble(message or {'sender': self.me, 'body': text})
                item = QListWidgetItem()
                item.setSizeHint(QSize(0, row.sizeHint().height()))
                self.thread_list.addItem(item)
                self.thread_list.setItemWidget(item, row)
                self.thread_list.scrollToBottom()
                self.chat_sub.setText('Sent')
        elif kind == 'opened':
            self.conversations.insert(0, value)
            self.show_conversations()
            self.chat_list.setCurrentRow(0)
        elif kind == 'friends':
            self.friends, self.requests = value
            self.notify_requests(self.requests)
            self.want_faces({f['account_id']: f['online_id'] for f in self.friends + self.requests})
            self.friends.sort(key=lambda f: (f.get('presence') != 'online', f['online_id'].lower()))
            self.show_friends()
            self.show_invites()
        elif kind == 'answered':
            account_id, accepted, _ = value
            self.requests = [r for r in self.requests if r['account_id'] != account_id]
            self.show_friends()
            if accepted:
                self.load_friends()
        elif kind == 'results':
            self.results = value
            self.want_faces({r['account_id']: r['online_id'] for r in value})
            self.show_results()
        elif kind == 'request-sent':
            account_id, _ = value
            for result in self.results:
                if result['account_id'] == account_id:
                    result['relation'] = 'requested'
            self.show_results()
        self.update_social()

    def social_failed(self, kind, message):
        self.loaded.discard(kind)
        if kind == 'faces':
            return  # initials stay; no error banner for a missing picture
        if kind in ('profile', 'title-trophies', 'profile-request'):
            self.profile_hint.setText(message)
            if kind == 'profile':
                self.profile_status.setText('Could not load this profile.')
            return
        if kind == 'parties-poll':
            self.polling = False  # stay quiet; the next poll retries
            if message == BLOCKED:
                self.show_blocked(True)
            elif self.status.text() == 'Checking for invitations…':
                self.set_status(message, 'error')
            return
        if kind in ('conversations',):
            self.chat_hint.setText(message)
        elif kind in ('thread', 'sent', 'opened'):
            self.chat_sub.setText(message)
        elif kind in ('friends', 'answered'):
            self.friend_hint.setText(message)
            self.show_friends()
        else:
            self.result_hint.setText(message)
            self.show_results()
        self.update_social()

    def reset_social(self):
        self.generation += 1  # drop late results from the previous account
        self.show_blocked(False)
        self.social_client = None
        self.loaded = set()
        self.conversations, self.thread, self.friends, self.requests, self.results = [], None, [], [], []
        self.chat_title.setText('Messages')
        self.chat_sub.setText('Pick a conversation on the left.' if self.me else 'Sign in to see your messages.')
        self.thread_list.clear()
        self.show_conversations()
        self.chat_hint.setText('' if self.me else 'Not signed in.')
        self.show_friends()
        self.result_list.clear()
        self.result_hint.setText('Type an Online ID and press Search.')
        self.update_social()

    def join(self):
        party = self.selected_party()
        if party is None or self.busy:
            return
        self.hosting = False
        owner = party.get('owner', {}).get('onlineId', '')
        options = VoiceOptions(party['sessionId'], owner,
                               self.input_box.currentData(), self.output_box.currentData())
        self.control = SessionControl()
        self.set_status('Joining ' + owner + "'s party…", 'busy')
        self.start_worker(lambda: asyncio.run(run(options, self.control, self.log)))
        self.ticker.start(33)
        self.show_invites()

    def party_from_chat(self):
        """What the mobile app does from a chat: a normal party plus an invite per member."""
        if self.busy or not self.me or self.thread is None:
            return
        members = [{'account_id': a, 'online_id': n} for a, n in self.thread.get('names', {}).items() if n != self.me]
        self.start_party()
        for friend in members:
            self.invited.add(friend['account_id'])
            self.control.invites.put(friend)  # sent right after the party exists
        self.rail.select(PARTIES)
        self.show_invites()

    def start_party(self):
        if self.busy or not self.me:
            return
        options = VoiceOptions(None, self.me, self.input_box.currentData(), self.output_box.currentData(), create=True)
        self.hosting = True
        self.control = SessionControl()
        self.set_status('Starting your party…', 'busy')
        self.start_worker(lambda: asyncio.run(run(options, self.control, self.log)))
        self.ticker.start(33)
        self.roster = None
        self.show_party()
        self.show_invites()

    def show_invites(self):
        if 'friends' not in self.loaded:
            self.load_friends()
        in_party = set(self.roster or [])
        rows = []
        for friend in self.friends:
            if friend['online_id'] in in_party:
                continue
            sent = friend['account_id'] in self.invited
            button = Button('check' if sent else 'join', 'Invited' if sent else 'Invite',
                            variant='soft' if sent else 'accent', size=34)
            button.setEnabled(not sent)
            button.clicked.connect(lambda _=False, f=friend, b=button: self.invite_friend(f, b))
            rows.append(self.person_row(friend['online_id'], friend.get('status') or 'Offline',
                                        friend.get('presence'), [button], (friend['account_id'], friend['online_id'])))
        self.fill(self.invite_list, rows)
        self.invite_hint.setText('' if rows else 'Everyone on your friends list is here.' if self.friends
                                 else 'Loading friends…')

    def invite_friend(self, friend, button):
        if self.control is None:
            return
        self.invited.add(friend['account_id'])
        self.control.invites.put(friend)
        button.setText('Inviting…')
        button.setEnabled(False)

    def mute(self):
        if self.control:
            if self.control.muted.is_set():
                self.control.muted.clear()
            else:
                self.control.muted.set()
            muted = self.control.muted.is_set()
            self.mute_button.setChecked(muted)
            self.mute_button.setToolTip('Unmute microphone' if muted else 'Mute microphone')
            if self.me in self.avatars:
                self.avatars[self.me].muted = muted
                self.avatars[self.me].update()

    def leave(self):
        if self.control:
            self.control.stop.set()
            self.set_status('Leaving party…', 'busy')
            self.leave_button.setEnabled(False)

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ('log', 'error'):
                self.log_box.appendPlainText(value)
                if kind == 'error' and value == BLOCKED:
                    self.show_blocked(True)
                elif kind == 'error':
                    self.set_status('Stopped · ' + value, 'error')
                elif value == 'WebRTC: connected':
                    self.connected_at = self.connected_at or time.monotonic()
                    self.set_status('Connected · voice is live', 'live')
                elif value == 'WebRTC: failed':
                    self.set_status('Direct connection failed · waiting for peer retry', 'error')
                elif value == 'WebRTC: connecting':
                    self.set_status('Connecting audio…', 'busy')
                elif value.startswith('LEAVE -> HTTP 204'):
                    self.set_status('Left party')
                elif value.startswith('INVITE '):
                    name, _, code = value[len('INVITE '):].partition(' -> HTTP ')
                    if code.startswith('2'):
                        self.set_status('Invite sent to ' + name, 'live')
                    else:
                        self.set_status(f'Invite to {name} failed (HTTP {code})', 'error')
                        self.invited -= {f['account_id'] for f in self.friends if f['online_id'] == name}  # allow retry
                    self.show_invites()
            elif kind == 'parties':
                self.show_blocked(False)
                self.notify_invites(value)
                self.set_parties(value)
                if not getattr(self, 'quiet_refresh', False):
                    self.set_status(f'{len(value)} invitation' + ('' if len(value) == 1 else 's') if value
                                    else 'No active invitations')
            elif kind in ('social', 'social-error'):
                generation, name, payload = value
                if generation == self.generation:
                    (self.social_result if kind == 'social' else self.social_failed)(name, payload)
            elif kind == 'update':
                self.show_update(*value)
            elif kind == 'accounts':
                self.reload_account()
            elif kind == 'signed-in':
                self.npsso_input.clear()
                self.risk_box.setChecked(False)
                self.reload_account()
                self.set_status('Signed in as ' + self.me, 'live')
                self.rail.select(PARTIES)
            elif kind == 'done':
                was_call = self.control is not None
                self.share_panel.reset()
                self.busy = False
                self.control = None
                self.hosting = False
                self.invited.clear()
                self.silenced.clear()
                self.connected_at = None
                self.ticker.stop()
                self.mute_button.setChecked(False)
                self.meter.set_level(0)
                self.roster = None  # force a rebuild from the invitation data
                self.show_party()
                if was_call and self.me and not self.offline and not self.closing:
                    # Invitations survive a leave, so the party can be rejoined.
                    self.refresh(quiet=True)
        if self.closing and not self.busy:
            self.close()

    def showEvent(self, event):
        # Check right away: the first sign-in alone takes seconds, so waiting a poll period felt idle.
        QTimer.singleShot(0, self.poll_invitations)

    # Updates

    def check_updates(self):
        current = updates.current_version()
        if not current or self.offline:
            return  # source runs have no version file and update with git

        def work():
            try:
                tag, url = updates.latest_release()
            except Exception as exc:
                print('Update check failed:', type(exc).__name__, flush=True)
                return
            if updates.newer(current, tag):
                self.events.put(('update', (current, tag, url)))

        threading.Thread(target=work, name='update-check', daemon=True).start()

    def show_update(self, current, tag, url):
        if tag == self.update_tag:
            return  # already shown (or hidden by the user) for this release
        self.update_tag, self.update_url = tag, url
        self.update_text.setText(f'<b>Update available:</b> VoxChat {tag} is out. You have {current}.')
        self.update_bar.show()
        self.notify('Update available', f'VoxChat {tag} is ready to download.')

    # Tray and notifications

    def setup_tray(self):
        """Closing the window keeps VoxChat in the tray, so calls and notifications keep going."""
        self.tray, self.quitting, self.tray_told, self.notice_page = None, False, False, PARTIES
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return  # no tray (for example plain GNOME): closing the window quits, as before
        self.tray = QSystemTrayIcon(QApplication.windowIcon(), self)
        self.tray.setToolTip('VoxChat')
        menu = QMenu(self)
        menu.addAction('Open VoxChat', self.bring_back)
        menu.addAction('Quit VoxChat', self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and self.bring_back())
        self.tray.messageClicked.connect(lambda: self.bring_back(self.notice_page))
        self.tray.show()
        QApplication.instance().setQuitOnLastWindowClosed(False)

    def bring_back(self, page=None):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if page is not None:
            self.rail.select(page)

    def quit_app(self):
        self.quitting = True
        self.close()

    def notify(self, title, text, page=PARTIES):
        """A system notification, only when VoxChat isn't the window in front."""
        if self.tray and not self.isActiveWindow():
            self.notice_page = page
            self.tray.showMessage(title, text, QApplication.windowIcon(), 8000)
        write_log_file(f'Notification {title!r}: ' + ('shown' if self.tray and not self.isActiveWindow()
                       else 'skipped, the window is in front' if self.tray else 'skipped, no tray'))

    def notify_invites(self, parties):
        ids = {p.get('sessionId') for p in parties}
        if self.seen_invites is not None:
            for party in parties:
                owner = party.get('owner', {}).get('onlineId') or 'A friend'
                if party.get('sessionId') not in self.seen_invites and owner != self.me:
                    self.notify('Party invitation', f'{owner} invited you to a party.')
        else:
            write_log_file(f'Watching for new invitations ({len(ids)} already there)')
        self.seen_invites = (self.seen_invites or set()) | ids

    def notify_requests(self, requests):
        ids = {r['account_id'] for r in requests}
        if self.seen_requests is not None:
            for request in requests:
                if request['account_id'] not in self.seen_requests:
                    self.notify('Friend request', f"{request['online_id']} sent you a friend request.", FRIENDS)
        self.seen_requests = (self.seen_requests or set()) | ids

    def closeEvent(self, event):
        if self.tray and not self.quitting and not self.closing:
            event.ignore()
            self.hide()
            if not self.tray_told:
                self.tray_told = True
                self.notify('VoxChat is still running', 'It stays in the tray. Right-click its icon to quit.')
            return
        if self.busy:
            self.closing = True
            self.leave()
            self.set_status('Closing after network cleanup…', 'busy')
            event.ignore()
        else:
            event.accept()
            QApplication.quit()


def smoke_social(window, page):
    """Synthetic messages and friends for UI previews."""
    me = window.me or 'you'
    window.me = me
    conversations = [
        {'id': 'c1', 'name': 'NightOwl_42', 'members': ['NightOwl_42'], 'last': 'gg, rematch tonight?', 'unread': 2},
        {'id': 'c2', 'name': 'Squad night', 'members': ['Pixel_Rider', 'Racer_77', 'ExampleHost'],
         'last': 'Racer_77: I can host at 9'},
        {'id': 'c3', 'name': 'Pixel_Rider', 'members': ['Pixel_Rider'], 'last': 'sent you the clip'}]
    messages = [
        {'sender': me, 'body': f'{me} added NightOwl_42.', 'system': True},
        {'sender': 'NightOwl_42', 'body': 'that last round was unreal'},
        {'sender': me, 'body': 'right? the comeback in the final minute'},
        {'sender': 'NightOwl_42', 'body': 'gg, rematch tonight? I can start a party around 9 if everyone is free'},
        {'sender': me, 'body': 'count me in'}]
    friends = [
        {'account_id': '1', 'online_id': 'NightOwl_42', 'presence': 'online', 'status': 'Playing a racing game'},
        {'account_id': '2', 'online_id': 'Pixel_Rider', 'presence': 'online', 'status': 'Online'},
        {'account_id': '3', 'online_id': 'Racer_77', 'presence': 'offline', 'status': 'Last online 2 hours ago'},
        {'account_id': '4', 'online_id': 'ExampleHost', 'presence': 'offline', 'status': 'Offline'}]
    requests = [{'account_id': '5', 'online_id': 'Moonlit_Fox'}]
    results = [
        {'account_id': '6', 'online_id': 'Nova_Strike', 'relation': 'none'},
        {'account_id': '7', 'online_id': 'Nova_Strike_99', 'relation': 'requested'},
        {'account_id': '1', 'online_id': 'NightOwl_42', 'relation': 'friend'}]
    window.social_result('conversations', conversations)
    window.social_result('friends', (friends, requests))
    window.loaded = {'conversations', 'friends'}
    if page == 'messages':
        window.rail.select(MESSAGES, animate=False)
        window.chat_list.setCurrentRow(0)
        window.social_result('thread', ('c1', messages))
        window.chat_input.setText('see you at 9')
    elif page == 'settings':
        window.rail.select(SETTINGS, animate=False)
    elif page == 'host':
        # Synthetic hosted party: no PSN, no audio.
        window.rail.select(PARTIES, animate=False)
        window.busy, window.hosting, window.control = True, True, SessionControl()
        window.control.members = [me, 'NightOwl_42']
        window.invited = {'2'}
        window.roster = None
        window.show_party()
        window.update_buttons()
        QTimer.singleShot(400, lambda: window.set_status('Connected · voice is live', 'live'))
    else:
        window.rail.select(FRIENDS, animate=False)
        if page == 'add':
            window.friends_tab(1)
            window.search_input.setText('Nova_Strike')
            window.social_result('results', results)
    window.update_social()


LOG_PATH = Path(CONFIG_DIR) / 'voxchat.log'
_log_lock = threading.Lock()


def write_log_file(message):
    """Keep the connection log on disk too, so it can be attached to a bug report."""
    try:
        with _log_lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
                LOG_PATH.replace(LOG_PATH.with_suffix('.log.old'))
            with LOG_PATH.open('a', encoding='utf-8') as file:
                file.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n')
    except OSError:
        pass


class _FileLogHandler(logging.Handler):
    def emit(self, record):
        write_log_file(f'[{record.name}] {record.getMessage()}')


def setup_file_logging():
    """Network library warnings (and ICE progress) go to the log file, never the UI."""
    handler = _FileLogHandler(logging.INFO)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    for name in ('aioice', 'aiortc'):
        logging.getLogger(name).setLevel(logging.INFO)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--smoke-call', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--smoke-page', choices=('messages', 'friends', 'add', 'settings', 'host'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.smoke = args.smoke or bool(args.smoke_page)
    setup_file_logging()
    write_log_file(f'VoxChat started on {platform.platform()}, Python {platform.python_version()}')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('VoxChat')
    app.setDesktopFileName('voxchat')
    app.setWindowIcon(QIcon(str(ASSETS / 'voxchat.svg')))
    app.setFont(QFont('Noto Sans', 10))
    smooth = SmoothScroll(app)
    app.installEventFilter(smooth)
    window = PartyWindow()
    window.offline = args.smoke or args.smoke_call
    window.show()
    if args.smoke_page:
        smoke_social(window, args.smoke_page)
    if args.smoke or args.smoke_call:
        names = ['ExampleHost', window.me or "you", 'NightOwl_42', 'Pixel_Rider'] + (['Racer_77'] if args.smoke_call else [])
        window.events.put(('parties', [
            {'sessionId': 'demo', 'owner': {'onlineId': 'ExampleHost'}, 'members': [{'onlineId': n} for n in names]},
            {'sessionId': 'demo2', 'owner': {'onlineId': 'Racer_77'}, 'members': [{'onlineId': 'Racer_77'}]}]))
        if args.smoke_call:
            # Synthetic call state only: no PSN, no audio devices.
            def fake_call():
                window.poll()
                window.busy, window.control = True, SessionControl()
                window.control.members = names
                window.control.levels = {'ExampleHost': .09, 'NightOwl_42': .03}
                window.control.mic_level = .05
                window.control.states = {'ExampleHost': 'connected', 'NightOwl_42': 'connected',
                                         'Pixel_Rider': 'connecting', 'Racer_77': 'disconnected'}
                window.control.remote_muted = {'NightOwl_42'}
                window.toggle_silence('ExampleHost')
                window.connected_at = time.monotonic() - 83
                window.set_status('Connected · voice is live', 'live')
                window.update_buttons()
                window.ticker.start(33)
            QTimer.singleShot(200, fake_call)

        def screenshot():
            name = ('desktop-ui-call.png' if args.smoke_call else
                    f'desktop-ui-{args.smoke_page}.png' if args.smoke_page else 'desktop-ui.png')
            target = Path(__file__).resolve().parent.parent / 'screenshots' / name
            target.parent.mkdir(exist_ok=True)
            window.grab().save(str(target))
            print('UI screenshot:', target, flush=True)
            window.busy = False
            window.close()
        QTimer.singleShot(1200, screenshot)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
