from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner — codec, bitrate, the
# whole signed googlevideo URL — to the console for every stream it opens. That
# is two screenfuls per video, it looks like an error to anyone reading the log,
# and it puts a URL carrying the viewer's IP address into a terminal people
# paste into bug reports. Silenced here, before Qt initialises its logging.
# An explicit setting from the user is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping JARVIS from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

try:
    from core.avatar import HoloAvatar
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    HoloAvatar = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "MARK LVI"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 1280, 780
_MIN_W,     _MIN_H     = 1020, 680
_HEADER_H = 112
_LEFT_W  = 286
_RIGHT_W = 286

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#030914"
    PANEL     = "#071321"
    PANEL2    = "#0a1928"
    BORDER    = "#123253"
    BORDER_B  = "#1764a0"
    BORDER_A  = "#174775"
    PRI       = "#168cff"
    PRI_DIM   = "#0865bd"
    PRI_GHO   = "#061d3b"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#a8dcff"
    TEXT_DIM  = "#5683aa"
    TEXT_MED  = "#79b7e8"
    WHITE     = "#e5f5ff"
    DARK      = "#050e1a"
    BAR_BG    = "#0a1b2b"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _windows_wifi_status() -> tuple[str, int | None] | None:
    """Read the connected WLAN adapter's real signal quality through WLAN API."""
    if _OS != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
            ]

        class _InterfaceInfo(ctypes.Structure):
            _fields_ = [
                ("InterfaceGuid", _Guid),
                ("strInterfaceDescription", wintypes.WCHAR * 256),
                ("isState", wintypes.DWORD),
            ]

        class _InterfaceList(ctypes.Structure):
            _fields_ = [
                ("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _InterfaceInfo * 1),
            ]

        class _Dot11Ssid(ctypes.Structure):
            _fields_ = [
                ("uSSIDLength", wintypes.ULONG),
                ("ucSSID", ctypes.c_ubyte * 32),
            ]

        class _AssociationAttributes(ctypes.Structure):
            _fields_ = [
                ("dot11Ssid", _Dot11Ssid),
                ("dot11BssType", wintypes.DWORD),
                ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", wintypes.DWORD),
                ("uDot11PhyIndex", wintypes.ULONG),
                ("wlanSignalQuality", wintypes.ULONG),
                ("ulRxRate", wintypes.ULONG),
                ("ulTxRate", wintypes.ULONG),
            ]

        class _SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("bSecurityEnabled", wintypes.BOOL),
                ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", wintypes.DWORD),
                ("dot11CipherAlgorithm", wintypes.DWORD),
            ]

        class _ConnectionAttributes(ctypes.Structure):
            _fields_ = [
                ("isState", wintypes.DWORD),
                ("wlanConnectionMode", wintypes.DWORD),
                ("strProfileName", wintypes.WCHAR * 256),
                ("wlanAssociationAttributes", _AssociationAttributes),
                ("wlanSecurityAttributes", _SecurityAttributes),
            ]

        api = ctypes.WinDLL("wlanapi.dll")
        handle = wintypes.HANDLE()
        negotiated = wintypes.DWORD()
        api.WlanOpenHandle.argtypes = [
            wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.HANDLE),
        ]
        api.WlanOpenHandle.restype = wintypes.DWORD
        result = api.WlanOpenHandle(2, None, ctypes.byref(negotiated),
                                    ctypes.byref(handle))
        if result != 0:
            return None

        api.WlanEnumInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_InterfaceList)),
        ]
        api.WlanEnumInterfaces.restype = wintypes.DWORD
        api.WlanQueryInterface.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Guid), wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        ]
        api.WlanQueryInterface.restype = wintypes.DWORD
        api.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        api.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        api.WlanCloseHandle.restype = wintypes.DWORD

        interfaces = ctypes.POINTER(_InterfaceList)()
        qualities: list[int] = []
        has_wlan_interface = False
        try:
            result = api.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces))
            if result != 0 or not interfaces:
                return None
            info = interfaces.contents
            has_wlan_interface = info.dwNumberOfItems > 0
            for index in range(info.dwNumberOfItems):
                interface = info.InterfaceInfo[index]
                if interface.isState != 1:
                    continue
                data_size = wintypes.DWORD()
                data = ctypes.c_void_p()
                opcode_type = wintypes.DWORD()
                result = api.WlanQueryInterface(
                    handle, ctypes.byref(interface.InterfaceGuid), 7, None,
                    ctypes.byref(data_size), ctypes.byref(data),
                    ctypes.byref(opcode_type),
                )
                if result != 0 or not data:
                    continue
                try:
                    connection = ctypes.cast(
                        data, ctypes.POINTER(_ConnectionAttributes)
                    ).contents
                    if connection.isState == 1:
                        qualities.append(int(
                            connection.wlanAssociationAttributes.wlanSignalQuality
                        ))
                finally:
                    api.WlanFreeMemory(data)
        finally:
            if interfaces:
                api.WlanFreeMemory(interfaces)
            api.WlanCloseHandle(handle, None)

        if qualities:
            return "CONNECTED", max(0, min(100, max(qualities)))
        if has_wlan_interface:
            return "OFFLINE", None
        return "N/A", None
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        return None


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self.disk = -1.0
        self.mem_used_gb = 0.0
        self.mem_total_gb = 0.0
        self.disk_free_gb = -1.0
        self.disk_total_gb = -1.0
        self.wifi = "N/A"
        self.wifi_quality = -1
        self.battery = "N/A"
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        mem = memory.percent
        mem_used_gb = memory.used / (1024 ** 3)
        mem_total_gb = memory.total / (1024 ** 3)
        try:
            disk_usage = psutil.disk_usage(Path.home().anchor or os.path.sep)
            disk = disk_usage.percent
            disk_free_gb = disk_usage.free / (1024 ** 3)
            disk_total_gb = disk_usage.total / (1024 ** 3)
        except (OSError, PermissionError):
            disk = -1.0
            disk_free_gb = -1.0
            disk_total_gb = -1.0

        wifi_info = _windows_wifi_status()
        if wifi_info is None:
            wifi = "N/A"
            wifi_quality = -1
            try:
                wireless = [
                    stats.isup for name, stats in psutil.net_if_stats().items()
                    if any(token in name.lower() for token in
                           ("wi-fi", "wifi", "wlan", "wireless", "wlp"))
                ]
                if wireless:
                    wifi = "CONNECTED" if any(wireless) else "OFFLINE"
            except (OSError, AttributeError, NotImplementedError):
                pass
        else:
            wifi, quality = wifi_info
            wifi_quality = quality if quality is not None else -1

        battery = "N/A"
        try:
            state = psutil.sensors_battery()
            if state is not None:
                battery = f"{state.percent:.0f}%  " + (
                    "CHARGING" if state.power_plugged else "ON BATTERY"
                )
        except (OSError, AttributeError, NotImplementedError):
            pass

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp
            self.disk = disk
            self.mem_used_gb = mem_used_gb
            self.mem_total_gb = mem_total_gb
            self.disk_free_gb = disk_free_gb
            self.disk_total_gb = disk_total_gb
            self.wifi = wifi
            self.wifi_quality = wifi_quality
            self.battery = battery

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
                "disk": self.disk,
                "mem_used_gb": self.mem_used_gb,
                "mem_total_gb": self.mem_total_gb,
                "disk_free_gb": self.disk_free_gb,
                "disk_total_gb": self.disk_total_gb,
                "wifi": self.wifi,
                "wifi_quality": self.wifi_quality,
                "battery": self.battery,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # The holographic head that fills the HUD. If it could not be imported
        # we fall back to the old glowing core so the panel is never empty.
        self._avatar = None
        if HoloAvatar is not None:
            try:
                self._avatar = HoloAvatar()
            except Exception:
                self._avatar = None

        # Which centrepiece to draw. Read once here and changed live by the
        # settings toggle; the avatar object is kept either way so switching
        # back is instant and costs no reload.
        try:
            from memory.config_manager import get_hud_style
            self.hud_style = get_hud_style()
        except Exception:
            self.hud_style = "face"
        self._core_phase = 0.0
        self._energy_phase = 0.0

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0

        # Rescaled-face cache: the smooth rescale is expensive, so we keep the
        # last result and only rebuild it when the (quantised) size changes.

        # Static grid-dot layer, pre-rendered once per size/theme into a pixmap
        # so paintEvent blits it in one call instead of thousands of drawPoint()s.
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        self._wave_amp = 0.0
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None
        self._vis_i = None        # first schedule frame not yet handed to the mouth
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of (level, openness, width) frames.

        The playback thread writes up to 200 ms of audio in one go, so a single
        averaged level would only move the mouth five times a second — enough to
        flap, nowhere near enough to articulate. It instead posts the whole
        slice's worth of 20 ms frames here and `_step()` plays them out against
        the wall clock, in step with the audio going to the speakers.

        `at` is the wall-clock time this batch will *begin to sound*, which the
        caller tracks as a playback cursor. It is not the time of the call, and
        the difference is the whole point: `stream.write` returns once the buffer
        accepts the samples, so consecutive batches are handed over far faster
        than they play. Anchoring each one to "now" made every batch start while
        its predecessor was still sounding, so each schedule replaced the last
        after a couple of frames and the mouth only ever played the opening
        instant of every 200 ms — the reason it did not match the words.

        Successive batches are therefore *appended* into one continuous
        timeline, not swapped in. A paragraph is one schedule; the mouth stops
        falling into a gap at every chunk boundary and having to climb back out.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    # Where in the existing timeline does this batch land?
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        # Continues (or slightly overlaps) what is already
                        # queued: extend rather than restart. Drop whatever has
                        # already been played so the list cannot grow without
                        # bound over a long reply.
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _make_grid(self, W: int, H: int) -> QPixmap:
        """Pre-render the static grid-dot background into a transparent pixmap so
        paintEvent can blit it once per frame instead of running a nested
        drawPoint() loop across the whole widget every 16 ms."""
        pm = QPixmap(max(1, W), max(1, H))
        pm.fill(Qt.GlobalColor.transparent)
        gp = QPainter(pm)
        gp.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                gp.drawPoint(x, y)
        gp.end()
        return pm

    def _paint_energy_frame(self, p: QPainter, W: int, H: int) -> None:
        """Low-key animated energy rails frame the avatar without crossing it."""
        phase = self._energy_phase
        pulse = 0.5 + 0.5 * math.sin(phase * 1.7)
        side = min(W * 0.22, 150.0)
        top = 24.0
        bottom = H * 0.76

        p.save()
        p.setPen(Qt.PenStyle.NoPen)

        # Soft blue/red light washes stay at the HUD's outer edges.
        for x, color, direction in (
            (0.0, C.PRI, 1.0),
            (W, C.RED, -1.0),
        ):
            glow = QLinearGradient(x, 0, x + direction * side, 0)
            glow.setColorAt(0.0, qcol(color, int(18 + pulse * 12)))
            glow.setColorAt(1.0, qcol(color, 0))
            p.fillRect(QRectF(min(x, x + direction * side), top,
                              side, bottom - top), QBrush(glow))

        # Broken corner rails, like a restrained targeting frame.
        rail_alpha = int(90 + pulse * 70)
        p.setPen(QPen(qcol(C.PRI, rail_alpha), 1.2))
        p.drawLine(QPointF(18, top), QPointF(70, top))
        p.drawLine(QPointF(18, top), QPointF(18, top + 44))
        p.drawLine(QPointF(W - 18, H - top), QPointF(W - 70, H - top))
        p.drawLine(QPointF(W - 18, H - top), QPointF(W - 18, H - top - 44))

        p.setPen(QPen(qcol(C.RED, rail_alpha), 1.2))
        p.drawLine(QPointF(W - 18, top), QPointF(W - 70, top))
        p.drawLine(QPointF(W - 18, top), QPointF(W - 18, top + 44))
        p.drawLine(QPointF(18, H - top), QPointF(70, H - top))
        p.drawLine(QPointF(18, H - top), QPointF(18, H - top - 44))

        # Short travelling energy streaks along the side rails.
        travel = (phase * 44.0) % max(1.0, bottom - top)
        for x, color, lean in ((22.0, C.PRI, 1.0), (W - 22.0, C.RED, -1.0)):
            y = top + travel
            beam = QLinearGradient(x, y - 20, x, y + 20)
            beam.setColorAt(0.0, qcol(color, 0))
            beam.setColorAt(0.5, qcol(color, int(150 + pulse * 70)))
            beam.setColorAt(1.0, qcol(color, 0))
            p.setPen(QPen(QBrush(beam), 2.0))
            p.drawLine(QPointF(x, y - 18), QPointF(x + lean * 8, y + 18))

        # Tiny circuit-like lightning forks; they remain outside the face area.
        fork_y = top + 70 + 18 * math.sin(phase * 0.8)
        for x, color, direction in ((30.0, C.PRI, 1.0), (W - 30.0, C.RED, -1.0)):
            path = QPainterPath(QPointF(x, fork_y))
            path.lineTo(x + direction * 8, fork_y + 8)
            path.lineTo(x + direction * 1, fork_y + 15)
            path.lineTo(x + direction * 11, fork_y + 23)
            p.setPen(QPen(qcol(color, int(85 + pulse * 120)), 1.3))
            p.drawPath(path)

        p.restore()

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # A viseme schedule, if one is playing, gives both the level and the
        # mouth shape for this exact instant; otherwise fall back to the peak
        # level the audio threads pushed in.
        v_open = v_wide = v_level = None
        v_seq = None
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                # Hand over *every* frame since the last tick, not just the one
                # under the cursor. This timer runs at 60 Hz but the paint is
                # throttled and the machine may be busy, so a tick can span two
                # or three 20 ms frames — and a consonant closure is only two
                # frames long. Sampling one and discarding the rest is how the
                # closures between words went missing.
                j = self._vis_i if self._vis_i is not None else i
                v_seq = frames[max(0, j):i + 1]
                self._vis_i = max(j, i + 1)
                v_level, v_open, v_wide = frames[i]
                if v_seq:
                    peak = max(f[0] for f in v_seq)
                    if peak > self._live_amp:
                        self._live_amp = peak
            elif i >= len(frames):
                self._visemes = None        # schedule spent
                self._vis_i = None

        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.90
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        wave_target = min(1.0, math.sqrt(self._live_amp) * 1.15)
        wave_response = 0.62 if wave_target > self._wave_amp else 0.20
        self._wave_amp += (wave_target - self._wave_amp) * wave_response
        amp = self._amp_disp

        # The avatar and waveform share the same audio source; the waveform
        # has its own response curve so quiet speech remains visible.
        dt = now - self._step_t
        self._step_t = now
        # Integrated, not derived from absolute time: multiplying wall-clock by
        # a rate that changes with state jumps the rings the instant JARVIS
        # starts talking. Same lesson the head's sway taught.
        self._core_phase += min(0.10, max(0.0, dt))

        if self._avatar is not None and self.hud_style == "face":
            self._avatar.step(dt, amp, speaking=self.speaking,
                              muted=self.muted, state=self.state,
                              v_open=v_open, v_wide=v_wide or 0.0,
                              v_level=v_level, v_seq=v_seq,
                              v_hop=(sched[2] if sched is not None else 0.02))
        else:
            # Fallback core: slow "breathing" base target, lifted by the level.
            if now - self._last_t > (0.12 if self.speaking else 0.5):
                if self.speaking:
                    self._base_scale = 1.03
                    self._base_halo  = 122.0
                elif self.muted:
                    self._base_scale = random.uniform(0.998, 1.002)
                    self._base_halo  = random.uniform(15, 28)
                else:
                    self._base_scale = random.uniform(1.001, 1.008)
                    self._base_halo  = random.uniform(48, 68)
                self._last_t = now

            if self.muted:
                self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
            elif self.speaking:
                self._tgt_scale = self._base_scale + amp * 0.13
                self._tgt_halo  = self._base_halo  + amp * 95.0
            else:
                self._tgt_scale = self._base_scale + amp * 0.06
                self._tgt_halo  = self._base_halo  + amp * 75.0

            sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
            self._scale += (self._tgt_scale - self._scale) * sp
            self._halo  += (self._tgt_halo  - self._halo)  * sp

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint throttling — advancing the animation state above is cheap at
        # 60 Hz, but the paint is heavy. Active (speaking, audio, thinking) runs
        # at ~30 Hz, which is the frame rate animation has used for talking
        # characters forever and is indistinguishable here; idle drops to ~20 Hz
        # so a sleeping HUD stops pinning a CPU core. The visuals stay smooth
        # either way because the animation state keeps stepping at 60 Hz.
        self._paint_tick = (self._paint_tick + 1) % 6
        active = (self.speaking or amp > 0.02
                  or self.state in ("THINKING", "PROCESSING"))
        if _blinked or (self._paint_tick % 2 == 0 if active
                        else self._paint_tick % 3 == 0):
            # Nothing is on screen when the window is hidden or minimised, so
            # rendering the avatar into it is pure waste — and this app is meant
            # to sit running all day. The animation state above keeps stepping,
            # so it picks up mid-motion instead of snapping when you come back.
            if self._on_screen():
                self.update()

    def _on_screen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    # ── reactor core ─────────────────────────────────────────────────────────
    # The centrepiece for anyone who did not want a face looking back at them.
    # Built from the same budget as the head — software QPainter, no GPU — and
    # from the same principle: everything on it means something. The rings turn
    # at a rate the state sets, the spectrum ring is the real audio level, and
    # the core brightens with the voice. Nothing here is decoration that moves
    # for its own sake, which is what made the old glowing orb feel dead.

    def _core_colours(self):
        if self.muted:
            return qcol(C.MUTED_C), qcol(C.MUTED_C)
        if self.speaking:
            return qcol(C.PRI), qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return qcol(C.PRI), qcol(C.ACC2)
        if self.state == "LISTENING":
            return qcol(C.PRI), qcol(C.GREEN)
        return qcol(C.PRI), qcol(C.PRI_DIM)

    def _paint_core(self, p: QPainter, cx: float, cy: float, r: float,
                    W: float = 0.0, H: float = 0.0):
        """Draw the reactor at (cx, cy) with outer radius r, using the whole
        canvas (W x H) for the marks that frame it."""
        main, acc = self._core_colours()
        bg = qcol(C.BG)
        amp = self._amp_disp
        t = self._core_phase
        live = (self.speaking or amp > 0.04) and not self.muted

        def blend(col: QColor, a: float) -> QColor:
            """Pre-mix onto the background instead of asking Qt to composite.
            The raster engine's opaque path is several times faster than its
            translucent one, and everything here is a line or an arc."""
            k = max(0.0, min(1.0, a))
            return QColor(int(bg.red()   + (col.red()   - bg.red())   * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue()  + (col.blue()  - bg.blue())  * k))

        p.setBrush(Qt.BrushStyle.NoBrush)

        # 1. The atmosphere. One radial gradient doing what a stack of discs did
        #    badly: a wide, soft body of light that gives the thing presence
        #    before any detail is read. This single element decides whether the
        #    HUD looks vast or looks small, so it is drawn first and drawn big.
        # Concentrated rather than spread: a gradient reaching the outer rim
        # washes the whole disc a flat dim blue and reads as fog. Ending it at
        # two thirds leaves it a body of light with somewhere to fall off to,
        # which is what makes it look lit rather than tinted.
        lift = 1.0 + 0.55 * amp + (0.18 if self.speaking else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0, a1 in ((r * 0.70, 0.30, 0.0), (r * 0.34, 0.34, 0.0)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, blend(main, min(0.95, a0 * lift)))
            g.setColorAt(0.45, blend(main, min(0.95, a0 * lift * 0.52)))
            g.setColorAt(0.78, blend(main, min(0.95, a0 * lift * 0.18)))
            g.setColorAt(1.00, blend(main, a1))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Frame marks at the corners of the whole canvas, not of the circle.
        #    They are what set the scale: the eye reads the reactor as filling
        #    the room rather than sitting in the middle of it.
        if W > 40 and H > 40:
            m, arm = min(W, H) * 0.035, min(W, H) * 0.055
            p.setPen(QPen(blend(main, 0.45), 1.4))
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                x = cx + sx * (W / 2 - m)
                y = cy + sy * (H / 2 - m)
                p.drawLine(QLineF(x, y, x - sx * arm, y))
                p.drawLine(QLineF(x, y, x, y - sy * arm))

        # 3. Crosshair across the full canvas, broken around the core so it
        #    frames the reactor rather than crossing it.
        p.setPen(QPen(blend(main, 0.16), 1))
        gap = r * 0.62
        if W > 40:
            p.drawLine(QLineF(cx - W / 2, cy, cx - gap, cy))
            p.drawLine(QLineF(cx + gap, cy, cx + W / 2, cy))
        if H > 40:
            p.drawLine(QLineF(cx, cy - H / 2, cx, cy - gap))
            p.drawLine(QLineF(cx, cy + gap, cx, cy + H / 2))

        # 4. Two thin outer circles. Sparse on purpose — a dense ring reads as a
        #    grey band at this size, and restraint is what made the original
        #    look expensive.
        for rr, a in ((1.00, 0.34), (0.93, 0.16)):
            rad = r * rr
            p.setPen(QPen(blend(main, a), 1))
            p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))

        # 5. Long, sparse graduations: 24 majors reaching well in from the rim,
        #    with shorter minors between them.
        major, minor = [], []
        for i in range(72):
            a = math.radians(i * 5.0)
            ca, sa = math.cos(a), math.sin(a)
            if i % 3 == 0:
                major.append(QLineF(cx + ca * r * 0.885, cy + sa * r * 0.885,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
            else:
                minor.append(QLineF(cx + ca * r * 0.945, cy + sa * r * 0.945,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
        p.setPen(QPen(blend(main, 0.42), 1.3))
        p.drawLines(major)
        p.setPen(QPen(blend(main, 0.18), 1))
        p.drawLines(minor)

        # 6. Sweeping arcs. Long spans, not dashes — the original's grandeur
        #    came from a few big strokes. Speed is the state: idle drifts,
        #    thinking hurries, speaking runs.
        rate = 1.0 + (1.9 if self.state in ("THINKING", "PROCESSING") else 0.0) \
                   + (1.2 if self.speaking else 0.0)
        for k, (rr, span, count, dirn, col, a, wid) in enumerate((
                (0.955, 118, 2, +1, acc,  0.75, 2.0),
                (0.845, 82,  3, -1, main, 0.38, 1.3),
                (0.760, 150, 1, +1, acc,  0.45, 1.6),
                (0.660, 64,  4, -1, main, 0.26, 1.1),
                (0.545, 128, 2, +1, main, 0.30, 1.2))):
            rad = r * rr
            p.setPen(QPen(blend(col, a), wid))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (t * rate * (9 + k * 6) * dirn) % 360.0
            for sgm in range(count):
                p.drawArc(box, int((base + sgm * (360.0 / count)) * 16),
                          int(span * 16))

        # 7. The voice, as a ring of graduations that grow with it. Kept out at
        #    a wide radius so it never crowds the middle.
        n = 60
        ring = r * 0.415
        spikes = []
        for i in range(n):
            a = math.radians(i * (360.0 / n))
            ca, sa = math.cos(a), math.sin(a)
            wob = 0.5 + 0.5 * math.sin(t * 2.3 + i * 0.42)
            idle = 0.018 + 0.012 * math.sin(t * 1.2 + i * 0.7)
            h = r * (idle + (amp * 0.20 * wob if live else 0.0))
            spikes.append(QLineF(cx + ca * ring, cy + sa * ring,
                                 cx + ca * (ring + h), cy + sa * (ring + h)))
        p.setPen(QPen(blend(acc if live else main, 0.25 + 0.5 * amp), 1.6))
        p.drawLines(spikes)

        # 8. The inner ring the name sits in.
        inner = r * 0.355
        p.setPen(QPen(blend(acc, 0.30 + 0.45 * amp), 1.5))
        p.drawEllipse(QRectF(cx - inner, cy - inner, inner * 2, inner * 2))

        # 9. The name, sized from the string rather than from the radius alone:
        #    "J.A.R.V.I.S" and a name someone renamed to "MAX" are very
        #    different widths, and a fixed fraction of r spills one of them past
        #    the ring it is supposed to sit inside.
        name = self._assistant_name or ""
        if name:
            space = max(1.0, r * 0.018)
            fsz = max(8, int(min(r * 0.105,
                                 (inner * 1.75) / max(1, len(name)) * 1.6 - space)))
            f = QFont("Courier New", fsz, QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, space)
            p.setFont(f)
            p.setPen(QPen(blend(qcol(C.WHITE), 0.6 + 0.4 * min(1.0, amp * 2)), 1))
            p.drawText(QRectF(cx - r, cy - fsz, r * 2, fsz * 2),
                       Qt.AlignmentFlag.AlignCenter, name)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots — blitted from a cached layer; rebuilt only when the size
        # or the theme's ghost colour changes (so live re-theming still works).
        _gkey = (W, H, C.PRI_GHO)
        if self._grid_cache is None or self._grid_key != _gkey:
            self._grid_cache = self._make_grid(W, H)
            self._grid_key   = _gkey
        p.drawPixmap(0, 0, self._grid_cache)
        self._paint_energy_frame(p, W, H)
        self._energy_phase = (self._energy_phase + 0.016) % 1000.0

        # ── holographic head ────────────────────────────────────────────────
        # Sized to the band between the top of the canvas and the status line,
        # capped by width, so it fills the HUD at any window size — including
        # fullscreen — without ever colliding with the status text below.
        _sy_status = min(cy + fw * 0.30, H - 100)
        if self._avatar is not None and self.hud_style == "face":
            _band_t = 12.0
            _band_h = max(60.0, _sy_status - 12.0 - _band_t)
            _r_head = min(fw * 0.335, _band_h / (self._avatar.SPAN + 0.08))
            _head_cy = _band_t + (_band_h - self._avatar.SPAN * _r_head) / 2.0 + _r_head

            if self.muted:
                _main = _acc = qcol(C.MUTED_C)
            else:
                _main = qcol(C.PRI)
                if self.speaking:
                    _acc = qcol(C.ACC)
                elif self.state in ("THINKING", "PROCESSING"):
                    _acc = qcol(C.ACC2)
                elif self.state == "LISTENING":
                    _acc = qcol(C.GREEN)
                else:
                    _acc = qcol(C.PRI)
            self._avatar.paint(p, cx, _head_cy, _r_head, _main, _acc, qcol(C.BG))

        # reactor core — the other centrepiece, and the fallback if the head
        # could not be built. There is no third path: the old face.png branch
        # was unreachable (no such file ships) and the bare orb it fell through
        # to is what this replaces.
        else:
            _band_t = 12.0
            _band_h = max(60.0, _sy_status - 12.0 - _band_t)
            _r = min(W * 0.43, max(24.0, (_band_h - 28.0) / 2.0))
            self._paint_core(p, cx, _band_t + _band_h / 2.0, _r, W, _band_h)

        # status text
        sy = _sy_status
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 24), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform — reacts to the real audio level (mic while listening,
        # JARVIS's own voice while speaking). Falls back to a gentle idle
        # ripple when there's no sound. _amp_disp is the smoothed 0–1 level.
        baseline = min(H - 30, sy + 62)
        count = 49
        pitch = min(10.0, max(5.5, (W - 64) / count))
        bar_w = max(3.0, pitch * 0.55)
        wx0 = (W - count * pitch) / 2
        amp = self._wave_amp
        mid = (count - 1) / 2.0

        p.setPen(QPen(qcol(C.BORDER_B, 100), 1))
        p.drawLine(QPointF(wx0 - 8, baseline), QPointF(wx0 + count * pitch + 8, baseline))
        for i in range(count):
            if self.muted:
                hgt = 2.0 + 1.2 * math.sin(i * 0.52 + self._tick * 0.08)
                color = C.MUTED_C
            else:
                env = (1.0 - abs(i - mid) / mid) ** 0.48
                wave_a = abs(math.sin(self._tick * 0.16 + i * 0.39))
                wave_b = abs(math.sin(self._tick * 0.09 - i * 0.22))
                idle = 2.0 + 3.5 * (0.58 * wave_a + 0.42 * wave_b)
                hgt = min(46.0, idle + amp * 54.0 * env)
                color = C.PRI

            bar = QRectF(wx0 + i * pitch, baseline - hgt / 2, bar_w, hgt)
            grad = QLinearGradient(bar.left(), bar.top(), bar.right(), bar.bottom())
            grad.setColorAt(0.0, qcol(color, 145))
            grad.setColorAt(0.5, qcol(
                "#b8f7ff" if amp > 0.035 and not self.muted else color,
                245 if amp > 0.035 else 170,
            ))
            grad.setColorAt(1.0, qcol("#28baff" if not self.muted else color, 215))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(bar, bar_w / 2, bar_w / 2)

        p.end()   # end deterministically so the backing store never flushes an active painter

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self._phase = 0.0
        self.setFixedHeight(48)
        self.setMinimumWidth(80)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._anim = QTimer(self)
        self._anim.setInterval(70)
        self._anim.timeout.connect(self._animate)
        self._anim.start()

    def _animate(self):
        self._phase = (self._phase + 0.018) % 1.0
        self.update()

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        card = QRectF(1, 1, W - 2, H - 2)
        bg = QLinearGradient(0, 0, W, H)
        bg.setColorAt(0.0, qcol("#071a27"))
        bg.setColorAt(0.55, qcol(C.PANEL2))
        bg.setColorAt(1.0, qcol("#100d1b"))
        p.setBrush(QBrush(bg))
        p.setPen(QPen(qcol(C.BORDER_B, 160), 1))
        p.drawRoundedRect(card, 7, 7)

        # Two-tone illuminated edge and clipped travelling scan ray.
        p.save()
        p.setClipRect(card.adjusted(1, 1, -1, -1))
        p.setPen(QPen(qcol(C.PRI, 170), 1.4))
        p.drawLine(QPointF(8, 2), QPointF(W * 0.43, 2))
        p.setPen(QPen(qcol(C.RED, 150), 1.4))
        p.drawLine(QPointF(W * 0.68, H - 2), QPointF(W - 8, H - 2))
        scan_x = -36 + self._phase * (W + 72)
        scan = QLinearGradient(scan_x - 24, 0, scan_x + 24, 0)
        scan.setColorAt(0.0, qcol(C.PRI, 0))
        scan.setColorAt(0.5, qcol(C.PRI, 24))
        scan.setColorAt(1.0, qcol(C.RED, 0))
        p.fillRect(card, QBrush(scan))
        p.restore()

        p.setPen(QPen(qcol(self._color, 180), 2))
        p.drawLine(QPointF(9, 7), QPointF(27, 7))
        p.setPen(QPen(qcol(C.RED, 130), 1.5))
        p.drawLine(QPointF(W - 27, H - 7), QPointF(W - 9, H - 7))

        bar_x, bar_y = 10, H - 13
        bar_w, bar_h = W - 20, 5
        fill_w = int(bar_w * self._value / 100)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2.5, 2.5)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            grad = QLinearGradient(bar_x, bar_y, bar_x + fill_w, bar_y)
            grad.setColorAt(0, qcol(self._color, 150))
            grad.setColorAt(1, bar_col)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2.5, 2.5)
            p.setPen(QPen(qcol(bar_col.name(), int(50 + 50 * (
                0.5 + 0.5 * math.sin(self._phase * math.tau)))), 1))
            p.drawLine(QPointF(bar_x, bar_y - 1), QPointF(bar_x + fill_w, bar_y - 1))

        # Fine tick marks make the load read as instrumentation rather than a
        # generic progress bar.
        p.setPen(QPen(qcol(C.BG), 1))
        for i in range(1, 20):
            x = bar_x + bar_w * i / 20
            p.drawLine(QPointF(x, bar_y), QPointF(x, bar_y + bar_h))

        label_font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        p.setFont(label_font)
        p.setPen(QPen(qcol(C.TEXT_MED), 1))
        p.drawText(QRectF(10, 7, max(50, W - 82), 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)

        p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        text_color = (C.TEXT_DIM if self._text in ("--", "N/A")
                      else bar_col.name())
        p.setPen(QPen(qcol(text_color), 1))
        p.drawText(QRectF(W - 82, 5, 70, 19),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self._text)

        p.end()


class RingGauge(QWidget):
    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._display_value = 0.0
        self._text = "N/A"
        self._phase = 0.0
        self.setFixedSize(82, 90)
        self._anim = QTimer(self)
        self._anim.setInterval(75)
        self._anim.timeout.connect(self._animate)
        self._anim.start()

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text = text

    def _animate(self):
        self._phase = (self._phase + 0.025) % 1.0
        self._display_value += (self._value - self._display_value) * 0.22
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, 37
        rect = QRectF(cx - 28, cy - 28, 56, 56)
        available = self._text not in ("N/A", "--")
        glow = 0.5 + 0.5 * math.sin(self._phase * math.tau)

        p.setPen(QPen(qcol("#102635"), 5.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 225 * 16, 270 * 16)
        scan = int(self._phase * 360 * 16)
        p.setPen(QPen(qcol(self._color, 100), 1.4,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, scan, 42 * 16)
        if available and self._display_value > 0.2:
            p.setPen(QPen(qcol(self._color, int(160 + glow * 95)), 5.0,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, 225 * 16,
                      int(270 * 16 * self._display_value / 100))

        p.setPen(QPen(qcol(self._color, 85), 1))
        for i in range(11):
            angle = math.radians(225 + 27 * i)
            inner = 32
            outer = 35 if i % 5 == 0 else 33.5
            p.drawLine(
                QPointF(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                QPointF(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer),
            )

        p.setPen(QPen(qcol(self._color, int(45 + glow * 35)), 1))
        p.setBrush(QBrush(qcol("#07131e")))
        p.drawEllipse(QRectF(cx - 21, cy - 21, 42, 42))

        value_size = 10 if len(self._text) <= 4 else 8
        p.setFont(QFont("Consolas", value_size, QFont.Weight.Bold))
        value_color = self._color if available else C.TEXT_DIM
        p.setPen(QPen(qcol(value_color), 1))
        p.drawText(QRectF(cx - 23, cy - 9, 46, 18),
                   Qt.AlignmentFlag.AlignCenter, self._text)

        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_MED), 1))
        p.drawText(QRectF(0, 76, self.width(), 12),
                   Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


class WifiSignalIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._quality = -1
        self._connected = False
        self.setFixedSize(52, 38)

    def set_signal(self, quality: int, connected: bool):
        quality = max(-1, min(100, int(quality)))
        if quality == self._quality and connected == self._connected:
            return
        self._quality = quality
        self._connected = connected
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._connected and self._quality >= 0:
            bars = min(5, int(math.ceil(self._quality / 20)))
            color = (C.RED if self._quality < 20 else
                     C.ACC if self._quality < 45 else C.PRI)
        elif self._connected:
            bars = 0
            color = C.TEXT_DIM
        else:
            bars = 0
            color = C.TEXT_DIM

        base_y = self.height() - 10
        for index in range(5):
            height = 4 + index * min(4, self.height() / 9)
            rect = QRectF(4 + index * 8, base_y - height, 5, height)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(color if index < bars else C.BORDER, 230 if index < bars else 150)))
            p.drawRoundedRect(rect, 1.5, 1.5)

        p.setFont(QFont("Consolas", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(color), 1))
        value = f"{self._quality}%" if self._quality >= 0 else (
            "LINK" if self._connected else "OFF"
        )
        p.drawText(QRectF(0, base_y, self.width(), 10),
                   Qt.AlignmentFlag.AlignCenter, value)
        p.end()


class NetActivityGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._samples: list[float] = []
        self.setFixedHeight(66)
        self.setMinimumWidth(120)

    def set_rate(self, mb_per_sec: float):
        self._samples.append(max(0.0, float(mb_per_sec)))
        self._samples = self._samples[-36:]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        rect = QRectF(1, 1, w - 2, h - 2)
        p.setBrush(QBrush(qcol("#071624")))
        p.setPen(QPen(qcol(C.BORDER), 1))
        p.drawRoundedRect(rect, 7, 7)

        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_MED), 1))
        p.drawText(QRectF(10, 6, w - 20, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "NETWORK  /  LIVE THROUGHPUT")

        chart = QRectF(10, 23, w - 20, h - 31)
        p.setPen(QPen(qcol(C.BORDER, 120), 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(chart.left(), chart.bottom()),
                   QPointF(chart.right(), chart.bottom()))
        if not self._samples:
            p.end()
            return

        peak = max(0.25, max(self._samples))
        points = []
        for index, value in enumerate(self._samples):
            x = chart.left() + chart.width() * index / max(1, len(self._samples) - 1)
            y = chart.bottom() - chart.height() * min(1.0, value / peak)
            points.append(QPointF(x, y))

        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        area = QPainterPath(line)
        area.lineTo(chart.bottomRight())
        area.lineTo(chart.bottomLeft())
        area.closeSubpath()
        fill = QLinearGradient(0, chart.top(), 0, chart.bottom())
        fill.setColorAt(0, qcol(C.PRI, 80))
        fill.setColorAt(1, qcol(C.PRI, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawPath(area)
        p.setPen(QPen(qcol(C.PRI, 225), 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(line)
        if points:
            p.setPen(QPen(qcol(C.WHITE), 1))
            p.setBrush(QBrush(qcol(C.PRI)))
            p.drawEllipse(points[-1], 2.5, 2.5)
        p.end()


class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound — keeps memory flat and every insert cheap. Oldest
        # lines drop off the top automatically.
        self.document().setMaximumBlockCount(600)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                # SYS lines are the bulk of the log. Amber fought the cyan HUD
                # and, being a fixed status colour rather than a hue-linked one,
                # stayed amber even after the accent picker retinted everything
                # else. TEXT_MED follows the theme and drops the contrast to a
                # level you can read past.
                "sys":  qcol(C.TEXT_MED),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)
    file_cleared = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(76)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        if self._current_file is not None:
            self._current_file = None
            self._canvas.update()
            self.file_cleared.emit()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg = QLinearGradient(rect.topLeft(), rect.bottomRight())
        if z._current_file:
            bg.setColorAt(0, qcol("#08251f"))
            bg.setColorAt(1, qcol("#07141c"))
        elif z._drag_over:
            bg.setColorAt(0, qcol("#0a3546"))
            bg.setColorAt(1, qcol("#17152a"))
        elif z._hovering:
            bg.setColorAt(0, qcol("#0b2736"))
            bg.setColorAt(1, qcol("#101322"))
        else:
            bg.setColorAt(0, qcol("#091a27"))
            bg.setColorAt(1, qcol("#0a101b"))
        p.setBrush(QBrush(bg)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen_style = (Qt.PenStyle.DashLine if z._drag_over or z._hovering
                     else Qt.PenStyle.SolidLine)
        pen = QPen(border_col, 1.5, pen_style)
        if pen_style == Qt.PenStyle.DashLine:
            pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        # Luminous corner traces keep the drop target legible without a
        # permanently dashed outline.
        if not z._drag_over:
            p.setPen(QPen(qcol(C.PRI, 180), 2))
            p.drawLine(QPointF(rect.left() + 8, rect.top() + 1),
                       QPointF(rect.left() + 30, rect.top() + 1))
            p.setPen(QPen(qcol(C.RED, 150), 2))
            p.drawLine(QPointF(rect.right() - 30, rect.bottom() - 1),
                       QPointF(rect.right() - 8, rect.bottom() - 1))

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 5, W, 15), Qt.AlignmentFlag.AlignCenter,
                   "DROP FILE  /  BROWSE")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 19, W, 12), Qt.AlignmentFlag.AlignCenter,
                   "IMAGE   VIDEO   AUDIO   DOCS   CODE")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 400, 588

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self._refresh_voice_btns()

        # ── UI colour — colour wheel ─────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow wires it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the selected colour; hex box + wheel stay in sync, theme is live-previewed."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # While dragging: update the hex box, don't apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # If a preview was applied, revert to the colour from launch
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone JARVIS listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'JARVIS can't hear me' is usually 'JARVIS is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed

    # THESE EXIST BECAUSE QTimer.singleShot FROM A WORKER THREAD NEVER FIRES.
    #
    # The first version posted results back with `QTimer.singleShot(0, fn)` from
    # inside the audio thread. A QTimer belongs to the thread that creates it,
    # and that thread has no event loop — so the timer was created, never ran,
    # and the panel sat on "Speak now…" for ever while the recording finished
    # silently behind it. Reported as "I saw no sign of anything at all", which
    # is precisely what it looked like.
    #
    # A signal is the thread-safe way across: Qt queues it onto the receiving
    # object's thread, which for a widget is the GUI thread. Both carry only
    # plain values — never a widget — because the widgets belong to the GUI
    # thread and the worker must not touch them even to pass them along.
    _level_seen = pyqtSignal(int)          # 0-100, while recording
    _test_result = pyqtSignal(str, bool)   # sentence, was it good

    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        _test_css = (
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}"
            f"QPushButton:disabled {{ color: {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)

            # THE ONE CHECK THE STARTUP PROBE CANNOT MAKE.
            #
            # core/audio_devices decides at launch whether a device opens and
            # whether it moves audio, and it does that with silence. A muted
            # microphone, a level slider at zero and a privacy shutter all pass
            # every one of those tests and still deliver a flat line — the only
            # thing that separates them from a working microphone is a person
            # speaking into it.
            #
            # So the button is here, beside the dropdown, in the panel somebody
            # opens when JARVIS cannot hear them. It replaces a loose
            # `ses_testi.py` in the project root that nobody would ever find.
            btn_row = QHBoxLayout(); btn_row.setSpacing(6)
            btn_row.addWidget(box, 1)
            test = QPushButton("TEST")
            test.setFixedSize(64, 30)
            test.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            test.setCursor(Qt.CursorShape.PointingHandCursor)
            test.setStyleSheet(_test_css)
            btn_row.addWidget(test)
            lay.addLayout(btn_row)

            # A meter, because a static label is indistinguishable from a hang.
            # Three seconds of "Speak now" with nothing moving was reported as
            # "I saw no sign of anything" — correctly, since nothing on screen
            # responded to the user's voice.
            meter = QProgressBar()
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setFixedHeight(6)
            meter.setStyleSheet(
                f"QProgressBar {{ background: #000d12; border: 1px solid {C.BORDER}; "
                f"border-radius: 3px; }}"
                f"QProgressBar::chunk {{ background: {C.PRI}; border-radius: 2px; }}")
            meter.hide()
            lay.addWidget(meter)

            result = QLabel("")
            result.setWordWrap(True)
            result.setFont(QFont("Courier New", 7))
            result.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(result)

            test.clicked.connect(
                lambda _=False, k=kind, b=box, t=test, r=result, m=meter:
                    self._run_test(k, b, t, r, m))
            return box

        # Which row is being tested right now. One at a time — the button is
        # disabled for the duration — so a single slot is enough, and it keeps
        # widgets off the signal payload.
        self._active = None                # (button, label, meter)
        self._level_seen.connect(self._on_level)
        self._test_result.connect(self._on_result)

        self._in_box  = _row("MICROPHONE — what JARVIS hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what JARVIS talks through",
                             "output", get_output_device())

        # ── Output volume, beside the output device ──────────────────────────
        #
        # Choosing "Realtek(R) Audio" here and finding it far too quiet used to
        # mean leaving this panel, opening the system sound settings, finding
        # the same endpoint again and moving a slider there. The volume belongs
        # next to the device it applies to.
        _vol_now = None
        try:
            from core.audio_devices import get_output_volume
            # The SELECTED output, not the system default. Somebody puts the
            # assistant on their speakers and keeps everything else in their
            # headphones; a slider labelled with one device that moves the
            # other is worse than no slider at all.
            _vol_now = get_output_volume(self._out_box.currentData() or "")
        except Exception:
            pass

        if _vol_now is not None:
            vcap = QLabel("OUTPUT VOLUME")
            vcap.setFont(QFont("Courier New", 8))
            vcap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addSpacing(4)
            lay.addWidget(vcap)

            vrow = QHBoxLayout(); vrow.setSpacing(8)
            self._vol = QSlider(Qt.Orientation.Horizontal)
            self._vol.setRange(0, 100)
            self._vol.setValue(int(_vol_now))
            self._vol.setFixedHeight(22)
            self._vol.setStyleSheet(
                f"QSlider::groove:horizontal {{ background: #000d12; height: 4px; "
                f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
                f"QSlider::sub-page:horizontal {{ background: {C.PRI_DIM}; "
                f"border-radius: 2px; }}"
                f"QSlider::handle:horizontal {{ background: {C.PRI}; width: 10px; "
                f"margin: -5px 0; border-radius: 5px; }}")
            self._vol_lbl = QLabel(f"{int(_vol_now)}%")
            self._vol_lbl.setFont(QFont("Courier New", 8))
            self._vol_lbl.setFixedWidth(40)
            self._vol_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            vrow.addWidget(self._vol, 1)
            vrow.addWidget(self._vol_lbl)
            lay.addLayout(vrow)

            # valueChanged fires for every pixel of a drag, and each one is a
            # COM call into the audio endpoint. sliderReleased fires once, when
            # the user has decided — the label still follows the handle so the
            # number moves while they drag.
            self._vol.valueChanged.connect(
                lambda v: self._vol_lbl.setText(f"{v}%"))
            self._vol.sliderReleased.connect(self._apply_volume)
            self._out_box.currentIndexChanged.connect(
                lambda _i: self._volume_follows_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply_volume(self):
        """Set the system output volume from the slider, off the Qt thread.

        The Windows path goes through COM, which can block for tens of
        milliseconds; that is not long enough to see but is long enough to
        stutter a drag, and there is no reason to make the interface wait for
        it.
        """
        import threading
        value = self._vol.value()
        device = self._out_box.currentData() or ""

        def _work():
            from core import audio_devices
            if not audio_devices.set_output_volume(value, device):
                print(f"[Audio] the volume of '{device}' could not be set "
                      f"to {value}%")

        threading.Thread(target=_work, daemon=True, name="audio-volume").start()

    def _volume_follows_device(self):
        """Re-read the slider when the chosen output changes.

        Each device keeps its own level, so leaving the handle where it was
        would show the previous device's volume against the new device's name —
        and the next drag would then jump it there.
        """
        if not hasattr(self, "_vol"):
            return
        try:
            from core.audio_devices import get_output_volume
            now = get_output_volume(self._out_box.currentData() or "")
        except Exception:
            now = None
        if now is None:
            return
        self._vol.blockSignals(True)
        self._vol.setValue(int(now))
        self._vol.blockSignals(False)
        self._vol_lbl.setText(f"{int(now)}%")

    def _run_test(self, kind, box, button, label, meter=None):
        """Record or play for real, off the Qt thread.

        Both calls block for seconds — recording waits for the user to speak,
        the tone waits for itself — and doing that on the Qt thread freezes the
        HUD, which is the mistake Mark LV already paid for once with a 2.1
        second wake-word import in the settings drawer.

        Results come back by signal, not by timer. See the note on
        `_level_seen` above for why that distinction is the whole feature.
        """
        import threading

        name = box.currentData() or ""
        self._active = (button, label, meter)
        button.setEnabled(False)
        label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        label.setText("Speak now…" if kind == "input" else "Playing a tone…")
        if meter is not None and kind == "input":
            meter.setValue(0)
            meter.show()

        def _work():
            from core import audio_devices
            try:
                if kind == "input":
                    res = audio_devices.measure_input(
                        name,
                        on_level=lambda f: self._level_seen.emit(int(f * 100)))
                    text = f"peak {res['peak']}  —  {res['verdict']}"
                else:
                    res = audio_devices.play_tone(name)
                    text = res["verdict"]
                good = bool(res.get("ok", False))
            except Exception as e:
                text, good = f"The test itself failed: {e}", False
            self._test_result.emit(text, good)

        threading.Thread(target=_work, daemon=True, name=f"audio-test-{kind}").start()

    def _on_level(self, value: int):
        """A live level, already on the GUI thread thanks to the signal."""
        if self._active and self._active[2] is not None:
            self._active[2].setValue(max(0, min(100, value)))

    def _on_result(self, text: str, good: bool):
        if not self._active:
            return
        button, label, meter = self._active
        self._active = None
        button.setEnabled(True)
        if meter is not None:
            meter.setValue(0)
            meter.hide()
        label.setStyleSheet(
            f"color: {C.PRI if good else C.TEXT_DIM}; background: transparent;")
        label.setText(text)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything JARVIS has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("🧠  WHAT JARVIS REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)


class _ScrollSafeCombo(QComboBox):
    """A dropdown that does not change when you scroll past it.

    Qt's default is that a combo box under the pointer swallows the wheel and
    steps its own value with it. Inside a scrolling panel that is not a
    convenience, it is a trap: scrolling down the list of settings silently
    changes whichever ones the pointer crossed on the way, and nothing on screen
    says so. It was reported as "selections move around when I scroll", and it
    is the same bug every Qt settings panel has until somebody fixes it.

    The wheel is only accepted when the box has been clicked into. Otherwise the
    event is passed up, which is what makes the panel scroll instead.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # StrongFocus so it can still be tabbed to and used from the keyboard;
        # the wheel is gated on having focus, not on being focusable.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class ProvidersOverlay(_HudOverlay):
    """Choose what runs the assistant: Gemini Live, or your own stack.

    WHAT THE FIRST VERSION GOT WRONG, AND WHY IT IS WORTH WRITING DOWN
        It listed the PROVIDERS, and a provider here covers thirteen services at
        once because they all speak the same protocol. That is the right shape
        for the code — it is why `llm_openai.py` is one file instead of thirteen
        — and it is the wrong shape for a person.

        Watched in use: somebody who wanted to run a model on their own machine
        opened the dropdown, saw a single entry reading "OpenAI-compatible", and
        concluded the feature was not there. They were right to. Nobody looking
        for Ollama searches for the name of a protocol, and "OpenAI" is the last
        word you expect to find under "run it locally".

        So the panel now lists what people are actually looking for — Ollama, LM
        Studio, Groq, DeepSeek — from each provider's own `catalog`. One file
        behind the scenes, thirteen recognisable names in front of them.

    THE OTHER TWO CHANGES
        It was 560 pixels of stacked dropdowns with no grouping, which reads as
        a form to be endured rather than three decisions to be made. It is 760
        wide now, and the three parts of a conversation are three numbered boxes
        that say what they do in plain words.

        And every field a provider declares was shown at once — download folders,
        stability weights, thread counts. A setting almost nobody changes costs
        more in the confusion it adds to the five that matter than it saves for
        the one person who wants it, so `advanced` fields sit behind MORE.

    WHAT IT COSTS WHEN NOBODY OPENS IT
        Nothing. The button is built with the rest of the SETUP drawer, but the
        `providers` package is imported inside `__init__` of THIS class — on the
        click. Same lesson as the wake-word check that spent a measured 2.104
        seconds importing `openwakeword` on the Qt thread and made the drawer
        look broken.
    """

    changed = pyqtSignal()          # emitted on APPLY when the engine moved
    # Emitted from a worker thread when something the machine had to be asked
    # for — the installed voices — has arrived. A signal rather than a direct
    # call because the worker must not touch a widget.
    _live_ready = pyqtSignal()
    # (provider id, [(value, label), ...]) from a worker that went and asked an
    # endpoint what it offers.
    _models_ready = pyqtSignal(str, list)
    _OW = 760                       # the window's own minimum is 820

    # Which provider fills each slot, and what the slot is FOR — in the words
    # somebody configuring it would use, not the words the code uses.
    _SLOTS = (
        ("stt", "①", "HEARING",  "turns what you say into words"),
        ("llm", "②", "THINKING", "understands, decides, uses your tools"),
        ("tts", "③", "SPEAKING", "reads the answer out loud"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        # Imported HERE, on the click — never at startup. See the class docstring.
        from memory.config_manager import (get_pipeline, get_provider_config,
                                           get_provider_mode, get_sidecar_first,
                                           get_sidecar_provider)
        from providers import registry

        self._registry = registry
        self._get_cfg = get_provider_config
        # Warm anything a provider can only learn by asking the machine, off
        # this thread. The installed voice list costs a PowerShell subprocess —
        # measured at 1.70 seconds — and the form asks for it on every rebuild,
        # so without this the panel freezes for most of two seconds every time
        # a dropdown is touched. Started here rather than at startup, because
        # nothing in this folder may run before somebody opens it.
        self._live_ready.connect(self._on_live_ready)
        self._models_ready.connect(self._on_models_ready)
        self._model_cache: dict[str, list] = {}
        self._fetching: set = set()
        for _kind in ("tts", "stt", "llm"):
            for _decl in registry.discover(_kind):
                try:
                    _mod = __import__(f"providers.{_decl['_module']}",
                                      fromlist=["prefetch"])
                    _pf = getattr(_mod, "prefetch", None)
                    if _pf:
                        _pf(self._live_ready.emit)
                except Exception:
                    pass
        self._edits: dict[str, dict] = {}       # provider id -> {key: (widget, Field)}
        self._status: dict[str, QLabel] = {}    # provider id -> status line
        self._more: dict[str, bool] = {}        # provider id -> advanced shown
        # Fields deliberately not drawn — the catalog already answered them, or
        # they are behind MORE. Tracked rather than asked of Qt: see _fields_into.
        self._hidden: dict[str, set] = {}
        self._picked: dict[str, str] = {}       # slot -> catalog entry id

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ProvidersOverlay {{
                background: rgba(0, 6, 10, 249);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        self.setFixedWidth(self._OW)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(10)

        title = QLabel("🔌  ENGINE  &  PROVIDERS")
        title.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        outer.addWidget(title)

        sub = QLabel("Choose what powers the conversation. "
                     "Gemini is the default and needs nothing set up here.")
        sub.setWordWrap(True)
        sub.setFont(QFont("Courier New", 8))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        outer.addWidget(sub)

        # ── Mode: two cards, not a dropdown ─────────────────────────────────
        # A dropdown hides the option you did not choose. This is the one
        # decision on the panel that changes everything below it, and both
        # answers deserve to be readable at the same time.
        self._mode = get_provider_mode()
        cards = QHBoxLayout(); cards.setSpacing(10)
        self._card_gemini = self._mode_card(
            "☁  GEMINI LIVE",
            "One connection carries your voice, its voice, the transcript and "
            "the tools together. The fastest option — about half a second to "
            "the first word.",
            "gemini")
        self._card_local = self._mode_card(
            "🖥  MY OWN SETUP",
            "Your choice of hearing, thinking and speaking. Can run entirely on "
            "this computer, with nothing leaving it. Slower: 1.5 to 4 seconds.",
            "pipeline")
        cards.addWidget(self._card_gemini)
        cards.addWidget(self._card_local)
        outer.addLayout(cards)

        # ── Body ────────────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            f"QScrollBar:vertical {{ background: {C.DARK}; width: 9px; "
            f"margin: 0; border-radius: 4px; }}"
            f"QScrollBar::handle:vertical {{ background: {C.BORDER_B}; "
            f"border-radius: 4px; min-height: 30px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: none; }")
        self._scroll.setMinimumHeight(230)
        self._scroll.setMaximumHeight(360)
        outer.addWidget(self._scroll, 1)

        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 2, 8, 2)
        self._body_lay.setSpacing(10)
        self._scroll.setWidget(self._body)

        # Built BEFORE the body, because `_rebuild_body` writes into it — and
        # added to the layout after, so it still sits under the scroll area.
        self._footer = QLabel("")
        self._footer.setWordWrap(True)
        self._footer.setFont(QFont("Courier New", 7))
        self._footer.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        self._pipe = dict(get_pipeline())
        self._sidecar = get_sidecar_provider()
        self._sidecar_first = get_sidecar_first()
        self._rebuild_body()

        outer.addWidget(self._footer)

        row = QHBoxLayout(); row.setSpacing(8)
        apply_btn = QPushButton("▸  SAVE  &  APPLY")
        apply_btn.setFixedHeight(34)
        apply_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 4px; }}
            QPushButton:hover {{ background: {C.BORDER}; border-color: {C.PRI}; }}
        """)
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn, 2)

        close = QPushButton("CLOSE")
        close.setFixedHeight(34)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        row.addWidget(close, 1)
        outer.addLayout(row)
        self._refresh_cards()

    # ── pieces ───────────────────────────────────────────────────────────────
    def _mode_card(self, title: str, body: str, value: str) -> QPushButton:
        b = QPushButton()
        b.setCheckable(True)
        b.setMinimumHeight(84)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setText(f"{title}\n\n{body}")
        b.setFont(QFont("Courier New", 7))
        b.clicked.connect(lambda _=0, v=value: self._set_mode(v))
        b.setProperty("modeValue", value)
        return b

    def _refresh_cards(self) -> None:
        for card in (self._card_gemini, self._card_local):
            on = card.property("modeValue") == self._mode
            card.setChecked(on)
            card.setStyleSheet(f"""
                QPushButton {{
                    text-align: left; padding: 10px 12px;
                    background: {C.PRI_GHO if on else 'transparent'};
                    color: {C.TEXT if on else C.TEXT_DIM};
                    border: 1px solid {C.PRI_DIM if on else C.BORDER};
                    border-radius: 5px;
                }}
                QPushButton:hover {{ border-color: {C.BORDER_B}; }}
            """)

    def _on_live_ready(self) -> None:
        """A machine-supplied list has arrived — redraw once so it appears."""
        if not self.isHidden():
            self._rebuild_body()

    # ── Asking an endpoint what it has, without being asked to ──────────────
    def _fetch_models(self, pid: str) -> None:
        """Go and get the model list in the background.

        Pressing LIST worked, and making somebody press it was still wrong: at
        the moment a local server is chosen, the one thing they need is the list
        of models on it, and the app can simply go and read it. A button whose
        only job is "now do the obvious next thing" is a button that should not
        exist.

        Off the interface thread, because it is a network call — a local server
        answers in milliseconds and a cloud one does not, and a panel that
        freezes while it finds out is worse than one that fills in a second
        later. Failure is silent: not reaching the endpoint yet is the normal
        state while somebody is still typing an address, and TEST is where a
        real answer about reachability belongs."""
        import threading

        if not pid or pid in self._fetching:
            return
        try:
            values = self._values(pid) or dict(self._get_cfg(pid))
        except Exception:
            values = dict(self._get_cfg(pid))
        if not values.get("base_url") and not values.get("api_key"):
            return
        self._fetching.add(pid)

        def work():
            items = []
            try:
                inst = self._registry.build(pid, values)
                if hasattr(inst, "models"):
                    items = [(m, m) for m in inst.models(timeout=6.0)]
                elif hasattr(inst, "voices"):
                    items = list(inst.voices())
            except Exception:
                items = []
            finally:
                self._fetching.discard(pid)
            if items:
                self._models_ready.emit(pid, items)

        threading.Thread(target=work, daemon=True).start()

    def _prepare(self, pid: str) -> None:
        """Let a provider get itself ready as soon as it is configured.

        Piper uses it to download the chosen voice. Generic on purpose: the
        panel asks whether the module has a `prepare`, and providers that do not
        need one are simply skipped — nothing here knows what a voice is."""
        import threading

        try:
            decl = self._registry.get_declaration(pid)
            mod = __import__(f"providers.{decl['_module']}", fromlist=["prepare"])
            fn = getattr(mod, "prepare", None)
        except Exception:
            fn = None
        if fn is None:
            return
        values = self._values(pid) or dict(self._get_cfg(pid))

        def report(ok, msg):
            colour = C.TEXT_DIM if ok is None else (C.GREEN if ok else C.RED)
            mark = "" if ok is None else ("✓  " if ok else "✕  ")
            QTimer.singleShot(0, lambda: self._say(pid, mark + msg, colour))

        threading.Thread(target=lambda: fn(values, report), daemon=True).start()

    def _on_models_ready(self, pid: str, items: list) -> None:
        self._model_cache[pid] = list(items)
        if self.isHidden():
            return
        key = "voice_id" if any(v != l for v, l in items) else "model"
        entry = self._edits.get(pid, {}).get(key)
        if entry is None:
            entry = self._edits.get(pid, {}).get("model")
        if entry is None or not isinstance(entry[0], QComboBox):
            return
        box = entry[0]
        keep = box.currentText()
        box.clear()
        for value, label in items:
            box.addItem(str(label), str(value))
        j = box.findData(keep)
        if j >= 0:
            box.setCurrentIndex(j)
        elif keep:
            box.setCurrentText(keep)
        self._say(pid, f"{len(items)} model{'s' if len(items) != 1 else ''} "
                       f"found on this server — pick one above.", C.TEXT_DIM)

    def _set_mode(self, value: str) -> None:
        if value == self._mode:
            self._refresh_cards()
            return
        self._mode = value
        self._remember_edits()
        self._refresh_cards()
        QTimer.singleShot(0, self._rebuild_body)

    def _box(self, title_widgets: list) -> tuple[QWidget, QVBoxLayout]:
        """A bordered section. Grouping is the difference between three
        decisions and eleven controls."""
        w = QWidget()
        w.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; "
                        f"border-radius: 5px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(5)
        for tw in title_widgets:
            lay.addWidget(tw)
        return w, lay

    def _label(self, text: str, size: int = 8, colour: str = None,
               bold: bool = False) -> QLabel:
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setFont(QFont("Courier New", size,
                         QFont.Weight.Bold if bold else QFont.Weight.Normal))
        lb.setStyleSheet(f"color: {colour or C.TEXT_MED}; background: transparent; "
                         f"border: none;")
        return lb

    def _combo(self) -> QComboBox:
        box = _ScrollSafeCombo()
        box.setFont(QFont("Courier New", 8))
        box.setFixedHeight(29)
        box.setStyleSheet(
            f"QComboBox {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 9px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox::drop-down {{ border: none; width: 18px; }}"
            f"QComboBox QAbstractItemView {{ background: {C.DARK}; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; "
            f"border: 1px solid {C.BORDER_B}; padding: 2px; }}")
        return box

    def _line(self, text: str = "") -> QLineEdit:
        w = QLineEdit(text)
        w.setFont(QFont("Courier New", 8))
        w.setFixedHeight(27)
        w.setStyleSheet(
            f"QLineEdit {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 9px; }}"
            f"QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}")
        return w

    def _btn(self, text: str, slot, colour: str = None) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(25)
        b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        col = colour or C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {col};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ border-color: {C.BORDER_B}; color: {C.TEXT}; }}
        """)
        b.clicked.connect(slot)
        return b

    # ── catalog ──────────────────────────────────────────────────────────────
    def _catalog(self, kind: str) -> list[tuple[str, str, dict, dict]]:
        """(provider id, entry id, entry, declaration) for one slot.

        A provider with no catalog contributes itself as a single entry, so
        nothing has to declare one to appear."""
        out = []
        for decl in self._registry.discover(kind):
            entries = decl.get("catalog") or (
                {"id": decl["id"], "label": decl["label"],
                 "note": decl.get("hint", ""), "local": None, "values": {}},)
            for e in entries:
                out.append((decl["id"], e["id"], e, decl))
        return out

    def _current_entry(self, kind: str, pid: str) -> str:
        """Which catalog entry is selected.

        STORED, not derived. The first version worked it out by comparing the
        saved address against each entry's — which is elegant and wrong, and it
        showed up immediately: pick "Whisper on this computer", reopen the
        panel, and the box reads "— not set —" while the form beneath it is
        fully drawn. That entry's address is the empty string, so any config
        whose address had ever been touched matched nothing and fell through to
        no selection at all.

        The entry is a CHOICE somebody made, not a fact about the settings.
        Choices get stored. The derivation is kept only as a fallback for a
        config written before this key existed."""
        if not pid:
            return ""
        cfg = self._get_cfg(pid)
        saved = str(cfg.get("catalog") or "").strip()
        entries = [(eid, entry) for _p, eid, entry, _d in self._catalog(kind)
                   if _p == pid]
        if saved and any(eid == saved for eid, _e in entries):
            return saved

        # Written before the key existed: work it out once, from the address.
        best = ""
        for eid, entry in entries:
            vals = entry.get("values") or {}
            if not vals:
                best = best or eid            # a catch-all like "Something else"
                continue
            if all(str(cfg.get(k, "")).rstrip("/") == str(v).rstrip("/")
                   for k, v in vals.items() if k in ("base_url", "backend")):
                return eid
        return best or (entries[0][0] if len(entries) == 1 else "")

    # ── body ─────────────────────────────────────────────────────────────────
    def _clear(self, lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                self._clear(item.layout())

    def _rebuild_body(self) -> None:
        # Keep whatever is in the boxes before they are destroyed.
        #
        # A redraw can be triggered by things the person did not do: a model
        # list arriving from a background fetch, a slot changing, the panel
        # settling after a click. Every one of those replaces the widgets, and
        # anything typed but not yet saved went with them — most visibly a model
        # name entered by hand, which came back empty on SAVE with no
        # indication that it had been dropped.
        self._remember_edits()
        self._clear(self._body_lay)
        self._edits.clear()
        self._status.clear()

        if self._mode == "pipeline":
            for slot, num, name, what in self._SLOTS:
                self._body_lay.addWidget(self._slot_box(slot, num, name, what))
        else:
            w, lay = self._box([])
            lay.addWidget(self._label(
                "Nothing to set up. Your voice goes to Gemini and comes back as "
                "speech over one connection, with the transcript and every tool "
                "call on the same wire.", 8, C.TEXT_DIM))
            lay.addWidget(self._label(
                "Pick MY OWN SETUP above to run it on your own machine, or "
                "through another company's model.", 7, C.TEXT_DIM))
            self._body_lay.addWidget(w)

        self._body_lay.addWidget(self._sidecar_box_widget())
        self._body_lay.addStretch(1)
        self._update_footer()

    def _slot_box(self, slot: str, num: str, name: str, what: str) -> QWidget:
        head = self._label(f"{num}  {name}", 9, C.PRI, bold=True)
        w, lay = self._box([head, self._label(what, 7, C.TEXT_DIM)])

        entries = self._catalog(slot)
        box = self._combo()
        box.addItem("— not set —", ("", ""))
        for pid, eid, entry, decl in entries:
            label = entry["label"]
            if entry.get("local") is True:
                label = f"🖥  {label}"
            elif entry.get("local") is False:
                label = f"☁  {label}"
            else:
                label = f"    {label}"
            note = entry.get("note") or ""
            if not decl.get("_available", True):
                note = (note + " · needs install").strip(" ·")
            box.addItem(f"{label}{'   —  ' + note if note else ''}", (pid, eid))

        pid_now = self._pipe.get(slot, "")
        eid_now = self._picked.get(slot) or self._current_entry(slot, pid_now)
        idx = box.findData((pid_now, eid_now)) if pid_now else 0
        if pid_now and idx < 0:
            # A provider IS chosen — the form for it is drawn right below — so
            # the box must never read "not set". It did, and it was the first
            # thing anybody noticed: an entry whose settings no longer match
            # any catalog row (upgraded from an older layout, or edited by
            # hand) resolved to nothing at all, and the panel contradicted
            # itself on screen. Falling back to that provider's first row is
            # always closer to the truth than claiming nothing is selected.
            for i in range(box.count()):
                data = box.itemData(i)
                if data and data[0] == pid_now:
                    idx = i
                    self._picked[slot] = data[1]
                    break
        box.setCurrentIndex(idx if idx >= 0 else 0)
        box.currentIndexChanged.connect(
            lambda _i, s=slot, b=box: self._slot_changed(s, b))
        if pid_now:
            QTimer.singleShot(0, lambda p=pid_now: self._prepare(p))
        lay.addWidget(box)

        if pid_now:
            self._fields_into(lay, slot, pid_now, eid_now)
            # If a list was already fetched, use it; if not, go and get one.
            if self._model_cache.get(pid_now):
                QTimer.singleShot(0, lambda p=pid_now: self._on_models_ready(
                    p, self._model_cache[p]))
            else:
                QTimer.singleShot(0, lambda p=pid_now: self._fetch_models(p))
        return w

    def _slot_changed(self, slot: str, box: QComboBox) -> None:
        pid, eid = box.currentData() or ("", "")
        self._remember_edits()
        self._pipe[slot] = pid
        self._picked[slot] = eid
        if pid and eid:
            # Applying the shortcut is the entire point of the catalog: pick
            # Ollama and the address is already right. The choice itself is
            # saved alongside it — see _current_entry for why.
            from memory.config_manager import save_provider_config
            for _p, _e, entry, _d in self._catalog(slot):
                if _p == pid and _e == eid:
                    values = dict(entry.get("values") or {})
                    values["catalog"] = eid
                    save_provider_config(pid, values)
                    break
            self._fetch_models(pid)
        QTimer.singleShot(0, self._rebuild_body)

    def _fields_into(self, lay, slot: str, pid: str, eid: str) -> None:
        from providers.base import Field

        decl = self._registry.get_declaration(pid)
        if decl is None:
            return
        stored = dict(self._get_cfg(pid))
        entry = next((e for p, i, e, _ in self._catalog(slot)
                      if p == pid and i == eid), {})
        is_local = entry.get("local") is True
        entry_values = entry.get("values") or {}
        # An entry that MENTIONS the address has answered it — including
        # answering it with "", which is what the local speech recogniser does
        # because it does not use one at all. Testing the value rather than the
        # presence of the key is what put an empty "API address" box under
        # "Whisper on this computer", inviting somebody to fill in a field that
        # backend never reads.
        custom = "base_url" not in entry_values

        # Three buckets, tracked explicitly.
        #
        # `isHidden()` is NOT a usable test for "I hid this on purpose": a widget
        # that has not been added to a layout yet is already hidden, so asking it
        # returned True for every field and the form came up completely empty.
        # Which of these a field lands in is a decision made here, so it is kept
        # here rather than inferred from Qt afterwards.
        shown: dict = {}        # drawn now
        carried: dict = {}      # kept for saving, never drawn — answered by the catalog
        advanced: list = []     # drawn only behind MORE

        for f in decl.get("needs", ()):
            if not isinstance(f, Field):
                continue
            # Do not show what the catalog entry already answered. The address
            # somebody did not have to type is one more thing to read and one
            # more thing to break; a key a local server never asks for is a box
            # that makes people think they need an account.
            if f.key == "base_url" and not custom:
                carried[f.key] = (self._line(
                    str(stored.get(f.key, f.default) or "")), f)
                continue
            if f.key == "api_key" and is_local:
                carried[f.key] = (self._line(str(stored.get(f.key, "") or "")), f)
                continue
            if f.key == "backend":
                carried[f.key] = (self._line(
                    str(entry.get("values", {}).get("backend")
                        or stored.get(f.key, f.default) or "")), f)
                continue
            w = self._make_widget(pid, f, stored.get(f.key, f.default))
            if f.advanced:
                advanced.append((f, w))
            else:
                shown[f.key] = (w, f)

        for _key, (w, f) in shown.items():
            lay.addWidget(self._label(
                f.label + ("" if f.required else "   (optional)"), 7, C.TEXT_DIM))
            lay.addWidget(w)
            # A text box already shows its hint as placeholder text. Adding the
            # label as well printed every hint twice, which is how the panel
            # came to look careless.
            if f.hint and not isinstance(w, QLineEdit):
                lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))

        fields = dict(shown)
        fields.update(carried)
        self._hidden[pid] = set(carried) | {f.key for f, _w in advanced}

        # Buttons and status.
        status = self._label("", 7, C.TEXT_DIM)
        self._status[pid] = status

        brow = QHBoxLayout(); brow.setSpacing(6)
        brow.addWidget(self._btn("TEST", lambda _=0, p=pid: self._test(p), C.PRI))
        # Not "LIST" any more: the list arrives on its own when a service is
        # chosen. This is the button for when it did not — the server was
        # starting up, or a model has just been pulled.
        if any(k in shown for k in ("model", "voice_id", "voice")):
            brow.addWidget(self._btn("↻ REFRESH", lambda _=0, p=pid: self._list(p)))
        if advanced:
            brow.addWidget(self._btn(
                ("⌃ LESS" if self._more.get(pid) else "⌄ MORE"),
                lambda _=0, p=pid: self._toggle_more(p)))
        if not decl.get("_available", True) and decl.get("install"):
            brow.addWidget(self._btn(f"INSTALL {decl['install']}",
                                     lambda _=0, p=pid: self._install(p), C.ACC2))
        brow.addStretch(1)
        holder = QWidget()
        holder.setStyleSheet("background: transparent; border: none;")
        holder.setLayout(brow)
        lay.addWidget(holder)
        lay.addWidget(status)

        if self._more.get(pid):
            lay.addWidget(self._label("─" * 46, 7, C.BORDER))
            for f, w in advanced:
                lay.addWidget(self._label(f"{f.label}   (optional)", 7, C.TEXT_DIM))
                lay.addWidget(w)
                if f.hint and not isinstance(w, QLineEdit):
                    lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))
                fields[f.key] = (w, f)
                self._hidden[pid].discard(f.key)
        else:
            for f, w in advanced:
                fields[f.key] = (w, f)

        self._edits[pid] = fields

    def _make_widget(self, pid: str, f, value):
        if f.kind == "choice":
            w = self._combo()
            choices = list(self._live_choices(pid, f.key) or f.choices)
            if not f.required:
                w.addItem("— default —", "")
            for c in choices:
                w.addItem(str(c), str(c))
            w.setEditable(f.key in ("base_url", "model", "voice_id"))
            if w.isEditable():
                w.lineEdit().setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            if value:
                j = w.findData(str(value))
                if j < 0:
                    w.addItem(str(value), str(value))
                    j = w.count() - 1
                w.setCurrentIndex(j)
                if w.isEditable():
                    w.setCurrentText(str(value))
            return w
        if f.kind == "bool":
            w = self._combo()
            w.addItem("no", "")
            w.addItem("yes", "1")
            w.setCurrentIndex(1 if str(value) in ("1", "True", "true") else 0)
            return w
        w = self._line(str(value if value is not None else ""))
        if f.kind == "secret":
            w.setEchoMode(QLineEdit.EchoMode.Password)
        if f.hint:
            w.setPlaceholderText(f.hint[:64])
        return w

    def _toggle_more(self, pid: str) -> None:
        self._more[pid] = not self._more.get(pid)
        self._remember_edits()
        QTimer.singleShot(0, self._rebuild_body)

    def _sidecar_box_widget(self) -> QWidget:
        head = self._label("⚡  BACKGROUND REQUESTS", 9, C.PRI, bold=True)
        w, lay = self._box([head])
        lay.addWidget(self._label(
            "Reading a document, looking at your screen, writing code — the "
            "assistant's own requests, not the conversation. These use Gemini's "
            "text quota, which runs out before the voice does. Sending them "
            "somewhere else keeps them working, and changes nothing you hear.",
            7, C.TEXT_DIM))

        # Read the stored choice BEFORE the box exists. The first version asked
        # `_sidecar_choice()`, which reads the combo — and at this point that is
        # the empty box being built, so the answer was always "nothing chosen"
        # and the form under it never appeared.
        cur_pid = self._sidecar
        cur_eid = self._current_entry("llm", cur_pid) if cur_pid else ""

        self._sidecar_combo = self._combo()
        self._sidecar_combo.addItem("Gemini only  —  the way it has always been",
                                    ("", ""))
        for pid, eid, entry, decl in self._catalog("llm"):
            mark = "🖥" if entry.get("local") else ("☁" if entry.get("local")
                                                   is False else "  ")
            self._sidecar_combo.addItem(
                f"{mark}  Fall back to {entry['label']}", (pid, eid))
        i = self._sidecar_combo.findData((cur_pid, cur_eid)) if cur_pid else 0
        if cur_pid and i < 0:
            for j in range(1, self._sidecar_combo.count()):
                if (self._sidecar_combo.itemData(j) or ("", ""))[0] == cur_pid:
                    i = j
                    break
        self._sidecar_combo.setCurrentIndex(max(0, i))
        self._sidecar_combo.currentIndexChanged.connect(self._sidecar_changed)
        lay.addWidget(self._sidecar_combo)

        self._first_combo = self._combo()
        self._first_combo.addItem("Try Gemini first  —  faster", "after")
        self._first_combo.addItem(
            "Try the other one first  —  keeps more on your machine", "before")
        self._first_combo.setCurrentIndex(1 if self._sidecar_first else 0)
        lay.addWidget(self._first_combo)

        # In pipeline mode the model chosen for ② THINKING answers these too, so
        # there is nothing more to fill in. In Gemini mode there IS no ② section
        # — and the first version still said "it uses whatever you set up under
        # ② THINKING", which named a box that was not on the screen. So a
        # service picked here gets its own form, right here.
        if cur_pid:
            if self._mode == "pipeline" and cur_pid == self._pipe.get("llm"):
                lay.addWidget(self._label(
                    "Uses the model you chose for ② THINKING.", 7, C.TEXT_DIM))
            else:
                self._fields_into(lay, "llm", cur_pid, cur_eid)
        else:
            lay.addWidget(self._label(
                "Left as it is, these keep using Gemini and stop working when "
                "its text quota runs out.", 7, C.TEXT_DIM))
        return w

    def _sidecar_choice(self) -> tuple:
        """(provider id, catalog entry id) for the background-request model."""
        box = getattr(self, "_sidecar_combo", None)
        if box is not None:
            data = box.currentData()
            if isinstance(data, tuple):
                return data
        pid = self._sidecar
        return (pid, self._current_entry("llm", pid) if pid else "")

    def _sidecar_changed(self, _ix) -> None:
        pid, eid = self._sidecar_choice()
        self._remember_edits()
        self._sidecar = pid
        if pid and eid:
            from memory.config_manager import save_provider_config
            for _p, _e, entry, _d in self._catalog("llm"):
                if _p == pid and _e == eid:
                    values = dict(entry.get("values") or {})
                    values["catalog"] = eid
                    save_provider_config(pid, values)
                    break
        QTimer.singleShot(0, self._rebuild_body)

    def _update_footer(self) -> None:
        if self._mode != "pipeline":
            self._footer.setText(
                "Gemini Live. Nothing on this panel changes the conversation.")
            return
        missing = [name for slot, _n, name, _w in self._SLOTS
                   if not self._pipe.get(slot)]
        if "THINKING" in missing:
            self._footer.setText(
                "⚠  ② THINKING is not set — without it the assistant falls back "
                "to Gemini Live and says so in the log.")
        elif missing:
            words = " and ".join(f"{m.lower()}" for m in missing)
            self._footer.setText(
                f"Only {words} is missing — it will run, but you will type "
                f"instead of speak, or read instead of listen.")
        else:
            self._footer.setText(
                "All three set. SAVE & APPLY reconnects; the conversation is kept.")

    # ── actions ──────────────────────────────────────────────────────────────
    def _live_choices(self, pid: str, key: str) -> tuple:
        try:
            decl = self._registry.get_declaration(pid)
            mod = __import__(f"providers.{decl['_module']}", fromlist=["choices_for"])
            fn = getattr(mod, "choices_for", None)
            return tuple(fn(key)) if fn else ()
        except Exception:
            return ()

    def _values(self, pid: str) -> dict:
        out = {}
        hidden = self._hidden.get(pid, set())
        stored = self._get_cfg(pid)
        for key, (w, _f) in self._edits.get(pid, {}).items():
            if isinstance(w, QComboBox):
                value = (w.currentText().strip() if w.isEditable()
                         else (w.currentData() or ""))
            else:
                value = w.text().strip()
            # A field nobody can see must never blank out a setting that has
            # one. These widgets are rebuilt on every redraw, and a redraw that
            # raced a background fetch could recreate a hidden address as empty
            # and then save that over a working one — leaving a pipeline that
            # had been configured correctly pointing at nothing, with no visible
            # cause. Clearing a value is still possible; it just has to be done
            # in a box that is on screen.
            if not value and key in hidden and stored.get(key):
                continue
            out[key] = value
        return out

    def _remember_edits(self) -> None:
        """Keep what has been typed when the form is rebuilt.

        Without it, changing the speech engine wiped an API key somebody had
        just entered into the box above."""
        from memory.config_manager import save_provider_config
        for pid in list(self._edits):
            try:
                save_provider_config(pid, self._values(pid))
            except Exception:
                pass

    def _say(self, pid: str, text: str, colour: str) -> None:
        lb = self._status.get(pid)
        if lb is not None:
            lb.setText(text)
            lb.setStyleSheet(f"color: {colour}; background: transparent; border: none;")

    def _test(self, pid: str) -> None:
        # Read the boxes BEFORE letting Qt run. `processEvents` will happily
        # deliver a queued rebuild, which deletes the very widgets this is
        # about to read — and a deleted QWidget raises rather than returning
        # something wrong, so it takes the click down with it.
        values = self._values(pid)
        self._say(pid, "checking…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            probe = getattr(inst, "probe", None)
            if probe is None:
                self._say(pid, "Nothing to check for this one.", C.TEXT_DIM)
                return
            ok, msg = probe()
        except Exception as e:
            self._say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return
        self._say(pid, ("✓  " if ok else "✕  ") + msg, C.GREEN if ok else C.ACC)
        # TEST is also where "can this model see?" gets answered — by asking the
        # endpoint, not the user. Write the answer back into the stored settings
        # so the assistant is told the truth about its own eyes.
        seen = (getattr(inst, "caps", {}) or {}).get("vision")
        if ok and seen is not None:
            from memory.config_manager import save_provider_config
            save_provider_config(pid, {"vision": "1" if seen else ""})
            entry = self._edits.get(pid, {}).get("vision")
            if entry is not None and isinstance(entry[0], QComboBox):
                entry[0].setCurrentIndex(1 if seen else 0)

    def _list(self, pid: str) -> None:
        values = self._values(pid)          # before processEvents — see _test
        self._say(pid, "reading…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            if hasattr(inst, "models"):
                items, key = [(m, m) for m in inst.models()], "model"
            elif hasattr(inst, "voices"):
                items, key = list(inst.voices()), "voice_id"
            else:
                self._say(pid, "Nothing to list for this one.", C.TEXT_DIM)
                return
        except Exception as e:
            self._say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return

        entry = self._edits.get(pid, {}).get(key)
        if entry is None or not items:
            self._say(pid, f"✓  {len(items)} found." if items else "None found.",
                      C.GREEN if items else C.TEXT_DIM)
            return
        box = entry[0]
        if isinstance(box, QComboBox):
            keep = box.currentText()
            box.clear()
            for value, label in items:
                box.addItem(str(label), str(value))
            j = box.findData(keep)
            box.setCurrentIndex(j if j >= 0 else 0)
        else:
            self._say(pid, f"✓  {len(items)} available: "
                           f"{', '.join(str(v) for v, _ in items[:8])}", C.GREEN)
            return
        self._say(pid, f"✓  {len(items)} available — pick one above.", C.GREEN)

    def _install(self, pid: str) -> None:
        """Fetch a provider's optional dependency, off the Qt thread — pip takes
        tens of seconds and a frozen window looks like a crashed one."""
        import subprocess
        import sys as _sys
        import threading

        decl = self._registry.get_declaration(pid) or {}
        pkg = decl.get("install")
        if not pkg:
            return
        self._say(pid, f"installing {pkg} — this can take a minute…", C.ACC2)

        def work():
            try:
                r = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", pkg],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
                good = r.returncode == 0
                tail = r.stdout.decode("utf-8", "replace").strip().splitlines()
                msg = ("✓  installed — close and reopen this panel"
                       if good else (tail[-1][:150] if tail else "pip failed"))
            except Exception as e:
                good, msg = False, f"{type(e).__name__}: {str(e)[:120]}"
            QTimer.singleShot(0, lambda: self._say(pid, msg,
                                                   C.GREEN if good else C.RED))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self) -> None:
        from memory.config_manager import (get_pipeline, get_provider_mode,
                                           save_providers)

        before = (get_provider_mode(), dict(get_pipeline()))

        # ONE write, not five. See save_providers for what the five cost.
        creds = {pid: self._values(pid) for pid in list(self._edits)}
        for slot, eid in self._picked.items():
            pid = self._pipe.get(slot, "")
            if pid and eid:
                creds.setdefault(pid, {})["catalog"] = eid
        sidecar_pid, sidecar_eid = self._sidecar_choice()
        if sidecar_pid and sidecar_eid:
            creds.setdefault(sidecar_pid, {}).setdefault("catalog", sidecar_eid)

        save_providers(
            mode=self._mode,
            pipeline={"stt": self._pipe.get("stt", ""),
                      "llm": self._pipe.get("llm", ""),
                      "tts": self._pipe.get("tts", "")},
            creds=creds,
            sidecar=sidecar_pid,
            sidecar_first=self._first_combo.currentData() == "before",
        )

        # Providers are cached by configuration; a changed setting must not go
        # on being served by an object built from the old one.
        try:
            self._registry.reset()
        except Exception:
            pass

        after = (get_provider_mode(), dict(get_pipeline()))
        self.hide()
        # Background-request settings take effect on the next call with no
        # reconnect. Only the conversation engine needs the session rebuilt.
        if before != after:
            self.changed.emit()


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None,
                 bind_error: str = ""):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        # THE ONE FAILURE THAT LOOKS LIKE THE SERVER BEING OFF.
        #
        # Windows writes a BLOCK rule for an executable when its "Allow access?"
        # prompt is dismissed, and a block overrides every allow rule there is —
        # including the port rule this app carefully creates. The phone then
        # reports "refused to connect" while the server is running perfectly.
        #
        # It is invisible from the HUD, which is where the person scanning the
        # QR code is looking, and it is per-EXECUTABLE: the shortcut runs
        # pythonw.exe while an editor runs python.exe, so it can block one launch
        # method and not the other on the same machine. That difference is
        # unguessable without being told, so it is said here.
        # Whatever is stopping this from working, say it HERE — beside the QR
        # code, where the person about to scan it is looking. A dashboard that
        # cannot be reached and does not explain why is the single most
        # frustrating failure this app has.
        if bind_error:
            lay.addWidget(_lbl("⚠  " + bind_error, 8, color="#f4b942"))

        try:
            from dashboard.server import blocking_firewall_rules
            _blocked = blocking_firewall_rules()
        except Exception:
            _blocked = []
        if _blocked:
            warn = _lbl(
                "⚠  Windows is blocking incoming connections to this copy of "
                "Python, so your phone cannot reach this page. Restart JARVIS "
                "and accept the Windows prompt, or remove the rules named "
                + ", ".join(sorted({n for n, _ in _blocked}))
                + " under Windows Defender Firewall → Inbound Rules.",
                8, color="#f4b942")
            lay.addWidget(warn)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _wake_btns_sig   = pyqtSignal()          # wake state resolved off-thread
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # ── Video surface ────────────────────────────────────────────────
        # Built exactly like the camera page above and stacked beside it,
        # because it is the same idea: something takes the centre of the HUD
        # for a while and then gives it back. Sharing the stack rather than
        # inventing a second mechanism means the avatar, the camera and a video
        # can never be on screen at once.
        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did JARVIS close the mic, or the user?
        # A plain flag rather than reading the widget. video_is_playing() is
        # called from plugin threads, and reading a widget's state from one is
        # not something to rely on; an attribute is.
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        # Muted is the default and the button says so, because a soundtrack
        # talking over JARVIS is the one way this feature could make the
        # assistant worse rather than better.
        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = _vid_btn("✕  CLOSE")
        _vid_x.clicked.connect(self.stop_video)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        if HAVE_VIDEO:
            # A GRAPHICS ITEM, NOT A QVideoWidget.
            #
            # QVideoWidget gets a native window of its own, and a native window
            # is painted by the compositor above every ordinary widget that
            # shares the screen with it — so the settings drawer opened
            # *underneath* the video and raise_() could not help, because the
            # two are not in the same painting order at all. A video item drawn
            # into a QGraphicsView goes through Qt's own painter like any other
            # widget, and anything laid over it stays over it.
            self._video_scene = QGraphicsScene(self)
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)
            self._video_widget = QGraphicsView(self._video_scene)
            self._video_widget.setStyleSheet("background: #000; border: none;")
            self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
            self._video_widget.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            _vid_v.addWidget(self._video_widget, stretch=1)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            # Keep the picture filling the view as the window is resized.
            self._video_item.nativeSizeChanged.connect(self._fit_video)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self._on_video_error)

            # A SECOND player, for sound that arrives separately.
            #
            # YouTube no longer serves a single stream carrying both picture and
            # sound — not for new uploads and not for old ones; checked against
            # three videos including the oldest on the site, and every one of
            # them offered zero combined formats. Picture and sound are two
            # URLs, so they are two players, started together and nudged back
            # into line by the timer below.
            #
            # A local file or a direct video URL still uses the first player
            # alone: one stream, one player, nothing to synchronise.
            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self._sync_video_sound)
        else:
            self._video_scene = None
            self._video_item = None
            self._video_widget = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None
            _miss = QLabel("Video playback is not available in this Qt install.")
            _miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            _vid_v.addWidget(_miss, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._ctrl_drawer = self._build_controls_drawer()
        self._warm_wake_state()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._wake_btns_sig.connect(self._refresh_wake_btns)
        self._video_open_sig.connect(self._on_video_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def _on_video_open(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self.write_log("SYS: Video playback is not available in this Qt "
                           "install.")
            return
        # A video and the live camera cannot share the centre of the HUD.
        self._cam_stop.set()

        self._video_title.setText(f"▶  {(title or 'VIDEO')[:44].upper()}")
        self._video_split = bool(audio_source)
        self._set_video_muted(bool(muted))

        url = (QUrl.fromLocalFile(source) if Path(source).exists()
               else QUrl(source))
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        # Re-run now that the video counts as playing: _set_video_muted ran
        # before this line and saw no video, so its mic check was a no-op.
        self._sync_mic_for_video()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def _fit_video(self, *_a) -> None:
        """Size the picture to the panel, keeping its shape."""
        if not (self._video_item and self._video_widget):
            return
        try:
            native = self._video_item.nativeSize()
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            w, h = native.width() * scale, native.height() * scale
            self._video_item.setSize(QSizeF(w, h))
            self._video_scene.setSceneRect(0, 0, w, h)
            self._video_widget.centerOn(self._video_item)
        except Exception:
            pass

    def _on_video_close(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_on = False
        self._hud_cam_stack.setCurrentIndex(0)
        self._sync_mic_for_video()      # gives the microphone back

    def _set_video_muted(self, muted: bool) -> None:
        """Silence whichever output is carrying the sound for this video."""
        muted = bool(muted)
        if self._video_audio:
            # When the sound is a separate stream this player has none, but
            # muting it too costs nothing and keeps the two paths identical.
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self._sync_video_mute_btn()
        self._sync_mic_for_video()

    def _on_video_mute(self, muted: bool) -> None:
        self._set_video_muted(muted)

    def _sync_mic_for_video(self) -> None:
        """Close the microphone while the video is making sound.

        JARVIS subtracts its OWN output from the microphone — that is what
        core/echo.py does — but a video plays through a different output
        entirely, so the guard has never heard of it and the assistant answers
        the film. There is no arrangement in which an open microphone and a
        loudspeaker in the same room do not do that.

        So the microphone closes for exactly as long as the sound is on, and
        opens again by itself the moment it goes off or the video is closed.
        Only if JARVIS closed it: a microphone the user muted themselves stays
        muted, and pressing the mute key during a video hands the decision back
        to them for good.
        """
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self._set_muted(True, "The video's sound is on — silence it, close "
                                  "it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self._set_muted(False, "The video is quiet again.")

    def _sync_video_sound(self) -> None:
        """Keep the separate soundtrack in step with the picture.

        Two players started at the same moment do not stay together: they buffer
        independently, and measured on a real stream they were about half a
        second apart after nine seconds. So the sound is nudged back to the
        picture whenever it drifts far enough to hear — and only then, because
        correcting a smaller gap is itself audible.
        """
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def _sync_video_mute_btn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def _toggle_video_mute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._set_video_muted(not out.isMuted())

    def _on_video_error(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self.write_log(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self._on_video_close()

    def stop_video(self) -> None:
        self._video_close_sig.emit()

    def video_is_playing(self) -> bool:
        return bool(self._video_on)

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()
        if hasattr(self, '_ctrl_drawer') and self._ctrl_drawer.isVisible():
            self._position_ctrl_drawer()
        self._fit_video()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}K"
        else:
            net_str = f"{net:.1f}M"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)
        self._net_graph.set_rate(net)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        disk = snap["disk"]
        self._bar_disk.set_value(disk if disk >= 0 else 0,
                                 f"{disk:.0f}%" if disk >= 0 else "N/A")
        self._bar_npu.set_value(0, "N/A")
        mem_used_gb = snap["mem_used_gb"]
        mem_total_gb = snap["mem_total_gb"]
        self._ram_capacity_bar.setValue(int(mem))
        self._ram_capacity_lbl.setText(
            f"{mem_used_gb:.1f} / {mem_total_gb:.1f} GB"
            if mem_total_gb > 0 else "N/A"
        )
        self._storage_capacity_bar.setValue(int(disk) if disk >= 0 else 0)
        disk_free_gb = snap["disk_free_gb"]
        disk_total_gb = snap["disk_total_gb"]
        self._storage_capacity_lbl.setText(
            f"{disk_free_gb:.0f} GB FREE"
            if disk_free_gb >= 0 and disk_total_gb > 0 else "N/A"
        )

        wifi = snap["wifi"]
        wifi_quality = snap["wifi_quality"]
        self._wifi_signal.set_signal(
            wifi_quality, wifi == "CONNECTED"
        )
        wifi_text = {
            "CONNECTED": "LINK UP",
            "OFFLINE": "LINK OFF",
        }.get(wifi, "N/A")
        self._wifi_value.setText(wifi_text)
        self._wifi_value.setStyleSheet(
            f"color: {C.PRI if wifi == 'CONNECTED' else C.TEXT_DIM}; "
            "background: transparent; border: none;"
        )

        battery = snap["battery"]
        if battery == "N/A":
            battery_text = "N/A"
            battery_color = C.TEXT_DIM
        else:
            charge, _, status = battery.partition("  ")
            status_short = {"CHARGING": "CHARGING", "ON BATTERY": "ON BATTERY"}.get(status, status)
            battery_text = f"{charge}  {status_short}"
            battery_color = C.GREEN if status == "CHARGING" else C.ACC2
        self._battery_value.setText(battery_text)
        self._battery_value.setStyleSheet(
            f"color: {battery_color}; background: transparent; border: none;"
        )
        gpu_text = f"{gpu:.0f}%" if gpu >= 0 else "--"
        disk_text = f"{disk:.0f}%" if disk >= 0 else "--"
        for key, value in (
            ("CPU", f"{cpu:.0f}%"),
            ("GPU", gpu_text),
            ("RAM", f"{mem:.0f}%"),
            ("SSD", disk_text),
        ):
            self._header_metric_values[key].setText(value)
        wifi_short = {"CONNECTED": "UP", "OFFLINE": "OFF"}.get(wifi, "--")
        wifi_reading = (f"{wifi_quality}%  {wifi_short}"
                        if wifi_quality >= 0 else wifi_short)
        battery_short = (
            "N/A" if battery == "N/A"
            else f"{battery.partition('%')[0]}% "
                 f"{'AC' if battery.endswith('CHARGING') else 'BAT'}"
        )
        self._header_system_lbl.setText(
            f"NET {net_str}/s   WI-FI {wifi_reading}   BAT {battery_short}"
        )
        self._header_wifi_signal.set_signal(
            wifi_quality, wifi == "CONNECTED"
        )

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            proc_count = 0
            self._proc_lbl.setText("PROC  --")
        try:
            self._header_link_lbl.setText(
                f"● {self.hud.state}  ·  UP {h:02d}:{m:02d}  ·  {proc_count} PROC"
            )
            self._header_uptime.setText(f"UP  {h:02d}:{m:02d}")
            self._header_processes.setText(f"PROC  {proc_count}")
        except UnboundLocalError:
            self._header_link_lbl.setText("● SYSTEM ONLINE  ·  LINK STANDBY")
            self._header_uptime.setText("UP  --:--")
            self._header_processes.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setObjectName("MainHeader")
        w.setFixedHeight(_HEADER_H)
        w.setStyleSheet(f"""
            QWidget#MainHeader {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #071a2d, stop:0.22 #091426, stop:0.5 #080f1d,
                    stop:0.78 #0a1426, stop:1 #0b1c31);
                border-bottom: 1px solid #1764a0;
            }}
        """)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(18, 6, 18, 6)
        lay.setSpacing(10)

        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand_title = QLabel("J.A.R.V.I.S  /  MARK LVI")
        brand_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        brand_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        brand.addWidget(brand_title)
        self._header_link_lbl = QLabel("● VOICE CORE  ·  SYSTEM READY")
        self._header_link_lbl.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
        self._header_link_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        brand.addWidget(self._header_link_lbl)
        brand_box = QWidget()
        brand_box.setObjectName("HeaderBrand")
        brand_box.setStyleSheet(f"""
            QWidget#HeaderBrand {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0b2034, stop:1 #07111e);
                border: 1px solid #1764a0;
                border-left: 2px solid {C.PRI};
                border-radius: 7px;
            }}
        """)
        brand.setContentsMargins(10, 5, 10, 5)
        brand_box.setLayout(brand)
        lay.addWidget(brand_box)

        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(40, 40)
        self._drawer_btn.setFont(QFont("Segoe UI Symbol", 17))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0d2740, stop:1 #071321);
                color: {C.TEXT_MED};
                border: 1px solid #1b5686; border-radius: 9px;
            }}
            QPushButton:hover {{
                color: #d9f8ff; border-color: #4bcfff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #124d76, stop:1 #0a1b31);
            }}
            QPushButton:checked {{
                color: #e6fbff; border-color: #39c5ff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0d4772, stop:1 #09213b);
            }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.setToolTip("Setup — things you set once")
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)

        # A SECOND drawer, and the split is by how often a thing is touched.
        #
        # One panel held twelve buttons: the ones you press once when you set
        # the assistant up sitting next to the ones you flick on and off every
        # day. Separating them by that — setup behind ⚙, everyday behind 🎛 —
        # is what makes each list short enough to read at a glance.
        self._ctrl_btn = QPushButton("🎛")
        self._ctrl_btn.setFixedSize(40, 40)
        self._ctrl_btn.setFont(QFont("Segoe UI Symbol", 17))
        self._ctrl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ctrl_btn.setToolTip("Controls — the everyday switches")
        self._ctrl_btn.setStyleSheet(self._drawer_btn.styleSheet())
        self._ctrl_btn.setCheckable(True)
        self._ctrl_btn.clicked.connect(self._toggle_controls)
        lay.addSpacing(4)
        lay.addWidget(self._ctrl_btn)
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("Just A Rather Very Intelligent System"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "PERSONAL AI INTERFACE")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Segoe UI", 8))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        mid_box = QWidget()
        mid_box.setObjectName("HeaderIdentity")
        mid_box.setStyleSheet(f"""
            QWidget#HeaderIdentity {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0b2034, stop:0.52 #081424, stop:1 #0b1e32);
                border: 1px solid #174775;
                border-top: 2px solid {C.PRI};
                border-bottom: 1px solid #1b684f;
                border-radius: 8px;
            }}
        """)
        mid.setContentsMargins(8, 3, 8, 2)
        self._sub_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        mid_box.setLayout(mid)
        lay.addWidget(mid_box, stretch=1)

        right_col = QVBoxLayout(); right_col.setSpacing(5)
        metrics_row = QHBoxLayout()
        metrics_row.setContentsMargins(0, 0, 0, 0)
        metrics_row.setSpacing(6)
        self._header_metric_values = {}
        for key, color in (
            ("CPU", "#38baff"),
            ("GPU", "#a88aff"),
            ("RAM", "#35d8ee"),
            ("SSD", "#38d8a5"),
        ):
            metric = QWidget()
            metric.setObjectName("HeaderMetric")
            metric.setStyleSheet(f"""
                QWidget#HeaderMetric {{
                    background: #091a2b;
                    border: 1px solid #174775;
                    border-bottom: 2px solid {color};
                    border-radius: 5px;
                }}
            """)
            metric_lay = QVBoxLayout(metric)
            metric_lay.setContentsMargins(7, 3, 7, 3)
            metric_lay.setSpacing(1)
            name = QLabel(key)
            name.setFont(QFont("Segoe UI", 6, QFont.Weight.Bold))
            name.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            reading = QLabel("--")
            reading.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            reading.setStyleSheet(f"color: {color}; background: transparent;")
            metric_lay.addWidget(name)
            metric_lay.addWidget(reading)
            metrics_row.addWidget(metric, stretch=1)
            self._header_metric_values[key] = reading
        right_col.addLayout(metrics_row)
        system_box = QWidget()
        system_box.setObjectName("HeaderSystemStrip")
        system_box.setStyleSheet("""
            QWidget#HeaderSystemStrip {
                background: #081522;
                border: 1px solid #163b5b;
                border-left: 2px solid #1b684f;
                border-radius: 5px;
            }
        """)
        system_row = QHBoxLayout(system_box)
        system_row.setContentsMargins(5, 1, 4, 1)
        system_row.setSpacing(4)
        self._header_system_lbl = QLabel("NET --   WI-FI --   BAT --")
        self._header_system_lbl.setFont(QFont("Consolas", 7, QFont.Weight.DemiBold))
        self._header_system_lbl.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        self._header_system_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        system_row.addWidget(self._header_system_lbl, stretch=1)
        self._header_wifi_signal = WifiSignalIndicator()
        self._header_wifi_signal.setFixedSize(48, 30)
        system_row.addWidget(self._header_wifi_signal)
        right_col.addWidget(system_box)

        center_status = QHBoxLayout()
        center_status.setContentsMargins(8, 2, 8, 2)
        center_status.setSpacing(6)
        self._header_core_status = QLabel("VOICE CORE  /  INITIALISING")
        self._header_core_status.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
        self._header_core_status.setStyleSheet(
            f"color: {C.GREEN}; background: #081c1b; "
            "border: 1px solid #15513f; border-radius: 4px; padding: 3px 6px;"
        )
        self._header_core_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_status.addWidget(self._header_core_status, stretch=2)
        self._header_uptime = QLabel("UP  --:--")
        self._header_uptime.setFont(QFont("Consolas", 7, QFont.Weight.DemiBold))
        self._header_uptime.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        self._header_uptime.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_status.addWidget(self._header_uptime, stretch=1)
        self._header_processes = QLabel("PROC  --")
        self._header_processes.setFont(QFont("Consolas", 7, QFont.Weight.DemiBold))
        self._header_processes.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        self._header_processes.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_status.addWidget(self._header_processes, stretch=1)
        mid.addLayout(center_status)

        data_content = QWidget()
        data_content.setObjectName("HeaderDataContent")
        data_content.setStyleSheet("QWidget#HeaderDataContent { background: transparent; }")
        data_content.setLayout(right_col)
        data_content.setMinimumWidth(288)
        clock_box = QWidget()
        clock_box.setObjectName("HeaderClock")
        clock_box.setStyleSheet(f"""
            QWidget#HeaderClock {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #102b46, stop:1 #081522);
                border: 1px solid #1a5686;
                border-left: 2px solid {C.PRI};
                border-bottom: 2px solid #1b684f;
                border-radius: 7px;
            }}
        """)
        clock_col = QVBoxLayout(clock_box)
        clock_col.setContentsMargins(9, 8, 9, 8)
        clock_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: #45cbff; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clock_col.addWidget(self._clock_lbl)
        clock_col.addWidget(self._date_lbl)

        data_box = QWidget()
        data_box.setObjectName("HeaderTelemetry")
        data_box.setStyleSheet(f"""
            QWidget#HeaderTelemetry {{
                background: transparent;
                border: none;
            }}
        """)
        right_row = QHBoxLayout(data_box)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(7)
        right_row.addWidget(data_content, stretch=1)
        right_row.addWidget(clock_box)
        data_box.setMinimumWidth(414)
        clock_box.setFixedWidth(134)
        lay.addWidget(data_box)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setObjectName("TelemetryPanel")
        w.setStyleSheet(
            f"QWidget#TelemetryPanel {{ "
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 #06182a, stop:0.45 {C.DARK}, stop:1 #081426); "
            f"border-right: 1px solid {C.BORDER}; }}"
        )
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {C.DARK}; width: 6px; border: none; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("TelemetryContent")
        content.setStyleSheet("QWidget#TelemetryContent { background: transparent; }")
        scroll.setWidget(content)

        lay = QVBoxLayout(content)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        hdr = QLabel("◈  SYSTEM TELEMETRY")
        hdr.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        hdr.setStyleSheet(
            f"color: {C.PRI}; background: transparent; "
            f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 7px;"
        )
        lay.addWidget(hdr)

        self._bar_cpu = RingGauge("CPU", "#168cff")
        self._bar_mem = RingGauge("RAM", "#32dcff")
        self._bar_gpu = RingGauge("GPU", "#9377ff")
        self._bar_net = RingGauge("NET", "#3cdbc2")
        self._bar_disk = RingGauge("SSD", "#4a91f2")
        self._bar_npu = RingGauge("NPU", "#ed70ff")
        self._bar_tmp = RingGauge("THERMAL", C.ACC)
        self._bar_disk.setToolTip(
            "Used capacity on the system volume. Drive health and SSD wear "
            "are not exposed by this cross-platform monitor."
        )
        self._bar_npu.setToolTip(
            "NPU utilisation is shown only when the operating system exposes "
            "a supported telemetry source; otherwise it is marked N/A."
        )

        gauges = QGridLayout()
        gauges.setContentsMargins(0, 2, 0, 2)
        gauges.setHorizontalSpacing(4)
        gauges.setVerticalSpacing(4)
        for column in range(3):
            gauges.setColumnStretch(column, 1)
        for index, gauge in enumerate((
            self._bar_cpu, self._bar_mem, self._bar_gpu,
            self._bar_net, self._bar_disk, self._bar_npu,
            self._bar_tmp,
        )):
            gauges.addWidget(gauge, index // 3, index % 3,
                             alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addLayout(gauges)
        self._net_graph = NetActivityGraph()
        lay.addWidget(self._net_graph)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)

        def _status_tile(title: str, initial: str, accent: str):
            tile = QWidget()
            tile.setObjectName("TelemetryTile")
            tile.setStyleSheet(f"""
                QWidget#TelemetryTile {{
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER};
                    border-radius: 7px;
                }}
            """)
            tile_lay = QVBoxLayout(tile)
            tile_lay.setContentsMargins(9, 6, 9, 6)
            tile_lay.setSpacing(3)
            title_lbl = QLabel(title)
            title_lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            title_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            value_lbl = QLabel(initial)
            value_lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            value_lbl.setStyleSheet(f"color: {accent}; background: transparent; border: none;")
            tile_lay.addWidget(title_lbl)
            body = QHBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(5)
            if title == "WI-FI LINK":
                self._wifi_signal = WifiSignalIndicator()
                body.addWidget(self._wifi_signal)
                value_lbl.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
                value_lbl.setWordWrap(True)
            body.addWidget(value_lbl, stretch=1)
            tile_lay.addLayout(body)
            return tile, value_lbl

        wifi_tile, self._wifi_value = _status_tile("WI-FI LINK", "CHECKING", C.PRI)
        battery_tile, self._battery_value = _status_tile("BATTERY", "CHECKING", C.PRI)
        status_row.addWidget(wifi_tile, stretch=1)
        status_row.addWidget(battery_tile, stretch=1)
        self._wifi_value.setToolTip(
            "Signal bars and quality use the operating system's reported Wi-Fi "
            "signal when available. Other systems show link state only."
        )
        self._battery_value.setToolTip(
            "Battery charge and external power state, when available."
        )
        lay.addLayout(status_row)

        info_panel = QWidget()
        info_panel.setObjectName("SystemInfo")
        info_panel.setStyleSheet(f"""
            QWidget#SystemInfo {{
                background: {C.PANEL2};
                border: 1px solid {C.BORDER};
                border-radius: 7px;
            }}
        """)
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(10, 8, 10, 8)
        ip_lay.setSpacing(5)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Consolas", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WINDOWS", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  /  {os_name}")
        os_lbl.setFont(QFont("Consolas", 8))
        os_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)

        resource_panel = QWidget()
        resource_panel.setObjectName("ResourceSnapshot")
        resource_panel.setStyleSheet(f"""
            QWidget#ResourceSnapshot {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0b2034, stop:1 #071321);
                border: 1px solid #174775;
                border-top: 2px solid {C.PRI};
                border-radius: 8px;
            }}
        """)
        resource_lay = QVBoxLayout(resource_panel)
        resource_lay.setContentsMargins(10, 8, 10, 9)
        resource_lay.setSpacing(7)
        resource_title = QLabel("◈  COMPONENT CAPACITY")
        resource_title.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        resource_title.setStyleSheet(
            f"color: {C.PRI}; background: transparent; border: none;"
        )
        resource_lay.addWidget(resource_title)

        def _capacity_row(title: str, color: str):
            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            labels = QHBoxLayout()
            labels.setContentsMargins(0, 0, 0, 0)
            name = QLabel(title)
            name.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            name.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            reading = QLabel("--")
            reading.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
            reading.setAlignment(Qt.AlignmentFlag.AlignRight)
            reading.setStyleSheet(f"color: {color}; background: transparent;")
            labels.addWidget(name)
            labels.addStretch()
            labels.addWidget(reading)
            meter = QProgressBar()
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setFixedHeight(7)
            meter.setStyleSheet(f"""
                QProgressBar {{
                    background: #06101b;
                    border: 1px solid #173b5a;
                    border-radius: 3px;
                }}
                QProgressBar::chunk {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 {color}, stop:1 #49d6ff);
                    border-radius: 2px;
                }}
            """)
            row.addLayout(labels)
            row.addWidget(meter)
            resource_lay.addLayout(row)
            return meter, reading

        self._ram_capacity_bar, self._ram_capacity_lbl = _capacity_row(
            "MEMORY IN USE", "#35cfff"
        )
        self._storage_capacity_bar, self._storage_capacity_lbl = _capacity_row(
            "SYSTEM DRIVE  /  USED", "#38d8a5"
        )
        self._resource_cpu_lbl = QLabel(
            f"CPU THREADS  {psutil.cpu_count(logical=True) or '--'}"
        )
        self._resource_cpu_lbl.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
        self._resource_cpu_lbl.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        resource_lay.addWidget(self._resource_cpu_lbl)
        lay.addWidget(resource_panel)

        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("CommandPanel")
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(
            f"QWidget#CommandPanel {{ "
            f"background: qlineargradient(x1:1, y1:0, x2:0, y2:1, "
            f"stop:0 #0d2440, stop:0.48 {C.DARK}, stop:1 #071d27); "
            f"border-left: 1px solid {C.BORDER}; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(13, 13, 13, 13)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 3)
        title = QLabel("J.A.R.V.I.S  /  COMMAND DECK")
        title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        top.addWidget(title)
        top.addStretch()
        self._command_state_lbl = QLabel("●  STANDBY")
        self._command_state_lbl.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
        self._command_state_lbl.setStyleSheet(
            f"color: {C.GREEN}; background: #09251f; "
            f"border: 1px solid #17654c; border-radius: 5px; padding: 3px 6px;"
        )
        top.addWidget(self._command_state_lbl)
        lay.addLayout(top)
        top_rule = QFrame()
        top_rule.setFixedHeight(1)
        top_rule.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #168cff, stop:0.7 #174775, stop:1 #17654c); border: none;"
        )
        lay.addWidget(top_rule)

        activity = QHBoxLayout()
        activity.addWidget(QLabel("CONVERSATION  /  LIVE LOG"))
        activity.addStretch()
        activity_lbl = activity.itemAt(0).widget()
        activity_lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        activity_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addLayout(activity)
        self._log = LogWidget()
        self._log.setStyleSheet(f"""
            QTextEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #07182a, stop:0.55 #06111e, stop:1 #091a2b);
                color: {C.TEXT};
                border: 1px solid #1a5887;
                border-radius: 9px;
                padding: 10px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.DARK}; width: 6px; border: none; margin: 2px 1px;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 20px;
            }}
        """)
        self._log.setMinimumHeight(100)
        lay.addWidget(self._log, stretch=1)

        files_hdr = QHBoxLayout()
        files_hdr.addWidget(QLabel("ATTACHMENT  /  DROP OR BROWSE"))
        files_hdr.addStretch()
        self._file_state_lbl = QLabel("READY")
        self._file_state_lbl.setFont(QFont("Consolas", 7))
        self._file_state_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        files_hdr.addWidget(self._file_state_lbl)
        file_title = files_hdr.itemAt(0).widget()
        file_title.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        file_title.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addLayout(files_hdr)
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        self._drop_zone.file_cleared.connect(self._on_file_cleared)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("Drop a file here or click to browse")
        self._file_hint.setFont(QFont("Segoe UI", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._file_hint.setWordWrap(True)
        self._file_hint.setMaximumHeight(28)
        lay.addWidget(self._file_hint)

        command_title = QLabel("MESSAGE J.A.R.V.I.S")
        command_title.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        command_title.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(command_title)
        lay.addLayout(self._build_input_row())

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self._interrupt_btn = QPushButton("■  STOP  ·  ESC")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #1b101a; color: #ff7189;
                border: 1px solid #71304a; border-radius: 6px;
            }}
            QPushButton:hover {{
                background: #351426; border-color: #ff557a;
            }}
            QPushButton:pressed {{ background: #501b32; }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        action_row.addWidget(self._interrupt_btn, stretch=1)

        self._mute_btn = QPushButton("MIC  ·  ACTIVE")
        self._mute_btn.setFixedHeight(34)
        self._mute_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        action_row.addWidget(self._mute_btn, stretch=1)
        lay.addLayout(action_row)

        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #092333, stop:1 #101326);
                color: #69edff;
                border: 1px solid #187c98; border-radius: 7px;
                text-align: left; padding: 0 10px;
            }}
            QPushButton:hover {{
                color: #f5ffff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0c5365, stop:1 #0b2d4d);
                border-color: #36bfff;
            }}
            QPushButton:pressed {{ background: #091824; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #081622, stop:1 #0b101a);
                color: {C.TEXT_MED};
                border: 1px solid #17394d; border-radius: 7px;
                text-align: left; padding: 0 10px;
            }}
            QPushButton:hover {{
                color: #aaf8ff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #103246, stop:1 #10243a);
                border-color: #3aaac5;
            }}
            QPushButton:pressed {{ background: #101a27; }}
        """

        self._BTN_PRI, self._BTN_DIM = _BTN_STYLE_PRI, _BTN_STYLE_DIM
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #071a2b, stop:0.55 #080d18, stop:1 #0b1b30);
                border: 1px solid #22728d;
                border-top: none;
                border-radius: 0 0 10px 10px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ SETUP")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(34)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(30)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(30)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(30)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(30)
        audio_btn.setFont(QFont("Courier New", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(30)
        mem_btn.setFont(QFont("Courier New", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(30)
        plugin_btn.setFont(QFont("Courier New", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = QPushButton("⚙  PLUGIN SETTINGS")
        settings_btn.setFixedHeight(30)
        settings_btn.setFont(QFont("Courier New", 7))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        # The button is built here with the rest of the drawer; the panel behind
        # it — and the whole providers package — is imported on the click. The
        # drawer used to stutter for a measured 2.104 seconds because a state
        # check imported openwakeword while it was being built, and adding a
        # second thing that does that would undo the fix rather than repeat it.
        prov_btn = QPushButton("🔌  ENGINE & PROVIDERS")
        prov_btn.setFixedHeight(30)
        prov_btn.setFont(QFont("Courier New", 7))
        prov_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        prov_btn.setStyleSheet(_BTN_STYLE_DIM)
        prov_btn.clicked.connect(self._open_providers)
        lay.addWidget(prov_btn)

        w.adjustSize()
        return w

    def _build_controls_drawer(self) -> QWidget:
        """The switches that get flicked every day, in their own panel."""
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(self._quick_drawer.styleSheet())
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        def _row(button, style=None, height=30):
            button.setFixedHeight(height)
            button.setFont(QFont("Courier New", 7))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                button.setStyleSheet(style)
            lay.addWidget(button)
            return button

        fs_btn = _row(QPushButton("⛶  FULLSCREEN  [F11]"), self._BTN_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)

        self._brief_btn = _row(QPushButton())
        self._brief_btn.clicked.connect(self._toggle_brief)

        self._wake_btn = _row(QPushButton())
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        self._wake_sleep_btn = _row(QPushButton())
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        # Neutral until the real state lands from the background warm-up.
        self._wake_btn.setText("🎙  WAKE WORD")
        self._wake_btn.setStyleSheet(self._BTN_DIM)
        self._wake_sleep_btn.hide()

        self._ptt_btn = _row(QPushButton())
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        self._refresh_talk_btns()

        self._hud_btn = _row(QPushButton())
        self._hud_btn.clicked.connect(self._toggle_hud_style)
        self._refresh_hud_btn()

        w.adjustSize()
        return w

    def _warm_wake_state(self) -> None:
        """Work out the wake-word state off the UI thread, once.

        It used to be resolved when the drawer opened, and that made opening the
        drawer take two seconds the first time — measured at 2.10s, all of it
        `import openwakeword` dragging in onnxruntime behind it. The button
        count had nothing to do with it; the Qt thread was simply waiting on an
        import. Afterwards the same check costs about a millisecond, so it only
        ever needed to happen somewhere other than in front of the user.
        """
        def work():
            try:
                self._wake_state()
            except Exception:
                pass
            self._wake_btns_sig.emit()
        threading.Thread(target=work, daemon=True, name="wake-state-warm").start()

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._close_controls()
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _toggle_controls(self, checked: bool):
        if checked:
            self._close_setup()
            self._refresh_wake_btns()   # cheap now: the state was warmed at boot
            self._position_ctrl_drawer()
            self._ctrl_drawer.show()
            self._ctrl_drawer.raise_()
        else:
            self._ctrl_drawer.hide()

    def _close_setup(self):
        if hasattr(self, "_quick_drawer"):
            self._quick_drawer.hide()
        if hasattr(self, "_drawer_btn"):
            self._drawer_btn.setChecked(False)

    def _close_controls(self):
        if hasattr(self, "_ctrl_drawer"):
            self._ctrl_drawer.hide()
        if hasattr(self, "_ctrl_btn"):
            self._ctrl_btn.setChecked(False)

    def _place_drawer(self, drawer, left: int):
        _W = 258
        drawer.setFixedWidth(_W)
        drawer.adjustSize()
        drawer.setGeometry(left, _HEADER_H, _W, drawer.sizeHint().height())

    def _position_quick_drawer(self):
        if hasattr(self, "_quick_drawer"):
            self._place_drawer(self._quick_drawer, 12)

    def _position_ctrl_drawer(self):
        # Under its own header button rather than under the other one's.
        if hasattr(self, "_ctrl_drawer"):
            left = 12
            try:
                left = max(12, self._ctrl_btn.mapTo(self.centralWidget(),
                                                    QPoint(0, 0)).x() - 8)
            except Exception:
                pass
            self._place_drawer(self._ctrl_drawer, left)

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(4)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Message J.A.R.V.I.S…")
        self._input.setFont(QFont("Segoe UI", 9))
        self._input.setFixedHeight(42)
        self._input.setMinimumWidth(0)
        self._input.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0b1e2b, stop:1 #070d18);
                color: {C.WHITE};
                border: 1px solid #1b6398; border-radius: 9px;
                padding: 0 13px; selection-background-color: {C.PRI_GHO};
            }}
            QLineEdit:focus {{
                border: 1px solid {C.PRI};
                background: #071722;
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, stretch=1)

        send = QPushButton("↑")
        send.setFixedSize(40, 42)
        send.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                color: {C.BG};
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #54efff, stop:0.45 {C.PRI}, stop:1 #007b9e);
                border: 1px solid #7df5ff; border-radius: 9px;
            }}
            QPushButton:hover {{
                color: #ffffff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {C.PRI}, stop:1 #0874c7);
                border: 1px solid #60cfff;
            }}
            QPushButton:pressed {{ background: {C.PRI_GHO}; }}
        """)
        send.setToolTip("Send message")
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        # The panel opens below the head, so the head looks down at it. It is a
        # tiny thing that answers "did that land?" before you read a word.
        self.hud.glance(0.0, -0.85, hold=1.3)
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: JARVIS says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Courier New;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        self.hud.glance(0.0, -0.85, hold=1.3)
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260, 0])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Courier New", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Courier New", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Courier New", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._hide_quiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Courier New", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Courier New", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        self.hud.glance(0.0, -0.85, hold=1.3)
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Courier New", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("By FatihMakes", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}")
        self._file_hint.setToolTip(str(p))
        self._file_state_lbl.setText("FILE LOADED")
        self._file_state_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _on_file_cleared(self):
        self._current_file = None
        self._file_hint.setText("Drop a file here or click to browse")
        self._file_hint.setToolTip("")
        self._file_state_lbl.setText("READY")
        self._file_state_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        berr   = result[4] if len(result) >= 5 else ""
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw,
                               bind_error=berr)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  SLEEP NOW" if st["awake"] else "👂  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {chord_label()}" if ptt
                              else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")


    def _refresh_hud_btn(self):
        from memory.config_manager import get_hud_style
        face = get_hud_style() == "face"
        # Neither state is "off", so both read as active — this is a choice
        # between two things, not a switch with a disabled side.
        style = f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER_A}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.WHITE}; border: 1px solid {C.BORDER_B}; }}"""
        self._hud_btn.setText("🧑  HUD: ANIMATED FACE" if face
                              else "◉  HUD: REACTOR CORE")
        self._hud_btn.setStyleSheet(style)
        self._hud_btn.setToolTip(
            "An animated head that speaks your words and shows what JARVIS is "
            "doing. Tap to switch to the reactor core."
            if face else
            "A reactor core that turns with the state and moves with your voice. "
            "Tap to switch to the animated head.")

    def _toggle_hud_style(self):
        """Swap the centrepiece. Both objects stay in memory, so the change is
        instant and switching back costs nothing."""
        from memory.config_manager import get_hud_style, save_hud_style
        want = "core" if get_hud_style() == "face" else "face"
        save_hud_style(want)
        try:
            self.hud.hud_style = want
            self.hud.update()
        except Exception:
            pass
        self._refresh_hud_btn()
        self._log.append_log(
            "SYS: HUD switched to the animated face." if want == "face"
            else "SYS: HUD switched to the reactor core.")

    def _toggle_ptt(self):
        from memory.config_manager import (get_push_to_talk_enabled,
                                           save_push_to_talk_enabled)
        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Engine & providers ───────────────────────────────────────────────────

    def _open_providers(self):
        """Open the engine panel. Everything it needs is imported on this call.

        A failure here must not take the drawer with it — the folder can be
        deleted, which is the point of it being a folder, and the honest answer
        to that is a line in the log rather than a traceback.
        """
        try:
            ov = ProvidersOverlay(parent=self.centralWidget())
        except ImportError:
            self._log.append_log(
                "SYS: The providers folder is not installed — "
                "the assistant runs on Gemini Live.")
            return
        except Exception as e:
            self._log.append_log(f"SYS: Provider settings unavailable: {e}")
            return
        ov.changed.connect(self._on_providers_applied)
        self._centre_overlay(ov)
        self._providers_overlay = ov        # keep a reference so it isn't GC'd

    def _on_providers_applied(self):
        """Rebuild the session so the new engine takes over.

        Reuses the audio-device callback rather than adding a second one: both
        mean 'the conversation has to be rebuilt', main.py already keeps the
        transcript across such a rebuild through session resumption, and one
        path that is known to work is better than two that look alike."""
        self._log.append_log("SYS: Engine settings saved — reconnecting.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("MIC  ·  MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #1b0811; color: {C.MUTED_C};
                    border: 1px solid #7a2b42; border-radius: 6px;
                }}
                QPushButton:hover {{ background: #2a0d1c; border-color: {C.MUTED_C}; }}
            """)
        else:
            self._mute_btn.setText("MIC  ·  ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #061c17, stop:1 #09251e);
                    color: {C.GREEN};
                    border: 1px solid #1d7652; border-radius: 6px;
                }}
                QPushButton:hover {{ background: #0c3024; border-color: {C.GREEN}; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        color = (C.GREEN if state in ("LISTENING", "READY", "IDLE")
                 else C.ACC if state == "SPEAKING"
                 else C.ACC2 if state in ("THINKING", "PROCESSING")
                 else C.MUTED_C if state == "MUTED" else C.TEXT_MED)
        if hasattr(self, "_command_state_lbl"):
            self._command_state_lbl.setText(f"●  {state}")
            self._command_state_lbl.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
        if hasattr(self, "_header_link_lbl"):
            self._header_link_lbl.setText(f"● {state}  ·  VOICE CORE")
            self._header_link_lbl.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
        if hasattr(self, "_header_core_status"):
            self._header_core_status.setText(f"VOICE CORE  /  {state}")
            self._header_core_status.setStyleSheet(
                f"color: {color}; background: #081c1b; "
                f"border: 1px solid {color}; border-radius: 4px; padding: 3px 6px;"
            )

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app)
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: post a schedule of (level, openness, width) mouth frames
        for JARVIS's own speech. `at` is the wall-clock time the batch begins to
        sound, not the time of the call. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.push_visemes(frames, hop, at)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the plugin supplies it so
        the marking rules live with the questions rather than being duplicated
        here. Returning None from it means "JARVIS should judge this one", which
        is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the finished
        result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def hide_quiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        self._win._quiz_hide_sig.emit()

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD.

        `findings` is a list of {heading, detail, severity, quote, suggestion};
        severity is one of 'serious' / 'caution' / 'note' and decides colour and
        order here, so the caller supplies no styling of its own.
        """
        self._win._review_sig.emit(str(title or ""), str(summary or ""),
                                   list(findings or []), list(unclear or []))

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def show_video(self, source: str, title: str = "", muted: bool = True,
                   audio_source: str = "") -> None:
        """Thread-safe: play a video where the avatar normally is.

        `source` is a local path or a direct media URL. `audio_source` is for
        the case where the sound arrives as its own stream — YouTube serves no
        combined format any more — and when it is given the two are played
        together and kept in step.

        Muted by default, and that is a decision rather than a default: a
        soundtrack talking over JARVIS is the one way this could make the
        assistant worse. The user turns sound on from the header button or by
        asking, and closes it the same two ways.
        """
        self._win._video_open_sig.emit(str(source or ""), str(title or ""),
                                       bool(muted), str(audio_source or ""))

    def stop_video(self) -> None:
        """Thread-safe: close the video and give the HUD back to the avatar."""
        self._win._video_close_sig.emit()

    def set_video_muted(self, muted: bool) -> None:
        """Thread-safe: silence or unsilence whatever is playing."""
        self._win._video_mute_sig.emit(bool(muted))

    def video_is_playing(self) -> bool:
        return bool(self._win.video_is_playing())

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
