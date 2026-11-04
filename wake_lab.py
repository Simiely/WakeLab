#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WakeLab —— 点亮方式独立测试台（v1.1.0）。

v1.1.0 标注：0x60 切源往返已验证——等待填 0 即可点亮屏
（两台 Legion R27qe Gen2，动作约 185 ms）。

用途：把主程序点亮阶梯里的每一种手段拆成独立按钮，用户点哪个就只执行哪个，
用于在同一台显示器上对比「哪种方式最快最稳」。

铁律：
  1. 每个按钮只执行它自己那一条/那一组明确写出的调用，不自动串联、不重试、
     不做「前一级失败就升级」之类的早退/升级判断。
  2. 启动只做显示器枚举 + 只读能力串，绝不发送任何 DDC 写命令。
  3. 不使用任何鼠标注入或自动化点击。
  4. 每次动作后只读回 0xD6 / 0x10 / 0x60 各一次，用于记录，不参与任何判断。

命令行：
  WakeLab.exe                 启动图形界面
  WakeLab.exe --selftest      离线自检；不打开 Tk、不访问 dxva2，写 selftest.txt
"""
import os
import re
import sys
import time
import queue
import ctypes
import datetime
import threading
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import font as tkfont
from ctypes import wintypes
from time import perf_counter

APP_NAME = "WakeLab"
APP_VERSION = "1.1.0"

# ------------------------------------------------------------
# 配色：沿用主程序 MonitorBrightness 的 Apple 深色近似值
# ------------------------------------------------------------
UI = {
    "bg": "#0f0f11",
    "card": "#1c1c1e",
    "card_alt": "#242426",
    "fill": "#2a2a2d",
    "fill_hover": "#343438",
    "sep": "#2f2f31",
    "text": "#f2f2f4",
    "text2": "#a1a1a6",
    "text3": "#6c6c70",
    "accent": "#0a84ff",
    "accent_hover": "#3d9bff",
    "orange": "#ff9f0a",
    "red": "#ff453a",
    "green": "#30d158",
}

# VCP 特性码与 MCCS 取值
VCP_BRIGHTNESS = 0x10
VCP_INPUT_SOURCE = 0x60
VCP_POWER_MODE = 0xD6

POWER_ON = 1
POWER_STANDBY = 2
POWER_SOFT_OFF = 4
POWER_OFF = 5

SC_MONITORPOWER = 0xF170
SC_OFF = 2
SC_ON = -1
_WM_SYSCOMMAND = 0x0112
_HWND_BROADCAST = 0xFFFF

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002


def _ui_font(size, bold=False):
    return ("Microsoft YaHei UI", size, "bold") if bold else ("Microsoft YaHei UI", size)


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _hname(t):
    return "%s #%d" % (t.get("dev") or "?", int(t.get("index", -1)))


def _hex_values(values):
    return " ".join("%02X" % int(v) for v in values) or "—"


def format_ms(ms):
    return "%.1f" % float(ms)


# ============================================================
# 一、DDC/CI（dxva2）
# ------------------------------------------------------------
# 与主程序同一套 ctypes 声明：HANDLE 必须显式声明，否则 64 位下会被截断。
# ============================================================
class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


_DXVA2_READY = False


def _dxva2():
    global _DXVA2_READY
    lib = ctypes.windll.dxva2
    if _DXVA2_READY:
        return lib
    try:
        lib.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
        lib.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        lib.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR, wintypes.DWORD, ctypes.c_void_p]
        lib.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        lib.GetVCPFeatureAndVCPFeatureReply.argtypes = [
            wintypes.HANDLE, wintypes.BYTE, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        lib.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
        lib.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
        lib.SetVCPFeature.restype = wintypes.BOOL
        lib.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
        lib.DestroyPhysicalMonitor.restype = wintypes.BOOL
        lib.GetCapabilitiesStringLength.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        lib.GetCapabilitiesStringLength.restype = wintypes.BOOL
        lib.CapabilitiesRequestAndCapabilitiesReply.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        lib.CapabilitiesRequestAndCapabilitiesReply.restype = wintypes.BOOL
    except Exception:
        pass
    _DXVA2_READY = True
    return lib


def read_vcp(handle, code):
    """读一个 VCP 特性，返回 (当前值, 最大值)；失败返回 None。"""
    try:
        cur, mx = wintypes.DWORD(), wintypes.DWORD()
        if _dxva2().GetVCPFeatureAndVCPFeatureReply(
                handle, wintypes.BYTE(int(code)), None,
                ctypes.byref(cur), ctypes.byref(mx)):
            return cur.value, mx.value
    except Exception:
        pass
    return None


def write_vcp(handle, code, value):
    """写一个 VCP 特性，返回是否成功。不做任何重试。"""
    try:
        return bool(_dxva2().SetVCPFeature(
            handle, wintypes.BYTE(int(code)), wintypes.DWORD(int(value))))
    except Exception:
        return False


def enumerate_targets():
    """枚举所有支持 DDC/CI 的物理显示器。只读，不写任何 VCP。"""
    if os.name != "nt":
        return []
    hmons = []
    try:
        user32 = ctypes.windll.user32

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]

        def _cb(hmon, hdc, lprect, lparam):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(mi)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                hmons.append((hmon, mi))
            return 1

        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)
        user32.EnumDisplayMonitors(None, None, proc(_cb), None)
    except Exception:
        return []

    targets = []
    lib = _dxva2()
    for hmon, mi in hmons:
        rc = mi.rcMonitor
        base = {"dev": str(mi.szDevice).split("\\")[-1] or str(mi.szDevice),
                "w": rc.right - rc.left, "h": rc.bottom - rc.top,
                "primary": bool(mi.dwFlags & 1)}
        try:
            count = wintypes.DWORD()
            if not lib.GetNumberOfPhysicalMonitorsFromHMONITOR(
                    hmon, ctypes.byref(count)):
                continue
            arr = (PHYSICAL_MONITOR * max(1, count.value))()
            if not lib.GetPhysicalMonitorsFromHMONITOR(
                    hmon, count.value, ctypes.byref(arr)):
                continue
            for p in list(arr)[:count.value]:
                item = dict(base)
                item["handle"] = p.hPhysicalMonitor
                item["desc"] = str(p.szPhysicalMonitorDescription)
                item["index"] = len(targets)
                targets.append(item)
        except Exception:
            continue
    return targets


def release_targets(targets):
    """释放 enumerate_targets() 拿到的物理句柄。"""
    lib = _dxva2()
    for t in targets or []:
        try:
            lib.DestroyPhysicalMonitor(t["handle"])
        except Exception:
            pass


def _capabilities(handle):
    """读 MCCS 能力串（实测约 3 秒/台）。失败返回 ""。只读。"""
    try:
        lib = _dxva2()
        n = wintypes.DWORD()
        if not lib.GetCapabilitiesStringLength(handle, ctypes.byref(n)) or not n.value:
            return ""
        buf = ctypes.create_string_buffer(n.value + 1)
        if not lib.CapabilitiesRequestAndCapabilitiesReply(handle, buf, n.value):
            return ""
        return buf.value.decode("ascii", "replace")
    except Exception:
        return ""


def caps_model(caps):
    m = re.search(r"model\(([^)]*)\)", caps or "", re.I)
    return (m.group(1).strip() if m else "") or None


def caps_power(caps):
    """能力串 D6(...) 声明的电源模式；数值是不带 0x 的十六进制。"""
    m = re.search(r"\bD6\(([^)]*)\)", caps or "", re.I)
    if not m:
        return []
    out = []
    for tok in m.group(1).split():
        try:
            code = int(tok, 16)
        except Exception:
            continue
        if 1 <= code <= 5:
            out.append(code)
    return out


def caps_inputs(caps):
    """能力串 60(...) 声明的输入源；数值是不带 0x 的十六进制。"""
    m = re.search(r"\b60\(([^)]*)\)", caps or "")
    if not m:
        return []
    out = []
    for tok in m.group(1).split():
        try:
            code = int(tok, 16)
        except Exception:
            continue
        if 1 <= code <= 19:
            out.append(code)
    return out


# ============================================================
# 二、系统级调用（不是 DDC）
# ============================================================
_USER32_READY = False


def _user32():
    global _USER32_READY
    lib = ctypes.windll.user32
    if _USER32_READY:
        return lib
    try:
        lib.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        lib.SendMessageTimeoutW.restype = wintypes.LPARAM
    except Exception:
        pass
    _USER32_READY = True
    return lib


def broadcast_monitor_power(flag):
    """向所有窗口广播 SC_MONITORPOWER。系统级，影响全部显示器。

    真实调用：
      SendMessageTimeoutW(0xFFFF, WM_SYSCOMMAND(0x0112),
                          SC_MONITORPOWER(0xF170),
                          wParam=flag, flags=0, timeout=2000, result=None)
    """
    try:
        return bool(_user32().SendMessageTimeoutW(
            _HWND_BROADCAST, _WM_SYSCOMMAND, SC_MONITORPOWER,
            int(flag), 0, 2000, None))
    except Exception:
        return False


def set_thread_execution_state(flags):
    """SetThreadExecutionState 的真实调用；返回 API 的返回值（0 = 失败）。"""
    try:
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes = [wintypes.DWORD]
        fn.restype = wintypes.DWORD
        return int(fn(wintypes.DWORD(int(flags))))
    except Exception:
        return 0


# ============================================================
# 三、按钮清单（同时作为 --selftest 的核对表）
# ============================================================
ACTION_SPECS = (
    {"id": "d6_1", "group": "0xD6 电源", "label": "0xD6 = 1 唤醒",
     "api": "SetVCPFeature(handle, 0xD6, 1)"},
    {"id": "d6_4", "group": "0xD6 电源", "label": "0xD6 = 4 软关",
     "api": "SetVCPFeature(handle, 0xD6, 4)"},
    {"id": "d6_5", "group": "0xD6 电源", "label": "0xD6 = 5 硬关",
     "api": "SetVCPFeature(handle, 0xD6, 5)"},
    {"id": "d6_2_roundtrip", "group": "0xD6 电源", "label": "0xD6 = 2→1",
     "api": ("SetVCPFeature(h, 0xD6, 2) → sleep 800ms → "
             "SetVCPFeature(h, 0xD6, 1)")},
    {"id": "bri_current", "group": "0x10 亮度", "label": "0x10 = 读回当前值",
     "api": ("GetVCPFeatureAndVCPFeatureReply(h, 0x10) → "
             "SetVCPFeature(h, 0x10, 当前值)")},
    {"id": "bri_100", "group": "0x10 亮度", "label": "0x10 = 100",
     "api": "SetVCPFeature(handle, 0x10, 100)"},
    {"id": "sc_on", "group": "系统广播", "label": "SC_MONITORPOWER -1 点亮",
     "api": ("SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, "
             "SC_MONITORPOWER, -1, 0, 2000)")},
    {"id": "recycle", "group": "系统广播", "label": "输出回收 2→800ms→-1",
     "api": ("SendMessageTimeoutW(..., SC_MONITORPOWER, 2, 0, 2000) → "
             "sleep 800ms → SendMessageTimeoutW(..., -1, 0, 2000)")},
    {"id": "input_roundtrip", "group": "0x60 信号源", "label": "0x60 切源往返",
     "api": ("GetVCPFeatureAndVCPFeatureReply(h, 0x60) → "
             "SetVCPFeature(h, 0x60, 17) → sleep(用户输入 ms) → "
             "SetVCPFeature(h, 0x60, 原值)")},
    {"id": "es_hold", "group": "系统输入", "label": "保持显示",
     "api": "SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)"},
    {"id": "es_release", "group": "系统输入", "label": "释放保持",
     "api": "SetThreadExecutionState(ES_CONTINUOUS)"},
)
SPEC_BY_ID = {s["id"]: s for s in ACTION_SPECS}

REQUIRED_IDS = {
    "d6_1", "d6_2_roundtrip", "d6_4", "d6_5",
    "bri_current", "bri_100", "sc_on", "recycle",
    "input_roundtrip", "es_hold",
}


# ============================================================
# 四、纯逻辑：能力串门槛（UI 与 --selftest 共用）
# ============================================================
def gate_d6(monitors, value):
    """判断 0xD6=value 能否对当前勾选的显示器执行。

    返回 (state, reason)：
      empty   未勾选显示器
      loading 能力串还在读
      none    全部未声明（按钮置灰）
      partial 部分声明（按钮可点，未声明的屏跳过）
      ok      全部声明
    """
    if not monitors:
        return "empty", "未勾选显示器"
    if any(m.get("caps_state") == "loading" for m in monitors):
        return "loading", "能力串读取中"
    hits = [m for m in monitors
            if m.get("caps_state") == "ok" and int(value) in (m.get("power") or [])]
    if not hits:
        if all(m.get("caps_state") == "fail" for m in monitors):
            return "none", "能力串读取失败"
        return "none", "能力串未声明 0x%02X" % int(value)
    if len(hits) < len(monitors):
        return "partial", "%d 台未声明，将跳过" % (len(monitors) - len(hits))
    return "ok", ""


def d6_button_text(value, monitors, loading=False):
    """按钮文字 + 置灰原因（未声明/读取中）。"""
    base = {1: "0xD6 = 1 唤醒", 2: "0xD6 = 2→1",
            4: "0xD6 = 4 软关", 5: "0xD6 = 5 硬关"}[int(value)]
    state, _reason = gate_d6(monitors, value)
    if loading and value != 1:
        return base + "（读取中）"
    if value == 1:
        return base
    if state == "none":
        if value == 2:
            return base + "（未声明 02）"
        return base + "（未声明）"
    if state == "partial":
        return base + "（部分跳过）"
    return base


def d6_enabled(value, monitors, loading=False, busy=False):
    if busy or not monitors:
        return False
    if value == 1:
        return True
    if loading:
        return False
    state, _reason = gate_d6(monitors, value)
    return state in ("ok", "partial")


# ============================================================
# 五、图形界面基础件（Apple 深色自绘控件）
# ============================================================
try:
    from PIL import Image, ImageDraw, ImageTk
    PIL_OK = True
except Exception:
    Image = ImageDraw = ImageTk = None
    PIL_OK = False


def _hex_rgba(color):
    c = str(color).lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)


_AA_CACHE = {}
_AA_REFS = []
_TICKBOX_IMAGES = []


def _aa_rounded(w, h, radius, fill, outline=None, ow=0):
    key = ("r", int(w), int(h), int(radius), fill, outline, int(ow))
    got = _AA_CACHE.get(key)
    if got is not None:
        return got
    img = Image.new("RGBA", (max(1, int(w)) * 4, max(1, int(h)) * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, max(1, int(w)) * 4 - 1, max(1, int(h)) * 4 - 1],
                        radius=max(1, int(radius) * 4), fill=_hex_rgba(fill),
                        outline=_hex_rgba(outline) if outline else None,
                        width=int(ow) * 4 if outline else 0)
    img = img.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS)
    ph = ImageTk.PhotoImage(img)
    _AA_CACHE[key] = ph
    _AA_REFS.append(ph)
    return ph


def _tickbox_image(size, fill, border, mark=None, mark_color=None):
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size * 4 - 1, size * 4 - 1],
                        radius=4 * 4, fill=_hex_rgba(fill))
    if mark:
        pts = [(x * 4, y * 4) for x, y in mark]
        d.line(pts, fill=_hex_rgba(mark_color or "#ffffff"),
               width=8, joint="curve")
    out = img.resize((size, size), Image.LANCZOS)
    ph = ImageTk.PhotoImage(out)
    _TICKBOX_IMAGES.append(ph)
    return ph


def install_dark_tickbox(style):
    size = 15
    img_off = _tickbox_image(size, UI["fill"], UI["fill"])
    img_on = _tickbox_image(size, UI["accent"], UI["accent"],
                            mark=((3.5, 7.5), (6.2, 10.8), (11.2, 4.5)),
                            mark_color="#ffffff")
    img_off_dim = _tickbox_image(size, UI["card"], UI["sep"])
    try:
        style.element_create("Dark.tickbox", "image", img_off,
                             ("selected", img_on),
                             ("disabled", img_off_dim))
    except Exception:
        pass
    style.configure("TCheckbutton", background=UI["card"], foreground=UI["text"],
                    indicatormargin=(0, 0, 8, 0), padding=0)
    style.map("TCheckbutton",
              background=[("active", UI["card"])],
              foreground=[("disabled", UI["text3"])])
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Dark.tickbox", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])


def apply_dark_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg=UI["bg"])
    style.configure(".", background=UI["bg"], foreground=UI["text"],
                    fieldbackground=UI["fill"], bordercolor=UI["sep"],
                    font=_ui_font(9))
    style.configure("Vertical.TScrollbar", background=UI["fill"],
                    troughcolor=UI["card"], bordercolor=UI["card"],
                    arrowcolor=UI["text3"], lightcolor=UI["fill"],
                    darkcolor=UI["fill"])
    style.map("Vertical.TScrollbar",
              background=[("active", UI["fill_hover"])])
    install_dark_tickbox(style)


def apply_dark_titlebar(root):
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


class Card(tk.Canvas):
    """圆角卡片容器（Pillow 4x 超采样，与主程序同一套）。"""

    R = 12

    def __init__(self, parent, pad=14, bg=None, page_bg=None):
        bg = bg or UI["card"]
        page = page_bg or parent.cget("bg")
        super().__init__(parent, bg=page, highlightthickness=0, bd=0)
        self._bg = bg
        self._pad = pad
        self.inner = tk.Frame(self, bg=bg)
        self._img = None
        self._win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self._painted = None
        self.inner.bind("<Configure>", lambda e: self._resize())
        self.bind("<Configure>", lambda e: self._paint())

    def _resize(self):
        w = self.inner.winfo_reqwidth() + self._pad * 2
        h = self.inner.winfo_reqheight() + self._pad * 2
        if (w, h) != (self.winfo_width(), self.winfo_height()):
            self.configure(width=w, height=h)

    def _paint(self):
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        if (w, h) == self._painted:
            return
        self._painted = (w, h)
        ph = _aa_rounded(w, h, self.R, self._bg)
        if self._img is None:
            self._img = self.create_image(0, 0, anchor="nw", image=ph)
        else:
            self.itemconfig(self._img, image=ph)
        self.tag_lower(self._img)
        self.itemconfig(self._win, width=max(1, int(w) - self._pad * 2))


class Button(tk.Canvas):
    """自绘按钮（圆角、hover、置灰）。"""

    def __init__(self, parent, text, command=None, kind="secondary",
                 width=None, height=34, radius=9, pad_bg=None, font_size=9):
        self._kind = kind
        self._text = text
        self._command = command
        self._radius = radius
        self._h = height
        self._fs = font_size
        self._bg = pad_bg or parent.cget("bg")
        self._font = _ui_font(font_size, bold=(kind == "primary"))
        self._enabled = True
        self._hover = False
        fm = tkfont.Font(font=self._font)
        w = width or (fm.measure(text) + 30)
        super().__init__(parent, width=w, height=height, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _colors(self):
        k = self._kind
        if not self._enabled:
            return UI["fill"], UI["text3"], ""
        if k == "primary":
            return (UI["accent_hover"] if self._hover else UI["accent"], "#ffffff", "")
        if k == "ghost":
            return (UI["fill"] if self._hover else UI["card"], UI["text2"], UI["sep"])
        if k == "link":
            return (self._bg, UI["accent"], "")
        return (UI["fill_hover"] if self._hover else UI["fill"], UI["text"], "")

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        fill, fg, outline = self._colors()
        if self._kind == "link":
            self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)
            return
        ph = _aa_rounded(w, h, self._radius, fill, outline or None,
                         1 if outline else 0)
        self.create_image(0, 0, anchor="nw", image=ph)
        self.create_text(w / 2, h / 2 + 0.5, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, _e):
        if not self._enabled or not getattr(self, "_pressed", False):
            return
        self._pressed = False
        if self._command:
            self._command()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._draw()

    def set_kind(self, kind):
        self._kind = kind
        self._draw()


class Label(tk.Label):
    def __init__(self, parent, text="", size=9, color=None, bold=False,
                 bg=None, anchor="w", **kw):
        super().__init__(parent, text=text, bg=bg or parent.cget("bg"),
                         fg=color or UI["text"], font=_ui_font(size, bold),
                         anchor=anchor, **kw)


class ScrollArea(ttk.Frame):
    """可滚动内容容器（窗口高度封顶，超出才出现滚动条）。"""

    def __init__(self, root, bg):
        super().__init__(root)
        self._max_h = max(400, self.winfo_screenheight() - 190)
        self.canvas = tk.Canvas(self, background=bg, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, background=bg)
        self._inner = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>",
                         lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>",
                         lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._inner, width=event.width)

    def _on_body(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        need = self.body.winfo_reqheight() > self._max_h
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()
        self.canvas.configure(
            width=self.body.winfo_reqwidth() + (16 if need else 0),
            height=min(self.body.winfo_reqheight(), self._max_h))

    def refresh(self):
        self.body.update_idletasks()
        self._on_body()


# ============================================================
# 六、应用
# ============================================================
class WakeLabApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self._closing = False
        self._busy = False
        self._refit_pending = False
        self._caps_loading = True
        self._caps_token = 0
        self.monitors = []
        self.mon_vars = {}
        self.mon_checks = {}
        self.mon_titles = {}
        self.mon_subs = {}
        self.buttons = {}
        self.link_buttons = []
        self.power_by_index = {}

        root.title("%s v%s · 点亮方式测试台" % (APP_NAME, APP_VERSION))
        root.resizable(False, False)
        apply_dark_theme(root)
        self._build_ui()
        root.report_callback_exception = self._on_tk_error
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._append_log([
            "[%s] %s v%s 启动 · 未发送任何 DDC 写命令" % (_ts(), APP_NAME, APP_VERSION),
            "        本程序只在点击按钮时发命令；不串联、不重试、不做早退判断。",
        ])
        self.refresh_monitors(initial=True)
        self.root.after(120, self._poll_queue)
        self.root.after(150, self._center)

    # ---------- 布局 ----------
    def _build_ui(self):
        self.scroll = ScrollArea(self.root, UI["bg"])
        self.scroll.pack(side="top", fill="both", expand=True)
        body = self.scroll.body

        head = tk.Frame(body, bg=UI["bg"])
        head.pack(fill="x", padx=20, pady=(9, 0))
        Label(head, "点亮方式测试台", size=15, bold=True).pack(side="left")
        Label(head, "v%s · 点哪个只跑哪个" % APP_VERSION, size=8,
              color=UI["text3"]).pack(side="right", anchor="e")
        Label(body, "不串联、不重试、不自动判断；启动不发送任何 DDC 写。",
              size=8, color=UI["text3"]).pack(anchor="w", padx=20, pady=(1, 0))
        Label(body, "实测：0x60 切源往返 + 等待 0 ms，两台 Legion R27qe Gen2 约 185 ms 点亮。",
              size=8, color=UI["green"]).pack(anchor="w", padx=20, pady=(1, 0))
        self.status_lbl = Label(
            body, "就绪 · 尚未发送任何 DDC 命令", size=8, color=UI["text3"],
            wraplength=600, justify="left")
        self.status_lbl.pack(anchor="w", padx=20, pady=(2, 0))

        # 目标显示器
        mhead = tk.Frame(body, bg=UI["bg"])
        mhead.pack(fill="x", padx=20, pady=(9, 4))
        Label(mhead, "目标显示器", size=8, color=UI["text3"], bold=True).pack(side="left")
        for text, cmd in (("刷新", self.refresh_monitors),
                          ("全不选", self.select_none),
                          ("全选", self.select_all)):
            b = Button(mhead, text, cmd, kind="link", height=18,
                       pad_bg=UI["bg"], font_size=8)
            b.pack(side="right", padx=(6, 0))
            self.link_buttons.append(b)
        mcard = Card(body)
        mcard.pack(fill="x", padx=14)
        self.mon_inner = mcard.inner

        # 独立动作
        ahead = tk.Frame(body, bg=UI["bg"])
        ahead.pack(fill="x", padx=20, pady=(9, 4))
        Label(ahead, "独立动作", size=8, color=UI["text3"], bold=True).pack(side="left")
        Label(ahead, "每个按钮只是那一条命令", size=8,
              color=UI["text3"]).pack(side="right")
        acard = Card(body)
        acard.pack(fill="x", padx=14)
        inner = acard.inner

        _first = [True]

        def group_title(text, color=None):
            Label(inner, text, size=8, color=color or UI["text3"], bold=True,
                  bg=UI["card"]).pack(anchor="w", pady=(0 if _first[0] else 7, 2))
            _first[0] = False

        # 0xD6
        group_title("0xD6 电源模式（4/5 可能直接黑屏，恢复点 =1）", UI["orange"])
        row = tk.Frame(inner, bg=UI["card"])
        row.pack(fill="x")
        for kind, value in (("d6_1", 1), ("d6_4", 4), ("d6_5", 5), ("d6_2_roundtrip", 2)):
            b = Button(row, d6_button_text(value, [], False),
                       lambda k=kind: self.on_action(k),
                       width=147, height=32, pad_bg=UI["card"], font_size=9)
            b.pack(side="left", padx=(0, 4))
            self.buttons[kind] = b
        self.d6_summary = Label(inner, "能力串读取中…", size=8, color=UI["text3"],
                                bg=UI["card"], justify="left", wraplength=590)
        self.d6_summary.pack(anchor="w", pady=(2, 0))

        # 0x10
        group_title("0x10 亮度")
        row = tk.Frame(inner, bg=UI["card"])
        row.pack(fill="x")
        b = Button(row, "0x10 = 读回当前值", lambda: self.on_action("bri_current"),
                   width=200, height=32, pad_bg=UI["card"])
        b.pack(side="left", padx=(0, 6))
        self.buttons["bri_current"] = b
        b = Button(row, "0x10 = 100", lambda: self.on_action("bri_100"),
                   width=160, height=32, pad_bg=UI["card"])
        b.pack(side="left")
        self.buttons["bri_100"] = b

        # 系统广播
        group_title("系统广播（影响全部显示器，含未勾选）", UI["orange"])
        row = tk.Frame(inner, bg=UI["card"])
        row.pack(fill="x")
        b = Button(row, "SC_MONITORPOWER -1 点亮", lambda: self.on_action("sc_on"),
                   width=250, height=32, pad_bg=UI["card"])
        b.pack(side="left", padx=(0, 6))
        self.buttons["sc_on"] = b
        b = Button(row, "输出回收 2→800ms→-1", lambda: self.on_action("recycle"),
                   width=250, height=32, pad_bg=UI["card"])
        b.pack(side="left")
        self.buttons["recycle"] = b

        # 0x60
        group_title("0x60 切源往返（写 17 → 等待 → 写回原值）")
        row = tk.Frame(inner, bg=UI["card"])
        row.pack(fill="x")
        b = Button(row, "当前源→17→等待→回原源", lambda: self.on_action("input_roundtrip"),
                   width=230, height=32, pad_bg=UI["card"])
        b.pack(side="left", padx=(0, 8))
        self.buttons["input_roundtrip"] = b
        Label(row, "等待", size=8, color=UI["text2"], bg=UI["card"]).pack(side="left")
        self.wait_entry = tk.Entry(
            row, width=7, bg=UI["fill"], fg=UI["text"], insertbackground=UI["text"],
            relief="flat", bd=0, justify="center", font=_ui_font(9),
            highlightthickness=1, highlightbackground=UI["sep"],
            highlightcolor=UI["accent"])
        self.wait_entry.insert(0, "0")
        self.wait_entry.pack(side="left", padx=(6, 4), ipady=3)
        Label(row, "ms", size=8, color=UI["text2"], bg=UI["card"]).pack(side="left")
        Label(row, "✓ 已验证 · 0 等待可点亮", size=8, bold=True,
              color=UI["green"], bg=UI["card"]).pack(side="left", padx=(10, 0))
        Label(inner, "实测：等待必须填 0。两台 Legion R27qe Gen2 上，0 等待、"
                     "动作约 185 ms 即可点亮；读回源值回到原值 15。",
              size=8, color=UI["text2"], bg=UI["card"],
              justify="left", wraplength=590).pack(anchor="w", pady=(3, 0))

        # 系统输入
        group_title("系统输入")
        row = tk.Frame(inner, bg=UI["card"])
        row.pack(fill="x")
        b = Button(row, "保持显示 ES_DISPLAY_REQUIRED", lambda: self.on_action("es_hold"),
                   width=260, height=32, pad_bg=UI["card"])
        b.pack(side="left", padx=(0, 6))
        self.buttons["es_hold"] = b
        b = Button(row, "释放保持", lambda: self.on_action("es_release"),
                   width=100, height=32, kind="ghost", pad_bg=UI["card"])
        b.pack(side="left")
        self.buttons["es_release"] = b

        # 本次结果
        rhead = tk.Frame(body, bg=UI["bg"])
        rhead.pack(fill="x", padx=20, pady=(9, 4))
        Label(rhead, "本次结果", size=8, color=UI["text3"], bold=True).pack(side="left")
        rcard = Card(body)
        rcard.pack(fill="x", padx=14)
        self.big_lbl = Label(rcard.inner, "尚未执行", size=12, bold=True, bg=UI["card"])
        self.big_lbl.pack(anchor="w")
        self.detail_lbl = Label(
            rcard.inner, "耗时包含动作本身 + 每台 3 次读回（0xD6 / 0x10 / 0x60，约 58 ms/次）。",
            size=8, color=UI["text2"], bg=UI["card"], justify="left", wraplength=590)
        self.detail_lbl.pack(anchor="w", pady=(3, 0))

        # 日志
        lhead = tk.Frame(body, bg=UI["bg"])
        lhead.pack(fill="x", padx=20, pady=(9, 4))
        Label(lhead, "日志", size=8, color=UI["text3"], bold=True).pack(side="left")
        Label(lhead, "可一键复制", size=8, color=UI["text3"]).pack(side="right")
        lcard = Card(body)
        lcard.pack(fill="x", padx=14)
        box = tk.Frame(lcard.inner, bg=UI["card"])
        box.pack(fill="x")
        self.log_text = tk.Text(
            box, height=7, width=60, bg=UI["card_alt"], fg=UI["text2"],
            insertbackground=UI["text"], relief="flat", bd=0,
            font=("Consolas", 9), wrap="word", padx=10, pady=8,
            highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(6, 0))
        self.log_text.configure(state="disabled")
        brow = tk.Frame(lcard.inner, bg=UI["card"])
        brow.pack(fill="x", pady=(8, 0))
        self.copy_btn = Button(brow, "复制日志", self.copy_log, kind="primary",
                               width=110, height=32, pad_bg=UI["card"])
        self.copy_btn.pack(side="left")
        Button(brow, "清空", self.clear_log, kind="ghost", width=80,
               height=32, pad_bg=UI["card"]).pack(side="left", padx=(6, 0))
        Label(brow, "复制后可直接粘贴给我", size=8,
              color=UI["text3"]).pack(side="right")

        tk.Frame(body, bg=UI["bg"], height=10).pack(fill="x")

    # ---------- 窗口 ----------
    def _center(self):
        try:
            self.root.update_idletasks()
            w = max(self.root.winfo_width(), self.root.winfo_reqwidth())
            h = max(self.root.winfo_height(), self.root.winfo_reqheight())
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2 - 30)
            self.root.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _refit(self):
        if self._refit_pending:
            return
        self._refit_pending = True
        self.root.after(60, self._refit_now)

    def _refit_now(self):
        self._refit_pending = False
        try:
            self.scroll.refresh()
            self.root.geometry("")
        except Exception:
            pass

    # ---------- 显示器 ----------
    def selected_indices(self):
        return sorted(i for i, v in self.mon_vars.items() if v.get())

    def selected_monitors(self):
        sel = set(self.selected_indices())
        return [m for m in self.monitors if m["index"] in sel]

    def select_all(self):
        for v in self.mon_vars.values():
            v.set(True)

    def select_none(self):
        for v in self.mon_vars.values():
            v.set(False)

    def _old_selected_devs(self):
        out = set()
        for i, v in self.mon_vars.items():
            if v.get() and i < len(self.monitors):
                out.add(self.monitors[i]["dev"])
        return out

    def refresh_monitors(self, initial=False):
        if self._busy:
            return
        old_devs = None if initial else self._old_selected_devs()
        targets = enumerate_targets()
        try:
            self.monitors = [{
                "index": t["index"], "dev": t["dev"], "desc": t["desc"],
                "w": t["w"], "h": t["h"], "primary": t["primary"],
                "caps_state": "loading", "caps": "", "power": [], "inputs": [],
                "model": "",
            } for t in targets]
        finally:
            release_targets(targets)
        self._caps_token += 1
        self._caps_loading = bool(self.monitors)
        self.power_by_index = {}
        self._rebuild_monitor_rows(old_devs)
        self._update_d6_summary()
        self._refresh_buttons()
        self._append_log([
            "",
            "[%s] 刷新：枚举到 %d 台显示器%s" % (
                _ts(), len(self.monitors),
                "；能力串后台只读读取中（约 3 秒/台）" if self.monitors else ""),
        ])
        if self.monitors:
            self._start_caps_load(self._caps_token)
        self._refit()

    def _rebuild_monitor_rows(self, old_devs):
        for w in self.mon_inner.winfo_children():
            w.destroy()
        self.mon_vars, self.mon_checks = {}, {}
        self.mon_titles, self.mon_subs = {}, {}
        if not self.monitors:
            Label(self.mon_inner, "（未检测到支持 DDC/CI 的显示器）",
                  size=8, color=UI["text3"], bg=UI["card"]).pack(anchor="w", pady=4)
            return
        for m in self.monitors:
            i = m["index"]
            row = tk.Frame(self.mon_inner, bg=UI["card"])
            row.pack(fill="x")
            var = tk.BooleanVar(value=(old_devs is None or m["dev"] in old_devs))
            var.trace_add("write", lambda *_: self._refresh_buttons())
            self.mon_vars[i] = var
            chk = ttk.Checkbutton(row, variable=var, text="")
            chk.pack(side="left", padx=(0, 8), pady=4)
            if self._busy:
                chk.configure(state="disabled")
            self.mon_checks[i] = chk
            txt = tk.Frame(row, bg=UI["card"])
            txt.pack(side="left", fill="x", expand=True)
            title = Label(txt, self._monitor_title(m), size=9, bg=UI["card"])
            title.pack(anchor="w")
            sub = Label(txt, self._monitor_sub(m), size=8, color=UI["text3"],
                        bg=UI["card"])
            sub.pack(anchor="w")
            self.mon_titles[i], self.mon_subs[i] = title, sub

    def _monitor_title(self, m):
        model = m.get("model") or m.get("desc") or "未识别型号"
        return "显示器 %d · %s%s · %d×%d" % (
            m["index"], model, " · 主屏" if m.get("primary") else "",
            m.get("w") or 0, m.get("h") or 0)

    def _monitor_sub(self, m):
        if m.get("caps_state") == "loading":
            cap = "能力串读取中…"
        elif m.get("caps_state") == "fail":
            cap = "能力串读取失败"
        else:
            cap = "0xD6: %s · 0x60: %s" % (
                _hex_values(m.get("power") or []), _hex_values(m.get("inputs") or []))
        return "%s · %s" % (m["dev"], cap)

    def _update_monitor_row(self, m):
        i = m["index"]
        if i in self.mon_titles:
            self.mon_titles[i].config(text=self._monitor_title(m))
        if i in self.mon_subs:
            self.mon_subs[i].config(text=self._monitor_sub(m))

    def _start_caps_load(self, token):
        threading.Thread(target=self._caps_worker, args=(token,), daemon=True).start()

    def _caps_worker(self, token):
        targets = []
        try:
            targets = enumerate_targets()
            for t in targets:
                if token != self._caps_token:
                    return
                caps = _capabilities(t["handle"])
                idx = t["index"]
                if idx >= len(self.monitors) or self.monitors[idx]["dev"] != t["dev"]:
                    idx = next((m["index"] for m in self.monitors
                                if m["dev"] == t["dev"]), -1)
                if idx >= 0:
                    self.q.put(("caps", token, idx, t["dev"], caps))
        finally:
            release_targets(targets)
            self.q.put(("caps_done", token))

    def _apply_caps(self, idx, caps):
        if idx < 0 or idx >= len(self.monitors):
            return
        m = self.monitors[idx]
        m["caps"] = caps or ""
        m["model"] = caps_model(caps) or ""
        m["power"] = caps_power(caps)
        m["inputs"] = caps_inputs(caps)
        m["caps_state"] = "ok" if caps else "fail"
        self.power_by_index[idx] = list(m["power"])
        self._update_monitor_row(m)
        self._update_d6_summary()

    def _caps_done(self, token):
        if token != self._caps_token:
            return
        self._caps_loading = False
        self._update_d6_summary()
        self._refresh_buttons()
        parts = []
        for m in self.monitors:
            if m["caps_state"] == "ok":
                parts.append("%s D6(%s) 60(%s)" % (
                    m["dev"], _hex_values(m["power"]), _hex_values(m["inputs"])))
            else:
                parts.append("%s 能力串读取失败" % m["dev"])
        self._append_log(["[%s] 能力串读取完成：%s" % (_ts(), "；".join(parts) or "无显示器")])
        if not self._busy:
            self.set_status("能力串读取完成 · 0xD6=4/5/2 按声明启用/置灰")
        self._refit()

    def _update_d6_summary(self):
        if not hasattr(self, "d6_summary"):
            return
        if not self.monitors:
            text = "未检测到支持 DDC/CI 的显示器。"
        elif self._caps_loading:
            text = "能力串读取中（只读，约 3 秒/台）；0xD6=4/5/2 暂不可点。"
        else:
            parts = []
            for m in self.monitors:
                if m["caps_state"] == "ok":
                    parts.append("%s %s" % (m["dev"], _hex_values(m["power"])))
                else:
                    parts.append("%s 读取失败" % m["dev"])
            text = "D6 声明：" + "；".join(parts)
        self.d6_summary.config(text=text)

    # ---------- 按钮状态 ----------
    def _refresh_buttons(self):
        if not self.buttons:
            return
        sel = self.selected_monitors()
        busy = self._busy
        loading = self._caps_loading
        has_sel = bool(sel)
        ready = not loading

        self.buttons["d6_1"].set_text(d6_button_text(1, sel, loading))
        self.buttons["d6_1"].set_enabled(has_sel and not busy and ready)
        for value, kind in ((4, "d6_4"), (5, "d6_5"), (2, "d6_2_roundtrip")):
            self.buttons[kind].set_text(d6_button_text(value, sel, loading))
            self.buttons[kind].set_enabled(
                d6_enabled(value, sel, loading=loading, busy=busy))

        for kind in ("bri_current", "bri_100", "sc_on", "recycle",
                     "input_roundtrip", "es_hold"):
            self.buttons[kind].set_enabled(has_sel and not busy and ready)
        self.buttons["es_release"].set_enabled(not busy and ready)
        self.copy_btn.set_enabled(not busy)
        for b in self.link_buttons:
            b.set_enabled(not busy)
        try:
            self.wait_entry.configure(state="disabled" if busy else "normal")
        except Exception:
            pass

    def _set_monitor_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        for chk in self.mon_checks.values():
            try:
                chk.configure(state=state)
            except Exception:
                pass

    # ---------- 状态 / 日志 ----------
    def set_status(self, text, bad=False):
        try:
            self.status_lbl.config(text=text, fg=UI["orange"] if bad else UI["text3"])
        except Exception:
            pass

    def _append_log(self, lines):
        if not hasattr(self, "log_text"):
            return
        try:
            self.log_text.configure(state="normal")
            for line in lines:
                self.log_text.insert("end", str(line) + "\n")
            self.log_text.configure(state="disabled")
            self.log_text.see("end")
        except Exception:
            pass

    def copy_log(self):
        try:
            text = self.log_text.get("1.0", "end-1c")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            lines = len([x for x in text.splitlines() if x.strip()])
            self.set_status("日志已复制到剪贴板（%d 行，%d 字符）" % (lines, len(text)))
        except Exception as e:
            self.set_status("复制失败：%s: %s" % (type(e).__name__, e), bad=True)

    def clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self._append_log(["[%s] 日志已清空" % _ts()])
            self.set_status("日志已清空")
        except Exception:
            pass

    # ---------- 动作 ----------
    def _target_text(self, sel):
        if not sel:
            return "（未勾选，无读回）"
        by_index = {m["index"]: m for m in self.monitors}
        return ", ".join(
            "%s #%d" % (by_index[i]["dev"] if i in by_index else "?", i)
            for i in sel)

    def _read_wait_ms(self):
        try:
            v = int(str(self.wait_entry.get()).strip())
        except Exception:
            return None
        return v if 0 <= v <= 10000 else None

    def on_action(self, kind):
        if self._busy or kind not in SPEC_BY_ID:
            return
        sel = self.selected_indices()
        if kind != "es_release" and not sel:
            self.set_status("请先勾选至少一台显示器（读回需要目标）", bad=True)
            return
        wait_ms = 0
        if kind == "input_roundtrip":
            wait_ms = self._read_wait_ms()
            if wait_ms is None:
                self.set_status("等待时长必须是 0–10000 的整数（ms）", bad=True)
                return

        spec = SPEC_BY_ID[kind]
        self._busy = True
        self._set_monitor_controls(False)
        self._refresh_buttons()
        self.set_status("正在执行：%s（只做这一项，不重试）…" % spec["label"])
        self._append_log([
            "",
            "[%s] 动作：%s" % (_ts(), spec["label"]),
            "        API：%s" % spec["api"],
            "        目标：%s" % self._target_text(sel),
        ])

        power = {m["index"]: list(m["power"]) for m in self.monitors}
        caps_state = {m["index"]: m["caps_state"] for m in self.monitors}
        if kind in ("es_hold", "es_release"):
            # SetThreadExecutionState 有线程语义，必须在界面线程调用。
            t0 = perf_counter()
            try:
                lines = self._execute_action(kind, [], wait_ms, power, caps_state)
            except Exception as e:
                lines = ["执行异常：%s: %s" % (type(e).__name__, e)]
            act_ms = (perf_counter() - t0) * 1000.0
            threading.Thread(
                target=self._readback_worker,
                args=(kind, sel, lines, act_ms, t0), daemon=True).start()
            return

        threading.Thread(
            target=self._action_worker,
            args=(kind, sel, wait_ms, power, caps_state), daemon=True).start()

    def _execute_action(self, kind, picked, wait_ms, power, caps_state):
        """执行且只执行一个动作；返回逐条可读日志。绝不重试、绝不串联。

        picked：本轮要操作的显示器（已按勾选过滤）。
        power / caps_state：启动时读到的能力串快照，用于 0xD6=4/5/2 的逐台跳过。
        """
        lines = []

        def declared(t):
            idx = t["index"]
            if caps_state.get(idx) != "ok":
                return []
            return power.get(idx, [])

        if kind == "d6_1":
            for t in picked:
                t0 = perf_counter()
                ok = write_vcp(t["handle"], VCP_POWER_MODE, POWER_ON)
                lines.append("%s 写 0xD6=1 %s（%s ms）" % (
                    _hname(t), "成功" if ok else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind in ("d6_4", "d6_5"):
            value = POWER_SOFT_OFF if kind == "d6_4" else POWER_OFF
            for t in picked:
                if value not in declared(t):
                    lines.append("%s 跳过：能力串未声明 0x%02X" % (_hname(t), value))
                    continue
                t0 = perf_counter()
                ok = write_vcp(t["handle"], VCP_POWER_MODE, value)
                lines.append("%s 写 0xD6=%d %s（%s ms）" % (
                    _hname(t), value, "成功" if ok else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind == "d6_2_roundtrip":
            for t in picked:
                if POWER_STANDBY not in declared(t):
                    lines.append("%s 跳过：能力串未声明 0x02（待机）" % _hname(t))
                    continue
                t0 = perf_counter()
                ok2 = write_vcp(t["handle"], VCP_POWER_MODE, POWER_STANDBY)
                lines.append("%s 写 0xD6=2 %s（%s ms）" % (
                    _hname(t), "成功" if ok2 else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
                time.sleep(0.8)
                lines.append("%s 等待 800 ms" % _hname(t))
                t1 = perf_counter()
                ok1 = write_vcp(t["handle"], VCP_POWER_MODE, POWER_ON)
                lines.append("%s 写 0xD6=1 %s（%s ms）" % (
                    _hname(t), "成功" if ok1 else "失败",
                    format_ms((perf_counter() - t1) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind == "bri_current":
            for t in picked:
                got = read_vcp(t["handle"], VCP_BRIGHTNESS)
                if not got:
                    lines.append("%s 读 0x10 失败，未写亮度" % _hname(t))
                    continue
                cur = got[0]
                t0 = perf_counter()
                ok = write_vcp(t["handle"], VCP_BRIGHTNESS, cur)
                lines.append("%s 读 0x10=%d → 写回 %s（%s ms）" % (
                    _hname(t), cur, "成功" if ok else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind == "bri_100":
            for t in picked:
                t0 = perf_counter()
                ok = write_vcp(t["handle"], VCP_BRIGHTNESS, 100)
                lines.append("%s 写 0x10=100 %s（%s ms）" % (
                    _hname(t), "成功" if ok else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind == "sc_on":
            t0 = perf_counter()
            ok = broadcast_monitor_power(SC_ON)
            lines.append("系统广播 SC_MONITORPOWER -1 %s（%s ms，影响全部显示器）" % (
                "成功" if ok else "失败",
                format_ms((perf_counter() - t0) * 1000)))

        elif kind == "recycle":
            t0 = perf_counter()
            ok1 = broadcast_monitor_power(SC_OFF)
            lines.append("系统广播 SC_MONITORPOWER 2 %s（%s ms，影响全部显示器）" % (
                "成功" if ok1 else "失败",
                format_ms((perf_counter() - t0) * 1000)))
            time.sleep(0.8)
            lines.append("等待 800 ms")
            t1 = perf_counter()
            ok2 = broadcast_monitor_power(SC_ON)
            lines.append("系统广播 SC_MONITORPOWER -1 %s（%s ms）" % (
                "成功" if ok2 else "失败",
                format_ms((perf_counter() - t1) * 1000)))

        elif kind == "input_roundtrip":
            for t in picked:
                got = read_vcp(t["handle"], VCP_INPUT_SOURCE)
                if not got:
                    lines.append("%s 读 0x60 失败，未切源" % _hname(t))
                    continue
                cur = got[0]
                if cur == 17:
                    lines.append("%s 当前已是 17，仍按定义写两次 17" % _hname(t))
                t0 = perf_counter()
                ok1 = write_vcp(t["handle"], VCP_INPUT_SOURCE, 17)
                lines.append("%s 写 0x60=17 %s（%s ms）" % (
                    _hname(t), "成功" if ok1 else "失败",
                    format_ms((perf_counter() - t0) * 1000)))
                time.sleep(wait_ms / 1000.0)
                lines.append("%s 等待 %d ms" % (_hname(t), wait_ms))
                t1 = perf_counter()
                ok2 = write_vcp(t["handle"], VCP_INPUT_SOURCE, cur)
                lines.append("%s 写回 0x60=%d %s（%s ms）" % (
                    _hname(t), cur, "成功" if ok2 else "失败",
                    format_ms((perf_counter() - t1) * 1000)))
            if not picked:
                lines.append("没有可执行目标")

        elif kind == "es_hold":
            ret = set_thread_execution_state(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
            lines.append("SetThreadExecutionState(0x%08X) → 0x%08X %s" % (
                ES_CONTINUOUS | ES_DISPLAY_REQUIRED, ret,
                "成功" if ret else "失败"))

        elif kind == "es_release":
            ret = set_thread_execution_state(ES_CONTINUOUS)
            lines.append("SetThreadExecutionState(0x%08X) → 0x%08X %s" % (
                ES_CONTINUOUS, ret, "成功" if ret else "失败"))

        else:
            raise ValueError("未知动作：%s" % kind)
        return lines

    def _readback(self, picked):
        """动作后读回 0xD6 / 0x10 / 0x60 各一次。只记录，不参与判断。"""
        t0 = perf_counter()
        lines = []
        for t in picked:
            t1 = perf_counter()
            d6 = read_vcp(t["handle"], VCP_POWER_MODE)
            br = read_vcp(t["handle"], VCP_BRIGHTNESS)
            src = read_vcp(t["handle"], VCP_INPUT_SOURCE)
            vals = [
                "0xD6=%s" % (d6[0] if d6 else "读失败"),
                "0x10=%s" % (br[0] if br else "读失败"),
                "0x60=%s" % (src[0] if src else "读失败"),
            ]
            lines.append("%s 读回 %s（%s ms）" % (
                _hname(t), " ".join(vals),
                format_ms((perf_counter() - t1) * 1000)))
        return lines, (perf_counter() - t0) * 1000.0

    def _action_worker(self, kind, sel, wait_ms, power, caps_state):
        targets = []
        payload = None
        t0 = perf_counter()
        try:
            targets = enumerate_targets()
            known = {t["index"] for t in targets}
            picked = [t for t in targets if t["index"] in set(sel)]
            lines = ["序号 %d 未找到（显示器可能已断开）" % i for i in sel if i not in known]
            lines += self._execute_action(kind, picked, wait_ms, power, caps_state)
            act_ms = (perf_counter() - t0) * 1000.0
            rb_lines, rb_ms = self._readback(picked)
            payload = {"kind": kind, "action_lines": lines, "act_ms": act_ms,
                       "readback_lines": rb_lines, "rb_ms": rb_ms,
                       "total_ms": (perf_counter() - t0) * 1000.0,
                       "count": len(picked)}
        except Exception as e:
            payload = {"kind": kind,
                       "action_lines": ["执行异常：%s: %s" % (type(e).__name__, e)],
                       "act_ms": 0.0, "readback_lines": [], "rb_ms": 0.0,
                       "total_ms": (perf_counter() - t0) * 1000.0, "count": 0}
        finally:
            release_targets(targets)
            self.q.put(("action_done", payload))

    def _readback_worker(self, kind, sel, action_lines, act_ms, t0):
        targets = []
        try:
            targets = enumerate_targets()
            known = {t["index"] for t in targets}
            picked = [t for t in targets if t["index"] in set(sel)]
            lines = list(action_lines)
            lines += ["序号 %d 未找到（显示器可能已断开）" % i for i in sel if i not in known]
            rb_lines, rb_ms = self._readback(picked)
            payload = {"kind": kind, "action_lines": lines, "act_ms": act_ms,
                       "readback_lines": rb_lines, "rb_ms": rb_ms,
                       "total_ms": (perf_counter() - t0) * 1000.0,
                       "count": len(picked)}
        except Exception as e:
            payload = {"kind": kind, "action_lines": list(action_lines) + [
                "读回异常：%s: %s" % (type(e).__name__, e)],
                "act_ms": act_ms, "readback_lines": [], "rb_ms": 0.0,
                "total_ms": (perf_counter() - t0) * 1000.0, "count": 0}
        finally:
            release_targets(targets)
            self.q.put(("action_done", payload))

    def _finish_action(self, payload):
        self._busy = False
        self._set_monitor_controls(True)
        self._refresh_buttons()
        if not payload:
            self.set_status("执行结束，但没有结果", bad=True)
            return
        spec = SPEC_BY_ID.get(payload["kind"], {"label": payload["kind"]})
        act = format_ms(payload["act_ms"])
        total = format_ms(payload["total_ms"])
        rb = "；".join(payload["readback_lines"]) or "无读回（未勾选显示器）"
        bad = any(("失败" in ln) or ("未找到" in ln) or ("异常" in ln)
                  for ln in payload["action_lines"])
        self.big_lbl.config(text="动作 %s ms · 含读回 %s ms" % (act, total))
        self.detail_lbl.config(text=rb)
        self._append_log(["        " + ln for ln in payload["action_lines"]])
        self._append_log([
            "        动作耗时：%s ms" % act,
            *["        " + ln for ln in payload["readback_lines"]],
            "        总耗时（含读回）：%s ms" % total,
            "",
        ])
        self.set_status("%s %s · 动作 %s ms · 总 %s ms" % (
            spec["label"], "完成（有失败项）" if bad else "完成", act, total),
            bad=bad)
        self._refit()

    # ---------- 队列 / 异常 / 退出 ----------
    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                tag = msg[0]
                if tag == "caps":
                    _tag, token, idx, _dev, caps = msg
                    if token == self._caps_token:
                        self._apply_caps(idx, caps)
                elif tag == "caps_done":
                    self._caps_done(msg[1])
                elif tag == "action_done":
                    self._finish_action(msg[1])
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(110, self._poll_queue)

    def _on_tk_error(self, exc, val, tb):
        text = "%s: %s" % (getattr(exc, "__name__", exc), val)
        self._append_log(["[%s] Tk 回调异常：%s" % (_ts(), text)])
        self.set_status("界面异常：" + text, bad=True)

    def _on_close(self):
        self._closing = True
        try:
            set_thread_execution_state(ES_CONTINUOUS)
        except Exception:
            pass
        self.root.destroy()


# ============================================================
# 七、离线自检（--selftest）
# ============================================================
FORBIDDEN_APIS = ("mouse_" + "event", "Send" + "Input", "SetCursor" + "Pos")


def run_selftest():
    checks = []

    def ck(name, cond, detail=""):
        checks.append((bool(cond), name, detail))

    ck("能力串 D6(01 04 05) → [1,4,5]",
       caps_power("D6(01 04 05)") == [POWER_ON, POWER_SOFT_OFF, POWER_OFF],
       str(caps_power("D6(01 04 05)")))
    ck("能力串 D6(01 05) → [1,5]",
       caps_power("D6(01 05)") == [POWER_ON, POWER_OFF],
       str(caps_power("D6(01 05)")))
    ck("能力串无 D6 → []", caps_power("mccs(10 12)") == [])
    ck("能力串 60(11 12 0F) → [17,18,15]",
       caps_inputs("60(11 12 0F)") == [17, 18, 15],
       str(caps_inputs("60(11 12 0F)")))
    ck("能力串 60(0F 11) → [15,17]", caps_inputs("60(0F 11)") == [15, 17])
    ck("型号解析 Legion R27qe Gen2",
       caps_model("model(Legion R27qe Gen2)") == "Legion R27qe Gen2")

    all_declared = [{"caps_state": "ok", "power": [1, 4, 5]},
                    {"caps_state": "ok", "power": [1, 4, 5]}]
    ck("4 全部声明 → ok", gate_d6(all_declared, 4)[0] == "ok")
    ck("2 未声明 → none", gate_d6(all_declared, 2)[0] == "none")
    ck("2 未声明时按钮文字带原因",
       "未声明" in d6_button_text(2, all_declared, False))
    ck("2 未声明时按钮不可点",
       d6_enabled(2, all_declared, loading=False, busy=False) is False)
    ck("4 已声明时按钮可点",
       d6_enabled(4, all_declared, loading=False, busy=False) is True)
    partial = [{"caps_state": "ok", "power": [1, 4, 5]},
               {"caps_state": "ok", "power": [1, 5]}]
    ck("部分声明 → partial 且可点",
       gate_d6(partial, 4)[0] == "partial"
       and d6_enabled(4, partial, loading=False, busy=False) is True)
    loading = [{"caps_state": "loading", "power": []}]
    ck("读取中 → loading 且不可点",
       gate_d6(loading, 4)[0] == "loading"
       and d6_enabled(4, loading, loading=True, busy=False) is False)

    ck("按钮清单覆盖全部要求", REQUIRED_IDS <= set(SPEC_BY_ID),
       "缺：%s" % sorted(REQUIRED_IDS - set(SPEC_BY_ID)))
    ck("版本 1.1.0", APP_VERSION == "1.1.0", APP_VERSION)
    ck("0x60 按钮已标注为切源往返",
       SPEC_BY_ID["input_roundtrip"]["label"] == "0x60 切源往返")
    ck("按钮清单共 11 项", len(ACTION_SPECS) == 11, str(len(ACTION_SPECS)))
    ck("每个按钮都有 API 说明",
       all(s.get("api") and s.get("label") for s in ACTION_SPECS))
    ck("耗时格式 12.34 → 12.3", format_ms(12.34) == "12.3")

    # 源码扫描：确认没有鼠标注入 API。打包后源码不在，跳过。
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wake_lab.py")
    if os.path.exists(src_path):
        try:
            with open(src_path, "r", encoding="utf-8") as f:
                src = f.read()
            found = [n for n in FORBIDDEN_APIS if n in src]
            ck("源码未包含鼠标注入 API", not found, "发现：%s" % found)
        except Exception as e:
            ck("源码未包含鼠标注入 API", False, "读取失败：%s" % e)
    else:
        ck("源码未包含鼠标注入 API", True, "打包运行，跳过源码扫描")

    ok_n = sum(1 for c in checks if c[0])
    total = len(checks)
    lines = [
        "%s v%s 离线自检" % (APP_NAME, APP_VERSION),
        "时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "平台：%s / Python %s" % (sys.platform, sys.version.split()[0]),
        "结果：%d/%d 通过" % (ok_n, total),
        "说明：本自检不打开 Tk、不访问 dxva2、不发送任何 DDC 命令。",
        "",
    ]
    for ok, name, detail in checks:
        lines.append("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                   ("（%s）" % detail) if detail else ""))
    lines += ["", "按钮清单（真实 API）："]
    for s in ACTION_SPECS:
        lines.append("  - %s：%s" % (s["label"], s["api"]))
    lines += [
        "",
        "启动路径：只枚举显示器 + 只读能力串；不调用 SetVCPFeature / "
        "SendMessageTimeoutW / SetThreadExecutionState。",
        "点击路径：每个按钮只执行上面列出的那一条/那一组调用；不做重试或自动升级。",
    ]
    report = "\n".join(lines) + "\n"

    out_path = ""
    try:
        base = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__))
        if not os.access(base, os.W_OK):
            base = os.path.join(os.environ.get("LOCALAPPDATA",
                                               os.path.expanduser("~")), APP_NAME)
            os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, "selftest.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception as e:
        report += "\n写 selftest.txt 失败：%s: %s\n" % (type(e).__name__, e)

    try:
        if sys.stdout is not None:
            sys.stdout.write(report)
            if out_path:
                sys.stdout.write("\nselftest.txt -> %s\n" % out_path)
            sys.stdout.flush()
    except Exception:
        pass
    return 0 if ok_n == total else 1


# ============================================================
# 八、入口
# ============================================================
def main():
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(run_selftest())
    if os.name != "nt":
        sys.stderr.write("WakeLab 只能在 Windows 上运行。\n")
        raise SystemExit(1)
    if not PIL_OK:
        try:
            from tkinter import messagebox
            messagebox.showerror(
                "缺少 Pillow",
                "图形界面需要 Pillow（圆角抗锯齿贴图）。\n安装后重试：pip install pillow")
        except Exception:
            pass
        raise SystemExit(1)
    root = tk.Tk()
    WakeLabApp(root)
    apply_dark_titlebar(root)
    root.mainloop()


if __name__ == "__main__":
    main()
