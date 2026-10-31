"""
i-Tail  QR -> Inkjet Sender

Reads a QR code with the camera and sends the commands inside it to a
Videojet 1710 over the LAN (WSI protocol, TCP port 3100).

Run:   python app.py            (or build the .exe with build_exe.bat)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageDraw, ImageTk

import wsi

APP_NAME = "i-Tail QR Inkjet Sender"
APP_VERSION = "1.0"

# ---- palette (taken from the i-Tail logo) ---------------------------------
BLUE = "#0B6FD0"
BLUE_DARK = "#0A4FA0"
BLUE_SOFT = "#EAF2FB"
ORANGE = "#F26B1D"
ORANGE_DARK = "#D85A10"
YELLOW = "#FFB81C"
BG = "#F2F6FB"
CARD = "#FFFFFF"
BORDER = "#D9E4F1"
TEXT = "#1B2B3F"
MUTED = "#6A7A90"
OK = "#1E9E5A"
OK_SOFT = "#E6F6EC"
ERR = "#D63A3A"
ERR_SOFT = "#FDECEC"
WARN_SOFT = "#FFF1E3"

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_H = ("Segoe UI", 11, "bold")
FONT_T = ("Segoe UI", 17, "bold")
FONT_BANNER = ("Segoe UI", 13, "bold")
FONT_MONO = ("Consolas", 9)

# The same QR that stays in front of the camera is handled ONCE. It counts as "gone" only
# after the camera has not seen it for this long; showing it again afterwards re-sends.
DEDUPE_GAP_SECONDS = 2.0


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def data_dir() -> str:
    env = os.environ.get("ITAIL_CONFIG_DIR")
    if env:
        path = env
    elif sys.platform.startswith("win"):
        path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "iTail-QR-Inkjet")
    else:
        path = os.path.join(os.path.expanduser("~"), ".config", "iTail-QR-Inkjet")
    os.makedirs(path, exist_ok=True)
    return path


DEFAULT_CONFIG = {"ip": "192.168.100.2", "port": wsi.DEFAULT_PORT, "camera": 0, "auto_send": True}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(os.path.join(data_dir(), "config.json"), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(os.path.join(data_dir(), "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def write_log(status: str, raw: str, message: str) -> None:
    try:
        with open(os.path.join(data_dir(), "send_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {status} | {raw} | {message}\n")
    except OSError:
        pass


def beep(ok: bool) -> None:
    try:
        import winsound
        winsound.PlaySound("SystemAsterisk" if ok else "SystemHand",
                           winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass


def gradient_image(width: int, height: int, stops) -> Image.Image:
    """Horizontal gradient. stops = [(position 0..1, (r,g,b)), ...]"""
    arr = np.zeros((height, width, 3), np.uint8)
    xs = np.linspace(0, 1, width)
    for ch in range(3):
        arr[:, :, ch] = np.interp(xs, [s[0] for s in stops], [s[1][ch] for s in stops])[None, :]
    return Image.fromarray(arr)


def hex_rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


# ===========================================================================
# Camera
# ===========================================================================
class CvSource:
    def __init__(self, index: int):
        backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.paced = False

    def is_open(self) -> bool:
        return self.cap.isOpened()

    def read(self):
        return self.cap.read()

    def release(self):
        self.cap.release()


class ImageSource:
    """Development only: pretend an image file is the camera (--test-image)."""

    def __init__(self, path: str):
        self.frame = cv2.imread(path)
        self.paced = True

    def is_open(self) -> bool:
        return self.frame is not None

    def read(self):
        return True, self.frame.copy()

    def release(self):
        pass


class CameraWorker(threading.Thread):
    def __init__(self, source, scans: "queue.Queue[bytes]", events: "queue.Queue[tuple]"):
        super().__init__(daemon=True)
        self.source = source
        self.scans = scans
        self.events = events
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.frame = None

    def stop(self):
        self.stop_flag.set()

    def run(self):
        fails, n = 0, 0
        poly, poly_t = None, 0.0
        while not self.stop_flag.is_set():
            ok, frame = self.source.read()
            if not ok or frame is None:
                fails += 1
                if fails > 40:
                    self.events.put(("camera_error", "อ่านภาพจากกล้องไม่ได้ (กล้องถูกใช้อยู่ หรือถูกถอดออก)"))
                    break
                time.sleep(0.05)
                continue
            fails = 0
            n += 1
            if n % 2 == 0:
                try:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    for r in zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode):
                        p = r.position
                        poly = np.array([[q.x, q.y] for q in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)],
                                        np.int32)
                        poly_t = time.time()
                        self.scans.put(bytes(r.bytes))
                except Exception as e:                       # decoding must never kill the camera
                    self.events.put(("camera_error", f"ถอดรหัส QR ผิดพลาด: {e}"))
            if poly is not None and time.time() - poly_t < 0.5:
                cv2.polylines(frame, [poly], True, (29, 107, 242), 4)          # orange (BGR)
            with self.lock:
                self.frame = frame
            if self.source.paced:
                time.sleep(0.033)
        self.source.release()

    def latest(self):
        with self.lock:
            f, self.frame = self.frame, None
        return f


# ===========================================================================
# GUI
# ===========================================================================
class App:
    def __init__(self, root: tk.Tk, test_image: str | None = None, autostart: bool = False):
        self.root = root
        self.cfg = load_config()
        self.test_image = test_image
        self.worker: CameraWorker | None = None
        self.scans: "queue.Queue[bytes]" = queue.Queue()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.current = None                  # (raw bytes, normalised bytes, packets)
        self.last_raw, self.last_seen = b"", 0.0
        self.photo = None
        self.canvas_img = None

        root.title(f"{APP_NAME}  v{APP_VERSION}")
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1180, sw - 40)}x{min(780, sh - 90)}+{max((sw - 1180) // 2, 0)}+10")
        root.minsize(980, 640)
        try:
            if sys.platform.startswith("win"):
                root.iconbitmap(resource_path("assets/icon.ico"))
            else:
                self._icon = ImageTk.PhotoImage(Image.open(resource_path("assets/mark.png")).resize((64, 64)))
                root.iconphoto(True, self._icon)
        except Exception:
            pass

        self._style()
        self._build_header()
        self._build_body()
        self._show_idle()
        self._draw_placeholder()

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(33, self._tick)
        if autostart:
            root.after(300, self.toggle_camera)

    # ------------------------------------------------------------------ style
    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background="white", fieldbackground="white", foreground=TEXT,
                     rowheight=26, borderwidth=0, font=FONT)
        st.configure("Treeview.Heading", background=BLUE_SOFT, foreground=BLUE_DARK, font=FONT_B,
                     relief="flat", borderwidth=0, padding=(6, 5))
        st.map("Treeview", background=[("selected", "#CFE3FA")], foreground=[("selected", TEXT)])
        st.map("Treeview.Heading", background=[("active", "#D6E6F7")])
        st.configure("TCombobox", fieldbackground="white", background=BLUE_SOFT, arrowcolor=BLUE,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=4)
        st.configure("Vertical.TScrollbar", background=BLUE_SOFT, troughcolor=BG, bordercolor=BG,
                     arrowcolor=BLUE)

    def _button(self, parent, text, command, kind="primary", width=None):
        palette = {"primary": (BLUE, "#0A5CAD", "white"),
                   "accent": (ORANGE, ORANGE_DARK, "white"),
                   "ghost": (BLUE_SOFT, "#D6E6F7", BLUE_DARK)}
        bg, hover, fg = palette[kind]
        b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=hover,
                      activeforeground=fg, relief="flat", bd=0, padx=16, pady=7, font=FONT_B,
                      cursor="hand2", disabledforeground="#B9C4D2", width=width)
        b._bg, b._hover = bg, hover
        b.bind("<Enter>", lambda e: b["state"] != "disabled" and b.config(bg=b._hover))
        b.bind("<Leave>", lambda e: b.config(bg=b._bg))
        return b

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Frame(head, bg=ORANGE, width=4, height=18).pack(side="left", padx=(0, 8))
        tk.Label(head, text=title, bg=CARD, fg=BLUE_DARK, font=FONT_H).pack(side="left")
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=(2, 12))
        return outer, inner

    # ----------------------------------------------------------------- header
    def _build_header(self):
        head = tk.Frame(self.root, bg="white", height=88)
        head.pack(fill="x")
        head.pack_propagate(False)
        logo = Image.open(resource_path("assets/logo.png"))
        h = 66
        logo = logo.resize((int(logo.width * h / logo.height), h), Image.LANCZOS)
        self.logo_img = ImageTk.PhotoImage(logo)
        tk.Label(head, image=self.logo_img, bg="white").pack(side="left", padx=(22, 14), pady=10)

        titles = tk.Frame(head, bg="white")
        titles.pack(side="left", pady=10)
        tk.Label(titles, text="QR  →  Inkjet Sender", bg="white", fg=BLUE, font=FONT_T).pack(anchor="w")
        tk.Label(titles, text="สแกน QR Code แล้วส่งข้อมูลเข้าเครื่อง Videojet 1710 ผ่านสาย LAN",
                 bg="white", fg=MUTED, font=FONT).pack(anchor="w")

        self.chip = tk.Label(head, text="●  ยังไม่ได้ทดสอบการเชื่อมต่อ", bg=BLUE_SOFT, fg=MUTED,
                             font=FONT_B, padx=14, pady=6)
        self.chip.pack(side="right", padx=22)

        # accent bar: warm band (yellow -> orange) then cool band (teal -> blue), like the logo
        self.bar = tk.Label(self.root, height=0, bd=0, bg=ORANGE)
        self.bar.pack(fill="x")
        self.bar.bind("<Configure>", self._redraw_bar)
        self._bar_w = 0

    def _redraw_bar(self, event):
        w = max(event.width, 10)
        if w == self._bar_w:
            return
        self._bar_w = w
        warm = int(w * 0.22)
        left = gradient_image(warm, 6, [(0, hex_rgb(YELLOW)), (1, hex_rgb(ORANGE))])
        right = gradient_image(w - warm - 4, 6, [(0, (77, 182, 176)), (0.45, hex_rgb(BLUE)),
                                                 (1, hex_rgb(BLUE_DARK))])
        strip = Image.new("RGB", (w, 6), (255, 255, 255))
        strip.paste(left, (0, 0))
        strip.paste(right, (warm + 4, 0))
        self.bar_img = ImageTk.PhotoImage(strip)
        self.bar.config(image=self.bar_img)

    # ------------------------------------------------------------------- body
    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        body.columnconfigure(0, weight=3, uniform="cols")
        body.columnconfigure(1, weight=2, uniform="cols")
        body.rowconfigure(0, weight=1)

        # ---- left: camera
        cam_outer, cam = self._card(body, "กล้องสแกน QR")
        cam_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.canvas = tk.Canvas(cam, bg="#0F2238", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.worker is None and self._draw_placeholder())

        ctrl = tk.Frame(cam, bg=CARD)
        ctrl.pack(fill="x", pady=(10, 0))
        tk.Label(ctrl, text="กล้อง", bg=CARD, fg=TEXT, font=FONT).pack(side="left")
        self.cam_var = tk.StringVar(value=f"กล้องที่ {self.cfg.get('camera', 0)}")
        self.cam_combo = ttk.Combobox(ctrl, textvariable=self.cam_var, width=11, state="readonly",
                                      values=[f"กล้องที่ {i}" for i in range(6)], font=FONT)
        self.cam_combo.pack(side="left", padx=(8, 12))
        self.cam_btn = self._button(ctrl, "เปิดกล้อง", self.toggle_camera, "primary")
        self.cam_btn.pack(side="left")
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_send", True)))
        tk.Checkbutton(ctrl, text="ส่งอัตโนมัติเมื่อสแกนได้", variable=self.auto_var, bg=CARD, fg=TEXT,
                       activebackground=CARD, selectcolor="white", font=FONT, command=self._save_cfg
                       ).pack(side="right")

        # ---- right column
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=4, minsize=318)
        right.rowconfigure(2, weight=1, minsize=100)

        # connection card
        con_outer, con = self._card(right, "เครื่อง Inkjet (Videojet 1710)")
        con_outer.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        row = tk.Frame(con, bg=CARD)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        tk.Label(row, text="IP Address", bg=CARD, fg=MUTED, font=FONT).grid(row=0, column=0, sticky="w")
        tk.Label(row, text="Port", bg=CARD, fg=MUTED, font=FONT).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.ip_var = tk.StringVar(value=str(self.cfg["ip"]))
        self.port_var = tk.StringVar(value=str(self.cfg["port"]))
        self.ip_entry = self._entry(row, self.ip_var, 16)
        self.ip_entry.grid(row=1, column=0, sticky="ew", ipady=5)
        self.port_entry = self._entry(row, self.port_var, 7)
        self.port_entry.grid(row=1, column=1, sticky="w", padx=(10, 0), ipady=5)
        self.ip_var.trace_add("write", lambda *a: self._on_conn_change())
        self.port_var.trace_add("write", lambda *a: self._on_conn_change())
        brow = tk.Frame(con, bg=CARD)
        brow.pack(fill="x", pady=(10, 0))
        self.test_btn = self._button(brow, "ทดสอบการเชื่อมต่อ", self.test_connection, "ghost")
        self.test_btn.pack(side="left")
        self.conn_msg = tk.Label(brow, text="บันทึกค่าให้อัตโนมัติ", bg=CARD, fg=MUTED, font=FONT,
                                 wraplength=250, justify="left")
        self.conn_msg.pack(side="left", padx=(12, 0))

        # last scan card
        res_outer, res = self._card(right, "ผลการสแกนล่าสุด")
        res_outer.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
        self.banner = tk.Label(res, text="", font=FONT_BANNER, padx=12, pady=9, anchor="w", justify="left",
                               wraplength=380)
        self.banner.pack(fill="x")
        # buttons and raw text are packed BEFORE the table, so on a short window the table
        # (which can scroll) is the part that shrinks, never the buttons
        actions = tk.Frame(res, bg=CARD)
        actions.pack(fill="x", pady=(8, 0))
        self.send_btn = self._button(actions, "ส่งข้อมูล", self.send_current, "accent")
        self.send_btn.pack(side="left")
        self.clear_btn = self._button(actions, "ล้างผล", self._clear_result, "ghost")
        self.clear_btn.pack(side="left", padx=(10, 0))
        self.send_btn.config(state="disabled")
        self.raw_label = tk.Label(res, text="", bg=CARD, fg=MUTED, font=FONT_MONO, anchor="nw", justify="left",
                                  wraplength=390, height=3)
        self.raw_label.pack(side="bottom", fill="x", pady=(6, 0))
        self.tree = ttk.Treeview(res, columns=("item", "value"), show="headings", height=4)
        self.tree.heading("item", text="รายการ")
        self.tree.heading("value", text="ค่าที่จะส่ง")
        self.tree.column("item", width=120, anchor="w", stretch=False)
        self.tree.column("value", width=240, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(8, 0))

        # history card
        his_outer, his = self._card(right, "ประวัติการส่ง")
        his_outer.grid(row=2, column=0, sticky="nsew")
        self.hist = ttk.Treeview(his, columns=("time", "status", "detail"), show="headings", height=3)
        for col, text, w, stretch in (("time", "เวลา", 70, False), ("status", "สถานะ", 70, False),
                                      ("detail", "รายละเอียด", 240, True)):
            self.hist.heading(col, text=text)
            self.hist.column(col, width=w, anchor="w", stretch=stretch)
        self.hist.tag_configure("ok", foreground=OK)
        self.hist.tag_configure("err", foreground=ERR)
        sb = ttk.Scrollbar(his, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=sb.set)
        self.hist.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, width=width, font=("Segoe UI", 12), relief="flat",
                        bg="white", fg=TEXT, insertbackground=BLUE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BLUE)

    # ----------------------------------------------------------- config / conn
    def _on_conn_change(self):
        ip_ok = wsi.valid_ip(self.ip_var.get())
        self.ip_entry.config(highlightbackground=BORDER if ip_ok else ERR,
                             highlightcolor=BLUE if ip_ok else ERR)
        port_ok = self._port() is not None
        self.port_entry.config(highlightbackground=BORDER if port_ok else ERR,
                               highlightcolor=BLUE if port_ok else ERR)
        self._set_chip("idle")
        if ip_ok and port_ok:
            self._save_cfg()

    def _port(self):
        try:
            p = int(self.port_var.get().strip())
            return p if 1 <= p <= 65535 else None
        except ValueError:
            return None

    def _save_cfg(self):
        try:
            cam = int(self.cam_var.get().split()[-1])
        except ValueError:
            cam = 0
        self.cfg.update({"ip": self.ip_var.get().strip(), "port": self._port() or wsi.DEFAULT_PORT,
                         "camera": cam, "auto_send": bool(self.auto_var.get())})
        save_config(self.cfg)

    def _set_chip(self, state: str):
        look = {"idle": ("●  ยังไม่ได้ทดสอบการเชื่อมต่อ", BLUE_SOFT, MUTED),
                "ok": ("●  เชื่อมต่อเครื่อง Inkjet ได้", OK_SOFT, OK),
                "err": ("●  เชื่อมต่อเครื่อง Inkjet ไม่ได้", ERR_SOFT, ERR),
                "busy": ("●  กำลังตรวจสอบ...", WARN_SOFT, ORANGE)}[state]
        self.chip.config(text=look[0], bg=look[1], fg=look[2])

    def test_connection(self):
        if not wsi.valid_ip(self.ip_var.get()) or self._port() is None:
            self.conn_msg.config(text="IP หรือ Port ไม่ถูกต้อง", fg=ERR)
            self._set_chip("err")
            return
        ip, port = self.ip_var.get().strip(), self._port()
        self.test_btn.config(state="disabled")
        self._set_chip("busy")
        self.conn_msg.config(text=f"กำลังเชื่อมต่อ {ip}:{port} ...", fg=MUTED)

        def work():
            ok, text = wsi.test_connection(ip, port)
            self.events.put(("conn", ok, text))
        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------------- camera
    def toggle_camera(self):
        if self.worker is not None:
            self._stop_camera()
            return
        try:
            index = int(self.cam_var.get().split()[-1])
        except ValueError:
            index = 0
        source = ImageSource(self.test_image) if self.test_image else CvSource(index)
        if not source.is_open():
            self._banner("err", f"เปิดกล้องที่ {index} ไม่ได้ - ลองเลือกกล้องอื่น หรือปิดโปรแกรมอื่นที่ใช้กล้องอยู่")
            return
        self._save_cfg()
        self.worker = CameraWorker(source, self.scans, self.events)
        self.worker.start()
        self.cam_btn.config(text="ปิดกล้อง", bg=ORANGE)
        self.cam_btn._bg, self.cam_btn._hover = ORANGE, ORANGE_DARK
        self.cam_btn.config(activebackground=ORANGE_DARK)
        self.cam_combo.config(state="disabled")
        self._show_idle()

    def _stop_camera(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.cam_btn.config(text="เปิดกล้อง", bg=BLUE)
        self.cam_btn._bg, self.cam_btn._hover = BLUE, "#0A5CAD"
        self.cam_btn.config(activebackground="#0A5CAD")
        self.cam_combo.config(state="readonly")
        self._draw_placeholder()

    def _draw_placeholder(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 2), max(c.winfo_height(), 2)
        self.canvas_img = None
        if not hasattr(self, "_mark"):
            m = Image.open(resource_path("assets/mark.png")).convert("RGB")
            m = m.resize((110, 110), Image.LANCZOS)
            self._mark = ImageTk.PhotoImage(m)
        c.create_rectangle(0, 0, w, h, fill="#0F2238", outline="")
        c.create_image(w // 2, h // 2 - 40, image=self._mark)
        c.create_text(w // 2, h // 2 + 44, text="กด “เปิดกล้อง” แล้วนำ QR Code มาให้กล้องอ่าน",
                      fill="white", font=("Segoe UI", 12))
        c.create_text(w // 2, h // 2 + 72, text="ถือ QR ให้นิ่งและอยู่ในกรอบภาพ ไม่สะท้อนแสง",
                      fill="#8FA6C0", font=("Segoe UI", 10))

    def _show_frame(self, frame):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        h, w = frame.shape[:2]
        s = min(cw / w, ch / h)
        nw, nh = max(int(w * s), 1), max(int(h * s), 1)
        rgb = cv2.cvtColor(cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        if self.canvas_img is None:
            self.canvas.delete("all")
            self.canvas_img = self.canvas.create_image(cw // 2, ch // 2, image=self.photo)
        else:
            self.canvas.itemconfig(self.canvas_img, image=self.photo)
            self.canvas.coords(self.canvas_img, cw // 2, ch // 2)

    # --------------------------------------------------------------- main loop
    def _tick(self):
        try:
            if self.worker is not None:
                frame = self.worker.latest()
                if frame is not None:
                    self._show_frame(frame)
            while True:
                try:
                    raw = self.scans.get_nowait()
                except queue.Empty:
                    break
                self._on_scan(raw)
            while True:
                try:
                    ev = self.events.get_nowait()
                except queue.Empty:
                    break
                self._on_event(ev)
        finally:
            self.root.after(33, self._tick)

    def _on_event(self, ev):
        kind = ev[0]
        if kind == "conn":
            _, ok, text = ev
            self.test_btn.config(state="normal")
            self.conn_msg.config(text=text, fg=OK if ok else ERR)
            self._set_chip("ok" if ok else "err")
        elif kind == "sent":
            _, result, raw = ev
            self.busy = False
            self.send_btn.config(state="normal" if self.current else "disabled")
            self._set_chip("ok" if result.ok or result.steps else "err")
            self._finish_send(result, raw)
        elif kind == "camera_error":
            self._banner("err", ev[1])
            if self.worker is not None:
                self._stop_camera()

    # ---------------------------------------------------------------- scanning
    def _on_scan(self, raw: bytes):
        now = time.time()
        same = raw == self.last_raw and now - self.last_seen < DEDUPE_GAP_SECONDS
        self.last_seen = now if (same or not self.busy) else self.last_seen
        if self.busy or same:
            return
        self.last_raw = raw
        try:
            data, packets = wsi.parse_scan(raw)
        except wsi.ProtocolError as e:
            self.current = None
            self._fill_tree([])
            self.raw_label.config(text=wsi.render_bytes(raw))
            self._banner("err", f"✗  QR นี้ส่งไม่ได้: {e}")
            self._history("err", "QR ผิดรูปแบบ", wsi.render_bytes(raw))
            write_log("REJECTED", wsi.render_bytes(raw), str(e))
            beep(False)
            self.send_btn.config(state="disabled")
            return
        self.current = (raw, data, packets)
        self._fill_tree(packets)
        self.raw_label.config(text=wsi.render_bytes(data))
        self.send_btn.config(state="normal")
        if self.auto_var.get():
            self.send_current()
        else:
            self._banner("info", "สแกนแล้ว - ตรวจข้อมูลแล้วกด “ส่งข้อมูล”")
            beep(True)

    def _fill_tree(self, packets):
        self.tree.delete(*self.tree.get_children())
        for p in packets:
            self.tree.insert("", "end", values=(p.label, p.value))

    def send_current(self):
        if not self.current or self.busy:
            return
        if not wsi.valid_ip(self.ip_var.get()) or self._port() is None:
            self._banner("err", "✗  IP หรือ Port ของเครื่อง Inkjet ไม่ถูกต้อง - แก้ไขแล้วสแกน/กดส่งอีกครั้ง")
            beep(False)
            return
        ip, port = self.ip_var.get().strip(), self._port()
        raw, data, packets = self.current
        self.busy = True
        self.send_btn.config(state="disabled")
        self._banner("busy", f"กำลังส่งข้อมูลไปที่ {ip}:{port} ...")

        def work():
            result = wsi.send_packets(ip, port, packets)
            self.events.put(("sent", result, raw))
        threading.Thread(target=work, daemon=True).start()

    def _finish_send(self, result: wsi.SendResult, raw: bytes):
        summary = ", ".join(f"{p.label}={p.value}" if p.kind == "U" else p.value
                            for p, _, _ in result.steps) or wsi.render_bytes(raw)
        if result.ok:
            self._banner("ok", f"✓  {result.message}")
            self._history("ok", "ส่งสำเร็จ", summary)
            write_log("OK", wsi.render_bytes(raw), result.message)
            beep(True)
        else:
            self._banner("err", f"✗  {result.message}")
            self._history("err", "ล้มเหลว", result.message)
            write_log("FAILED", wsi.render_bytes(raw), result.message)
            beep(False)

    def _history(self, tag: str, status: str, detail: str):
        self.hist.insert("", 0, values=(f"{datetime.datetime.now():%H:%M:%S}", status, detail), tags=(tag,))
        for iid in self.hist.get_children()[300:]:
            self.hist.delete(iid)

    def _clear_result(self):
        self.current = None
        self.last_raw = b""
        self._fill_tree([])
        self.raw_label.config(text="")
        self.send_btn.config(state="disabled")
        self._show_idle()

    def _show_idle(self):
        self._banner("info", "พร้อมสแกน - เปิดกล้องแล้วนำ QR Code มาให้กล้องอ่าน")

    def _banner(self, kind: str, text: str):
        bg, fg = {"info": (BLUE_SOFT, BLUE_DARK), "ok": (OK_SOFT, OK), "err": (ERR_SOFT, ERR),
                  "busy": (WARN_SOFT, ORANGE_DARK)}[kind]
        self.banner.config(text=text, bg=bg, fg=fg)

    # ------------------------------------------------------------------ close
    def close(self):
        self._save_cfg()
        if self.worker is not None:
            self.worker.stop()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--test-image", help="(development) use an image file instead of a camera")
    ap.add_argument("--autostart", action="store_true", help="(development) start the camera at launch")
    args = ap.parse_args()

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, test_image=args.test_image, autostart=args.autostart)
    root.mainloop()


if __name__ == "__main__":
    main()
