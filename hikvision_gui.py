#!/usr/bin/env python3
"""
Hikvision NVR/DVR Backup Tool — Desktop GUI
Place alongside pure_hik_client.py and run:  python hikvision_gui.py

Requires only the Python standard library (tkinter ships with Python).
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
import threading, queue, os, sys, re, time, logging, json, subprocess, csv
from datetime import datetime, timedelta, timezone
import urllib.request, urllib.error
from xml.sax.saxutils import escape

# Optional: Pillow for thumbnail display (pip install Pillow)
try:
    from PIL import Image, ImageTk
    import io as _pil_io
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "hik_profiles.json")

# ── dependency check ─────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pure_hik_client import PureHikClient, convert_to_mp4, DownloadIncompleteError
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Missing file",
        "pure_hik_client.py not found.\n"
        "Place it in the same folder as hikvision_gui.py and try again.")
    sys.exit(1)


# ── download helper (resume-capable) ─────────────────────────────────────────
_DL_ENDPOINTS = [
    "/ISAPI/ContentMgmt/download",
    "/PSIA/Custom/SelfExt/ContentMgmt/download",
]

def download_with_resume(client, playback_uri, dest_path,
                         progress_cb=None, limit_mbps=0.0, stop_event=None):
    """
    Download one clip, resuming a partial file when possible.
    Sends Range: bytes=<existing>- ; falls back to full download on HTTP 200.
    Calls progress_cb(total_bytes_on_disk, speed_MB_s) every chunk.
    Raises DownloadIncompleteError, InterruptedError (user stop), or IOError.
    Returns total bytes on disk.
    """
    m = re.search(r'[?&]size=(\d+)', playback_uri)
    expected = int(m.group(1)) if m else 0

    existing = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
    if expected > 0 and existing >= expected:
        return existing   # already complete

    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<downloadRequest version="1.0"'
        ' xmlns="http://urn:selfextension:psiaext-ver10-xsd">'
        f'<playbackURI>{escape(playback_uri)}</playbackURI>'
        '</downloadRequest>'
    ).encode()

    resp = None
    for ep in _DL_ENDPOINTS:
        hdrs = {"Content-Type": "application/xml", "User-Agent": "OpenHik-Client/1.0"}
        if existing > 0:
            hdrs["Range"] = f"bytes={existing}-"
        req = urllib.request.Request(
            client.base_url + ep, data=xml_body, headers=hdrs, method="POST")
        try:
            resp = client.opener.open(req, timeout=max(client.timeout, 30))
            break
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                continue
            raise

    if resp is None:
        raise ConnectionError("NVR did not accept any download endpoint")

    # 206 = server honours Range; 200 = start over
    if existing > 0 and resp.status == 200:
        existing, mode = 0, "wb"
    elif existing > 0 and resp.status == 206:
        mode = "ab"
    else:
        mode = "wb"

    CHUNK = 512 * 1024
    written = 0
    t0 = time.time()
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    with resp, open(dest_path, mode) as f:
        while True:
            if stop_event and stop_event.is_set():
                raise InterruptedError("Stopped by user")
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            if limit_mbps > 0:
                target = ((existing + written) * 8) / (limit_mbps * 1e6)
                slp = target - (time.time() - t0)
                if slp > 0:
                    time.sleep(slp)
            if progress_cb:
                elapsed = max(0.001, time.time() - t0)
                progress_cb(existing + written, (written / 1_048_576) / elapsed)

    total = existing + written
    if expected > 0 and total < expected:
        raise DownloadIncompleteError(
            f"{total:,} / {expected:,} bytes ({100*total/expected:.1f}%)")
    return total


# ── constants ─────────────────────────────────────────────────────────────────
CHK_ON  = "☑"
CHK_OFF = "☐"

# Platform-appropriate fonts
if sys.platform == "win32":
    F_UI   = ("Segoe UI",  10)
    F_MONO = ("Consolas",   9)
elif sys.platform == "darwin":
    F_UI   = ("SF Pro Text", 12)
    F_MONO = ("Menlo",       11)
else:
    F_UI   = ("Sans",        10)
    F_MONO = ("Monospace",    9)


# ── Channel Names dialog ──────────────────────────────────────────────────────
class ChannelNamesDialog(tk.Toplevel):
    """
    Popup with one Entry per channel so the user can assign friendly labels.
    e.g.  Ch 2 → "Front Gate"   Ch 3 → "Car Park"
    Calls on_save(names_dict) where names_dict maps ch_int → label_str.
    """
    def __init__(self, parent, channels, current_names, on_save):
        super().__init__(parent)
        self.title("Name Channels")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._on_save = on_save
        self._vars = {}   # ch_int → StringVar

        ttk.Label(self,
                  text="Give each channel a friendly name (leave blank to use default).",
                  font=(*F_UI[:1], F_UI[1]-1),
                  foreground="#555555").pack(anchor="w", padx=14, pady=(12, 6))

        grid = ttk.Frame(self, padding=(14, 0, 14, 8))
        grid.pack(fill="x")

        for row_idx, ch in enumerate(sorted(channels)):
            ttk.Label(grid, text=f"Ch {ch}", width=6, anchor="e").grid(
                row=row_idx, column=0, sticky="e", padx=(0, 8), pady=3)
            var = tk.StringVar(value=current_names.get(ch, ""))
            self._vars[ch] = var
            e = ttk.Entry(grid, textvariable=var, width=28)
            e.grid(row=row_idx, column=1, sticky="ew", pady=3)
            ttk.Label(grid, text=f"(default: Ch {ch})", foreground="#999999",
                      font=(*F_UI[:1], F_UI[1]-1)).grid(
                row=row_idx, column=2, padx=(8, 0), sticky="w")
        grid.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(self, padding=(14, 0, 14, 12))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(btn_row, text="Clear All",
                   command=self._clear).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side="right")

    def _save(self):
        names = {ch: var.get().strip()
                 for ch, var in self._vars.items()
                 if var.get().strip()}
        self._on_save(names)
        self.destroy()

    def _clear(self):
        for var in self._vars.values():
            var.set("")


# ── Available Dates dialog ────────────────────────────────────────────────────
class AvailableDatesDialog(tk.Toplevel):
    """
    Popup that shows which dates have recordings, per channel.
    User can double-click a date to auto-fill start/end in the main window.
    """
    def __init__(self, parent, dates_by_ch, on_pick, ch_names=None):
        """
        dates_by_ch: dict  { ch_int: set_of_date_strings_YYYY_MM_DD }
        on_pick:     callable(date_str) called when user picks a date
        ch_names:    optional dict { ch_int: label_str } for column headings
        """
        super().__init__(parent)
        self.title("Available Recording Dates")
        self.geometry("520x460")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._on_pick = on_pick
        self._ch_names = ch_names or {}
        self._build(dates_by_ch)

    def _build(self, dates_by_ch):
        # Collect all unique dates across channels, sorted
        all_dates = sorted({d for dates in dates_by_ch.values() for d in dates},
                           reverse=True)

        ttk.Label(self, text="Double-click a date to fill it into Start/End Date fields.",
                  font=(*F_UI[:1], F_UI[1]-1),
                  foreground="#555555").pack(anchor="w", padx=12, pady=(10, 4))

        # Build columns: Date | Ch1 | Ch2 | … (one column per channel)
        channels = sorted(dates_by_ch.keys())
        cols = ("date",) + tuple(f"ch{c}" for c in channels)
        col_labels = ("Date",) + tuple(
            self._ch_names.get(c, f"Ch {c}") for c in channels)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            selectmode="browse", height=16)

        for col, lbl in zip(cols, col_labels):
            tree.heading(col, text=lbl, anchor="center")
            w = 120 if col == "date" else max(50, 300 // len(channels))
            tree.column(col, width=w, minwidth=40, anchor="center")

        tree.tag_configure("has",  foreground="#27ae60")
        tree.tag_configure("none", foreground="#aaaaaa")

        for d in all_dates:
            vals = (d,) + tuple(
                "●" if d in dates_by_ch.get(ch, set()) else "·"
                for ch in channels)
            has_any = any(d in dates_by_ch.get(ch, set()) for ch in channels)
            tree.insert("", "end", iid=d, values=vals,
                        tags=("has" if has_any else "none",))

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        tree.bind("<Double-1>", self._on_dbl)
        self._tree = tree

        hint = ttk.Label(self,
            text=f"{len(all_dates)} date(s) with recordings  |  ● = has recordings  · = none",
            foreground="#888888", font=(*F_UI[:1], F_UI[1]-1))
        hint.pack(anchor="w", padx=12, pady=(0, 2))

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(btn_row, text="Use Selected Date",
                   command=self._use_selected).pack(side="left")
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side="right")

    def _on_dbl(self, event):
        iid = self._tree.identify_row(event.y)
        if iid:
            self._on_pick(iid)
            self.destroy()

    def _use_selected(self):
        sel = self._tree.selection()
        if sel:
            self._on_pick(sel[0])
            self.destroy()


# ── main application ──────────────────────────────────────────────────────────
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Hikvision NVR Backup")
        self.geometry("1020x980")
        self.minsize(820, 800)

        self.client     = None
        self.start_utc  = self.end_utc = None
        self._rows      = []        # [{"seg": seg, "ch": int, "iid": str}]
        self._checked   = {}        # iid -> bool
        self._ch_names  = {}        # ch_int -> label string
        self._profiles   = self._load_profiles()
        self._thumb_img  = None   # keep PhotoImage reference alive
        self._sel_row    = None   # currently selected row dict
        self._q          = queue.Queue()
        self._stop       = threading.Event()
        self._session_log_name = f"hik_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        self._setup_style()
        self._build()
        self.after(100, self._poll)

    # ── style ─────────────────────────────────────────────────────────────
    def _setup_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", font=F_UI)
        s.configure("Heading.TLabel", font=(F_UI[0], F_UI[1], "bold"))
        s.configure("Accent.TButton",
                    background="#1565c0", foreground="white",
                    font=(F_UI[0], F_UI[1], "bold"))
        s.map("Accent.TButton",
              background=[("active", "#1976d2"), ("disabled", "#90a4ae")])
        s.configure("Stop.TButton", background="#c0392b", foreground="white")
        s.map("Stop.TButton",
              background=[("active", "#e74c3c"), ("disabled", "#90a4ae")])
        s.configure("Teal.TButton", background="#00796b", foreground="white")
        s.map("Teal.TButton",
              background=[("active", "#00897b"), ("disabled", "#90a4ae")])

    # ── build ─────────────────────────────────────────────────────────────
    def _build(self):
        # Scrollable canvas so the UI fits any screen height
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(container, orient="vertical")
        vsb.pack(side="right", fill="y")

        self._canvas = tk.Canvas(container, yscrollcommand=vsb.set,
                                  highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._canvas.yview)

        outer = ttk.Frame(self._canvas, padding=6)
        self._scroll_win = self._canvas.create_window(
            (0, 0), window=outer, anchor="nw")

        outer.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Mouse-wheel scrolling
        self._canvas.bind_all("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self._canvas.bind_all("<Button-4>",
            lambda e: self._canvas.yview_scroll(-1, "units"))
        self._canvas.bind_all("<Button-5>",
            lambda e: self._canvas.yview_scroll(1, "units"))

        self._build_connection(outer)
        self._build_query(outer)
        self._build_tree(outer)
        self._build_detail(outer)
        self._build_options(outer)
        self._build_progress(outer)
        self._build_log(outer)

    def _on_frame_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._scroll_win, width=event.width)

    # ── 1. Connection ─────────────────────────────────────────────────────
    def _build_connection(self, parent):
        f = ttk.LabelFrame(parent, text="Device Connection", padding=6)
        f.pack(fill="x", pady=(0, 4))

        # Row 0 — profile bar
        ttk.Label(f, text="Profile").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.v_profile = tk.StringVar()
        self.cmb_profile = ttk.Combobox(
            f, textvariable=self.v_profile, width=22, state="readonly")
        self.cmb_profile.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.cmb_profile.bind("<<ComboboxSelected>>", self._on_profile_select)
        ttk.Button(f, text="Save", command=self._save_profile).grid(
            row=0, column=2, padx=(0, 4))
        ttk.Button(f, text="Delete", command=self._delete_profile).grid(
            row=0, column=3, padx=(0, 10))
        self._refresh_profile_combo()

        # Row 1 — connection fields
        fields = [
            ("Host",     "v_host", 18, "192.168.2.241", False),
            ("Port",     "v_port",  6, "8080",          False),
            ("Username", "v_user", 10, "admin",         False),
            ("Password", "v_pass", 14, "",              True ),
        ]
        col = 0
        for label, attr, width, default, secret in fields:
            ttk.Label(f, text=label).grid(row=1, column=col,   sticky="e", padx=(0, 4))
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            kw = {"show": "●"} if secret else {}
            ttk.Entry(f, textvariable=var, width=width, **kw).grid(
                row=1, column=col+1, sticky="ew", padx=(0, 10))
            col += 2

        self.btn_connect = ttk.Button(f, text="Connect", command=self._do_connect)
        self.btn_connect.grid(row=1, column=col, padx=(4, 0))

        self.btn_detect = ttk.Button(
            f, text="Detect Channels", command=self._do_detect, state="disabled")
        self.btn_detect.grid(row=1, column=col+1, padx=(6, 0))

        for c in (1, 3, 5, 7):
            f.columnconfigure(c, weight=1)

        self.lbl_status = ttk.Label(f, text="Not connected", foreground="#888888")
        self.lbl_status.grid(row=2, column=0, columnspan=col+2, sticky="w", pady=(6, 2))

        # Storage usage bar (populated after connect)
        stor_row = ttk.Frame(f)
        stor_row.grid(row=3, column=0, columnspan=col+2, sticky="ew", pady=(0, 2))
        ttk.Label(stor_row, text="Storage:").pack(side="left", padx=(0, 6))
        self.pb_storage = ttk.Progressbar(stor_row, mode="determinate",
                                          length=180, maximum=100)
        self.pb_storage.pack(side="left")
        self.lbl_storage = ttk.Label(stor_row, text="—", foreground="#888888")
        self.lbl_storage.pack(side="left", padx=(8, 0))

    # ── 2. Query ──────────────────────────────────────────────────────────

    def _build_query(self, parent):
        f = ttk.LabelFrame(parent, text="Search", padding=6)
        f.pack(fill="x", pady=(0, 4))

        today = datetime.now(timezone.utc)
        yd    = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        td    = today.strftime("%Y-%m-%d")

        self.v_start_date = tk.StringVar(value=yd)
        self.v_end_date   = tk.StringVar(value=yd)
        self.v_channels   = tk.StringVar(value="2,3,4")

        # Row 0 — date range
        ttk.Label(f, text="Start Date").grid(row=0, column=0, sticky="e", padx=(0, 4))
        ttk.Entry(f, textvariable=self.v_start_date, width=13).grid(
            row=0, column=1, sticky="ew", padx=(0, 6))

        ttk.Label(f, text="End Date").grid(row=0, column=2, sticky="e", padx=(0, 4))
        ttk.Entry(f, textvariable=self.v_end_date, width=13).grid(
            row=0, column=3, sticky="ew", padx=(0, 6))

        # Quick-fill helpers
        qf = ttk.Frame(f)
        qf.grid(row=0, column=4, padx=(4, 10))
        for label, days in (("Today", 0), ("Yesterday", 1), ("Last 7d", 6)):
            ttk.Button(qf, text=label, width=10,
                       command=lambda d=days: self._quick_fill(d)
                       ).pack(side="left", padx=2)

        # Row 1 — channels + buttons
        ttk.Label(f, text="Channels").grid(row=1, column=0, sticky="e",
                                           padx=(0, 4), pady=(6, 0))
        ttk.Entry(f, textvariable=self.v_channels, width=20).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(0, 6), pady=(6, 0))

        btn_row = ttk.Frame(f)
        btn_row.grid(row=1, column=4, sticky="w", pady=(6, 0), padx=(4, 0))

        self.btn_list_dates = ttk.Button(
            btn_row, text="📅 List Available Dates",
            command=self._do_list_dates,
            state="disabled", style="Teal.TButton")
        self.btn_list_dates.pack(side="left", padx=(0, 6))

        self.btn_search = ttk.Button(
            btn_row, text="Search Recordings",
            command=self._do_search,
            state="disabled", style="Accent.TButton")
        self.btn_search.pack(side="left")

        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

    # ── 3. Recordings treeview ────────────────────────────────────────────
    def _build_tree(self, parent):
        outer = ttk.LabelFrame(parent, text="Recordings", padding=6)
        outer.pack(fill="both", expand=True, pady=(0, 4))

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text=f"{CHK_ON} Select All",  command=self._select_all ).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text=f"{CHK_OFF} Select None", command=self._select_none).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="✏️ Name Channels", command=self._open_name_channels).pack(side="left", padx=(0, 4))
        self.btn_play = ttk.Button(bar, text="▶ Play", command=self._do_play, state="disabled")
        self.btn_play.pack(side="left")
        self.lbl_sel = ttk.Label(bar, text="No recordings loaded", foreground="#888888")
        self.lbl_sel.pack(side="right")

        tree_wrap = ttk.Frame(outer)
        tree_wrap.pack(fill="both", expand=True)

        cols = ("sel", "ch", "start", "end", "dur", "size")
        self.tree = ttk.Treeview(
            tree_wrap, columns=cols, show="headings",
            selectmode="none", height=7)

        self.tree.heading("sel",   text=CHK_ON,        anchor="center")
        self.tree.heading("ch",    text="Ch",           anchor="center")
        self.tree.heading("start", text="Start (UTC)")
        self.tree.heading("end",   text="End (UTC)")
        self.tree.heading("dur",   text="Duration",     anchor="center")
        self.tree.heading("size",  text="Size",         anchor="e")

        self.tree.column("sel",   width=34,  minwidth=34,  stretch=False, anchor="center")
        self.tree.column("ch",    width=44,  minwidth=44,  stretch=False, anchor="center")
        self.tree.column("start", width=190, minwidth=140)
        self.tree.column("end",   width=190, minwidth=140)
        self.tree.column("dur",   width=82,  minwidth=70,  stretch=False, anchor="center")
        self.tree.column("size",  width=86,  minwidth=70,  stretch=False, anchor="e")

        self.tree.tag_configure("on",  foreground="")
        self.tree.tag_configure("off", foreground="#aaaaaa")
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<ButtonRelease-1>", self._tree_select)
        self.tree.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        vsb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=vsb.set)

    # ── 3b. Detail panel (thumbnail + info) ──────────────────────────────
    def _build_detail(self, parent):
        f = ttk.LabelFrame(parent, text="Recording Preview", padding=6)
        f.pack(fill="x", pady=(0, 4))

        # Thumbnail pane
        thumb_frame = ttk.Frame(f, width=150, height=90, relief="sunken",
                                borderwidth=1)
        thumb_frame.pack(side="left", padx=(0, 12))
        thumb_frame.pack_propagate(False)

        self._thumb_label = tk.Label(
            thumb_frame,
            text="Select a\nrecording",
            fg="#aaaaaa", bg="#1a1a1a",
            font=(*F_MONO[:1], F_MONO[1]-1),
            anchor="center")
        self._thumb_label.pack(fill="both", expand=True)

        # Info grid
        info = ttk.Frame(f)
        info.pack(side="left", fill="both", expand=True)

        labels = ("Channel", "Start", "End", "Duration", "Size", "Local file")
        self._detail_vars = {}
        for i, lbl in enumerate(labels):
            ttk.Label(info, text=lbl + ":", foreground="#888888",
                      font=(*F_UI[:1], F_UI[1]-1)).grid(
                row=i, column=0, sticky="e", padx=(0, 6), pady=1)
            var = tk.StringVar(value="—")
            self._detail_vars[lbl] = var
            ttk.Label(info, textvariable=var).grid(
                row=i, column=1, sticky="w", pady=1)
        info.columnconfigure(1, weight=1)

        if not _PIL_OK:
            ttk.Label(f,
                text="📷 Install Pillow for thumbnails:\n  pip install Pillow",
                foreground="#888888",
                font=(*F_MONO[:1], F_MONO[1]-1),
                justify="left").pack(side="right", padx=(12, 0))

    # ── 4. Download options ───────────────────────────────────────────────
    def _build_options(self, parent):
        f = ttk.LabelFrame(parent, text="Download Options", padding=6)
        f.pack(fill="x", pady=(0, 4))

        ttk.Label(f, text="Save to").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.v_savedir = tk.StringVar(value=r"C:\NVR-Backup")
        ttk.Entry(f, textvariable=self.v_savedir).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(0, 4))
        ttk.Button(f, text="Browse…", command=self._browse).grid(row=0, column=4)

        self.v_convert      = tk.BooleanVar(value=False)
        self.v_keepraw      = tk.BooleanVar(value=False)
        self.v_skip_existing = tk.BooleanVar(value=True)
        self.v_limit        = tk.StringVar(value="0")

        ttk.Checkbutton(f, text="Convert to MP4  (requires ffmpeg)",
                        variable=self.v_convert,
                        command=self._toggle_convert).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.cb_keepraw = ttk.Checkbutton(
            f, text="Keep raw .ps after conversion",
            variable=self.v_keepraw, state="disabled")
        self.cb_keepraw.grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Checkbutton(f, text="Skip already-downloaded files",
                        variable=self.v_skip_existing).grid(
            row=3, column=0, columnspan=2, sticky="w")

        ttk.Label(f, text="Bandwidth limit (MB/s, 0 = unlimited)").grid(
            row=1, column=2, sticky="e", padx=(16, 4), pady=(6, 0))
        ttk.Entry(f, textvariable=self.v_limit, width=6).grid(
            row=1, column=3, sticky="w", pady=(6, 0))

        f.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(10, 0))

        self.btn_dl = ttk.Button(
            btn_row, text="Download Selected",
            command=self._do_download,
            state="disabled", style="Accent.TButton")
        self.btn_dl.pack(side="left")

        self.btn_stop = ttk.Button(
            btn_row, text="⏹  Stop",
            command=self._do_stop,
            state="disabled", style="Stop.TButton")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.btn_export_csv = ttk.Button(
            btn_row, text="Export CSV",
            command=self._export_csv,
            state="disabled")
        self.btn_export_csv.pack(side="left", padx=(8, 0))

    # ── 5. Progress ───────────────────────────────────────────────────────
    def _build_progress(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill="x", pady=(0, 4))

        self.lbl_file = ttk.Label(f, text="")
        self.lbl_file.pack(fill="x")
        self.pb_file = ttk.Progressbar(f, mode="determinate")
        self.pb_file.pack(fill="x", pady=(2, 6))

        self.lbl_overall = ttk.Label(f, text="")
        self.lbl_overall.pack(fill="x")
        self.pb_overall = ttk.Progressbar(f, mode="determinate")
        self.pb_overall.pack(fill="x", pady=(2, 0))

    # ── 6. Log ────────────────────────────────────────────────────────────
    def _build_log(self, parent):
        f = ttk.LabelFrame(parent, text="Log", padding=4)
        f.pack(fill="x")

        self.log_box = scrolledtext.ScrolledText(
            f, height=5, font=F_MONO,
            state="disabled", wrap="word",
            relief="flat", borderwidth=0)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("err",  foreground="#c0392b")
        self.log_box.tag_configure("warn", foreground="#d35400")
        self.log_box.tag_configure("ok",   foreground="#27ae60")
        self.log_box.tag_configure("info", foreground="")

    # ── queue poll ────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]

                if kind == "log":
                    self._log_write(msg[1], msg[2])

                elif kind == "status":
                    self.lbl_status.config(text=msg[1], foreground=msg[2])

                elif kind == "connected":
                    self.btn_search.config(state="normal")
                    self.btn_list_dates.config(state="normal")
                    self.btn_detect.config(state="normal")

                elif kind == "search_done":
                    self._on_search_done()

                elif kind == "file_prog":
                    self.lbl_file.config(text=msg[1])
                    self.pb_file["value"] = msg[2]

                elif kind == "overall_prog":
                    done, total = msg[1], msg[2]
                    pct = done / total * 100 if total else 0
                    self.lbl_overall.config(
                        text=f"Overall: {done} / {total} segments  ({pct:.0f}%)")
                    self.pb_overall["value"] = pct

                elif kind == "dl_done":
                    self._on_dl_done(msg[1], msg[2])

                elif kind == "list_dates_done":
                    self._on_list_dates_done(msg[1])

                elif kind == "list_dates_err":
                    self._on_list_dates_err(msg[1])

                elif kind == "detect_done":
                    self._on_detect_done(msg[1])

                elif kind == "storage_info":
                    total_gb, used_gb = msg[1], msg[2]
                    if total_gb > 0:
                        pct = min(100, used_gb / total_gb * 100)
                        self.pb_storage["value"] = pct
                        color = "#c0392b" if pct > 85 else "#e67e22" if pct > 70 else "#27ae60"
                        self.lbl_storage.config(
                            text=f"{used_gb:.0f} / {total_gb:.0f} GB  ({pct:.0f}% used)",
                            foreground=color)

                elif kind == "thumb_ready":
                    self._on_thumb_ready(msg[1])

                elif kind == "thumb_err":
                    self._thumb_label.config(image="", text="No preview")
                    self._thumb_img = None

        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _log_write(self, text, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"{ts}  {text}\n", tag)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        # Mirror to log file in the save folder (if one is set)
        save_dir = self.v_savedir.get().strip() if hasattr(self, "v_savedir") else ""
        if save_dir:
            try:
                os.makedirs(save_dir, exist_ok=True)
                log_path = os.path.join(save_dir, self._session_log_name)
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
            except Exception:
                pass  # never let log file errors crash the UI

    # ── channel name helpers ──────────────────────────────────────────────
    def _ch_name(self, ch):
        """Return the user-assigned label for a channel, or 'Ch <n>' if none set."""
        return self._ch_names.get(ch, f"Ch {ch}")

    def _open_name_channels(self):
        channels = self._parse_channels()
        if not channels:
            messagebox.showinfo("No channels",
                "Enter channel numbers in the Channels field first, then name them.")
            return
        def on_save(names):
            self._ch_names = names
            # Refresh treeview Ch column to reflect new names
            for row in self._rows:
                iid = row.get("iid")
                if iid and self.tree.exists(iid):
                    vals = list(self.tree.item(iid, "values"))
                    vals[1] = self._ch_name(row["ch"])
                    self.tree.item(iid, values=vals)
            named = [f"Ch {ch} → {name}" for ch, name in names.items()]
            if named:
                self._q.put(("log", "Channel names saved: " + "  |  ".join(named), "ok"))
            else:
                self._q.put(("log", "Channel names cleared — using defaults.", "info"))
        ChannelNamesDialog(self, channels, self._ch_names, on_save)

    # ── profile helpers ───────────────────────────────────────────────────
    def _load_profiles(self):
        try:
            with open(PROFILES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _persist_profiles(self):
        try:
            with open(PROFILES_FILE, "w") as f:
                json.dump(self._profiles, f, indent=2)
        except Exception as e:
            messagebox.showerror("Profile save error", str(e))

    def _refresh_profile_combo(self):
        names = sorted(self._profiles.keys())
        self.cmb_profile["values"] = names

    def _on_profile_select(self, _event=None):
        name = self.v_profile.get()
        p = self._profiles.get(name)
        if not p:
            return
        self.v_host.set(p.get("host", ""))
        self.v_port.set(p.get("port", "8080"))
        self.v_user.set(p.get("user", "admin"))
        self.v_pass.set(p.get("password", ""))
        if p.get("channels"):
            self.v_channels.set(p["channels"])
        if p.get("save_dir"):
            self.v_savedir.set(p["save_dir"])
        self._q.put(("log", f"Profile loaded: {name}", "info"))

    def _save_profile(self):
        name = simpledialog.askstring(
            "Save Profile", "Profile name:",
            initialvalue=self.v_profile.get() or "")
        if not name:
            return
        name = name.strip()
        if not name:
            return
        self._profiles[name] = {
            "host":     self.v_host.get().strip(),
            "port":     self.v_port.get().strip(),
            "user":     self.v_user.get().strip(),
            "password": self.v_pass.get(),
            "channels": self.v_channels.get().strip(),
            "save_dir": self.v_savedir.get().strip(),
        }
        self._persist_profiles()
        self._refresh_profile_combo()
        self.v_profile.set(name)
        self._q.put(("log", f"Profile saved: {name}", "ok"))

    def _delete_profile(self):
        name = self.v_profile.get()
        if not name or name not in self._profiles:
            messagebox.showinfo("No profile", "Select a profile to delete.")
            return
        if not messagebox.askyesno("Delete profile",
                f"Delete profile '{name}'?"):
            return
        del self._profiles[name]
        self._persist_profiles()
        self._refresh_profile_combo()
        self.v_profile.set("")
        self._q.put(("log", f"Profile deleted: {name}", "info"))

    # ── helpers ───────────────────────────────────────────────────────────
    def _toggle_convert(self):
        if self.v_convert.get():
            self.cb_keepraw.config(state="normal")
        else:
            self.cb_keepraw.config(state="disabled")
            self.v_keepraw.set(False)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.v_savedir.get() or "/")
        if d:
            self.v_savedir.set(d)

    def _parse_channels(self):
        return [int(c.strip())
                for c in self.v_channels.get().split(",")
                if c.strip().isdigit()]

    def _fmt_dur(self, s, e):
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            sec = int((datetime.strptime(e, fmt) - datetime.strptime(s, fmt)).total_seconds())
            return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"
        except Exception:
            return "—"

    def _update_sel_label(self):
        total   = len(self._rows)
        sel_cnt = sum(1 for r in self._rows if self._checked.get(r["iid"], True))
        sel_mb  = sum(r["seg"].size_mb for r in self._rows
                      if self._checked.get(r["iid"], True))
        if total == 0:
            self.lbl_sel.config(text="No recordings loaded", foreground="#888888")
            self.btn_dl.config(state="disabled", text="Download Selected")
            self.btn_export_csv.config(state="disabled")
        else:
            self.lbl_sel.config(
                text=f"{sel_cnt} of {total} selected  (~{sel_mb:.0f} MB)",
                foreground="#333333")
            self.btn_dl.config(
                text=f"Download {sel_cnt} Selected",
                state="normal" if sel_cnt > 0 else "disabled")
            self.btn_export_csv.config(state="normal")

    def _quick_fill(self, days_ago):
        """Fill start=end=today-days_ago, or start=today-days_ago, end=today for last7d."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        self.v_start_date.set(target)
        self.v_end_date.set(today if days_ago > 1 else target)

    def _parse_dates(self):
        """Returns (start_dt, end_dt) as timezone-aware UTC datetimes, or raises ValueError."""
        sd = datetime.strptime(self.v_start_date.get().strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        ed = datetime.strptime(self.v_end_date.get().strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if ed < sd:
            raise ValueError("End date must be on or after start date.")
        return sd, ed + timedelta(days=1)   # end is inclusive → add 1 day for API

    # ── treeview selection + detail panel ────────────────────────────────
    def _tree_select(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        row = next((r for r in self._rows if r.get("iid") == iid), None)
        if not row:
            return
        self._sel_row = row
        self.btn_play.config(state="normal")
        seg = row["seg"]
        ch  = row["ch"]

        # Fill detail info
        self._detail_vars["Channel"].set(self._ch_name(ch))
        self._detail_vars["Start"].set(seg.start_time or "—")
        self._detail_vars["End"].set(seg.end_time or "—")
        self._detail_vars["Duration"].set(
            self._fmt_dur(seg.start_time or "", seg.end_time or ""))
        self._detail_vars["Size"].set(
            f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?")

        # Check if file exists locally
        save_dir = self.v_savedir.get().strip()
        safe = (seg.start_time or "unknown").replace(":", "-").replace("T", "_").rstrip("Z")[:19]
        date_folder = (seg.start_time or "unknown")[:10]
        ps  = os.path.join(save_dir, date_folder, f"ch{ch}", f"ch{ch}_{safe}.ps")
        mp4 = os.path.splitext(ps)[0] + ".mp4"
        if os.path.exists(mp4):
            self._detail_vars["Local file"].set(f"✓ MP4  {mp4}")
        elif os.path.exists(ps):
            sz = os.path.getsize(ps)
            self._detail_vars["Local file"].set(
                f"✓ .ps  {sz/1_048_576:.1f} MB  {ps}")
        else:
            self._detail_vars["Local file"].set("Not downloaded")

        # Fetch thumbnail in background
        self._thumb_label.config(image="", text="Loading…", fg="#aaaaaa")
        self._thumb_img = None
        if self.client:
            threading.Thread(
                target=self._fetch_thumb_worker,
                args=(ch, seg.start_time),
                daemon=True).start()

    def _fetch_thumb_worker(self, ch, start_time):
        """Fetch a JPEG snapshot from the NVR for this channel/time."""
        channel_id = ch * 100 + 1   # Hikvision: Ch 2 → 201

        # Try historical snapshot first (recording time), then live
        endpoints = []
        if start_time:
            t = start_time.rstrip("Z") if start_time.endswith("Z") else start_time
            endpoints.append(
                f"/ISAPI/ContentMgmt/StreamingProxy/channels/{channel_id}"
                f"/picture?time={t}Z")
        endpoints.append(f"/ISAPI/Streaming/channels/{channel_id}/picture")

        jpeg_data = None
        for ep in endpoints:
            try:
                req = urllib.request.Request(
                    self.client.base_url + ep,
                    headers={"User-Agent": "OpenHik-Client/1.0"})
                with self.client.opener.open(req, timeout=8) as resp:
                    jpeg_data = resp.read()
                if jpeg_data and len(jpeg_data) > 100:
                    break
            except Exception:
                jpeg_data = None
                continue

        if not jpeg_data:
            self._q.put(("thumb_err",))
            return

        if not _PIL_OK:
            # Show file-size as indication it worked
            self._q.put(("thumb_err",))
            return

        try:
            img = Image.open(_pil_io.BytesIO(jpeg_data))
            img.thumbnail((160, 100), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._q.put(("thumb_ready", photo))
        except Exception:
            self._q.put(("thumb_err",))

    def _on_thumb_ready(self, photo):
        self._thumb_img = photo   # prevent GC
        self._thumb_label.config(image=photo, text="")

    # ── play ──────────────────────────────────────────────────────────────
    # ------------------------------------------------------------------
    # MP4 conversion helper
    # ------------------------------------------------------------------
    def _ps_to_mp4(self, ps_path: str, mp4_path: str) -> bool:
        """Convert a Hikvision .ps file to .mp4 using ffmpeg.

        Hikvision MPEG-PS files need the demuxer forced with -f mpegps.
        We try twice:
          Pass 1 — copy video + transcode audio to AAC (handles most files)
          Pass 2 — copy video + no audio (if audio causes an error)
        Returns True on success, False if ffmpeg is not found or both
        passes fail.
        """
        if not ps_path or not os.path.exists(ps_path):
            return False

        base_cmd = ["ffmpeg", "-y", "-f", "mpeg", "-i", ps_path]

        passes = [
            base_cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", mp4_path],
            base_cmd + ["-c:v", "copy", "-an", mp4_path],
        ]

        for attempt, cmd in enumerate(passes, 1):
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
                if result.returncode == 0 and os.path.exists(mp4_path):
                    return True
                # Log stderr for diagnosis (last 20 lines)
                err_tail = (result.stderr or b"").decode(errors="replace")
                err_lines = [l for l in err_tail.splitlines() if l.strip()][-20:]
                self._q.put(("log",
                    f"    ffmpeg pass {attempt} failed (rc={result.returncode}):",
                    "warn"))
                for line in err_lines:
                    self._q.put(("log", f"      {line}", "warn"))
            except FileNotFoundError:
                self._q.put(("log",
                    "    ffmpeg not found — install it and add to PATH", "warn"))
                return False
            except subprocess.TimeoutExpired:
                self._q.put(("log", "    ffmpeg timed out", "warn"))
                return False

        return False

    def _do_play(self):
        row = self._sel_row
        if not row:
            return
        seg = row["seg"]
        ch  = row["ch"]
        save_dir = self.v_savedir.get().strip()
        safe = (seg.start_time or "unknown").replace(":", "-").replace("T", "_").rstrip("Z")[:19]
        date_folder = (seg.start_time or "unknown")[:10]
        ps  = os.path.join(save_dir, date_folder, f"ch{ch}", f"ch{ch}_{safe}.ps")
        mp4 = os.path.splitext(ps)[0] + ".mp4"

        # Prefer MP4, then .ps
        target = mp4 if os.path.exists(mp4) else ps if os.path.exists(ps) else None

        if target:
            try:
                if sys.platform == "win32":
                    os.startfile(target)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", target])
                else:
                    subprocess.Popen(["xdg-open", target])
                self._q.put(("log", f"Opening: {target}", "info"))
            except Exception as e:
                messagebox.showerror("Playback error", str(e))
        else:
            # Not downloaded — offer to open stream in VLC
            uri = seg.playback_uri if hasattr(seg, "playback_uri") else None
            if uri and messagebox.askyesno(
                    "File not downloaded",
                    "This recording hasn't been downloaded yet.\n\n"
                    "Try to open the NVR stream directly in VLC?"):
                try:
                    subprocess.Popen(["vlc", uri])
                    self._q.put(("log", f"Opening stream in VLC: {uri}", "info"))
                except FileNotFoundError:
                    messagebox.showinfo(
                        "VLC not found",
                        f"Download the recording first, or open this URI manually in VLC:\n\n{uri}")
            elif not uri:
                messagebox.showinfo("Not downloaded",
                    "Download this recording first before playing.")

    # ── treeview checkbox ─────────────────────────────────────────────────
    def _tree_click(self, event):
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if col == "#1" and iid:
            new = not self._checked.get(iid, True)
            self._checked[iid] = new
            vals = list(self.tree.item(iid, "values"))
            vals[0] = CHK_ON if new else CHK_OFF
            self.tree.item(iid, values=vals, tags=(("on" if new else "off"),))
            self._update_sel_label()

    def _select_all(self):
        for r in self._rows:
            iid = r["iid"]
            self._checked[iid] = True
            vals = list(self.tree.item(iid, "values"))
            vals[0] = CHK_ON
            self.tree.item(iid, values=vals, tags=("on",))
        self._update_sel_label()

    def _select_none(self):
        for r in self._rows:
            iid = r["iid"]
            self._checked[iid] = False
            vals = list(self.tree.item(iid, "values"))
            vals[0] = CHK_OFF
            self.tree.item(iid, values=vals, tags=("off",))
        self._update_sel_label()

    # ── Connect ───────────────────────────────────────────────────────────
    def _do_connect(self):
        self.btn_connect.config(state="disabled")
        self.btn_search.config(state="disabled")
        self.btn_list_dates.config(state="disabled")
        self.btn_detect.config(state="disabled")
        self._q.put(("status", "Connecting…", "#e67e22"))
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            host = self.v_host.get().strip()
            port = int(self.v_port.get().strip())
            user = self.v_user.get().strip()
            pw   = self.v_pass.get()
            self.client = PureHikClient(host, port, user, pw)

            info = self.client.get_device_info()
            self._q.put(("status",
                f"●  {info.model}  |  FW {info.firmware}  |  SN {info.serial}",
                "#27ae60"))
            self._q.put(("log",
                f"Connected: {info.model}  FW={info.firmware}  SN={info.serial}", "ok"))

            try:
                st = self.client.get_device_status()
                self._q.put(("log",
                    f"Status: CPU {st.cpu_utilization}%  "
                    f"Uptime {st.uptime_seconds//3600}h  "
                    f"Time {st.current_time}", "info"))
            except Exception:
                pass

            try:
                disks = self.client.get_storage()
                total_gb = sum(d.capacity_gb for d in disks)
                free_gb  = sum(d.free_space_mb / 1024 for d in disks)
                used_gb  = total_gb - free_gb
                for d in disks:
                    self._q.put(("log",
                        f"Storage: {d.name}  {d.capacity_gb:.0f} GB  "
                        f"({d.free_space_mb/1024:.0f} GB free)  {d.status}", "info"))
                self._q.put(("storage_info", total_gb, used_gb))
            except Exception:
                pass

            self._q.put(("connected",))

        except Exception as e:
            self._q.put(("log", f"Connection failed: {e}", "err"))
            self._q.put(("status", f"Error: {e}", "#c0392b"))

        finally:
            self.after(0, lambda: self.btn_connect.config(state="normal"))

    # ── Detect Channels ───────────────────────────────────────────────────
    def _do_detect(self):
        self.btn_detect.config(state="disabled")
        self._q.put(("log", "Detecting channels …", "info"))
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self):
        """
        Try channels 1-32. For each, do a tiny 1-second search window; if it
        returns without error the channel exists. Collect all that respond.
        Uses the ISAPI /ContentMgmt/search endpoint via search_recordings().
        """
        found = []
        probe_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        probe_end   = probe_start + timedelta(seconds=1)

        for ch in range(1, 65):
            try:
                self.client.search_recordings(ch, probe_start, probe_end,
                                              max_results=1)
                found.append(ch)
                self._q.put(("log", f"  Channel {ch}: ✓ found", "ok"))
            except Exception as e:
                err = str(e).lower()
                # "no such channel", 400, 404 → channel doesn't exist; stop early
                if any(k in err for k in ("no such", "invalid", "404", "400")):
                    if ch > 4:   # give up after first gap past ch4
                        break
                # other errors (timeout, auth) → skip silently
        self._q.put(("detect_done", found))

    def _on_detect_done(self, found):
        self.btn_detect.config(state="normal")
        if not found:
            messagebox.showinfo("No channels found",
                "Could not detect any channels.\n"
                "Try entering channel numbers manually.")
            return
        ch_str = ",".join(str(c) for c in found)
        self.v_channels.set(ch_str)
        self._q.put(("log",
            f"Detected {len(found)} channel(s): {ch_str} — Channels field updated.",
            "ok"))
        if messagebox.askyesno("Name Channels",
                f"Found {len(found)} channel(s): {ch_str}.\n\n"
                "Would you like to give them friendly names now?\n"
                "(e.g. Ch 2 → Front Gate)"):
            self._open_name_channels()

    # ── List Available Dates ──────────────────────────────────────────────
    def _do_list_dates(self):
        channels = self._parse_channels()
        if not channels:
            messagebox.showerror("No channels", "Enter at least one channel number.")
            return

        self.btn_list_dates.config(state="disabled")
        self.btn_search.config(state="disabled")
        self._q.put(("log",
            f"Scanning all available recordings on channels {channels} …", "info"))

        threading.Thread(
            target=self._list_dates_worker, args=(channels,), daemon=True).start()

    def _list_dates_worker(self, channels):
        """
        Query each channel for ALL available recordings (no fixed day limit).
        Uses a start date far in the past so the NVR returns its full history.
        """
        today = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0)
        # Go back 10 years — NVR will simply return whatever it actually has
        start = datetime(today.year - 10, 1, 1, tzinfo=timezone.utc)
        end   = today + timedelta(days=1)

        dates_by_ch = {ch: set() for ch in channels}
        errors = []

        for ch in channels:
            self._q.put(("log", f"  Scanning channel {ch} …", "info"))
            try:
                segs = self.client.search_recordings(ch, start, end, max_results=10000)
                for seg in segs:
                    if seg.start_time:
                        try:
                            dates_by_ch[ch].add(seg.start_time[:10])
                        except Exception:
                            pass
                self._q.put(("log",
                    f"  Channel {ch}: {len(segs)} segment(s) across "
                    f"{len(dates_by_ch[ch])} date(s)", "info"))
            except Exception as e:
                errors.append(f"Ch {ch}: {e}")
                self._q.put(("log", f"  Channel {ch} error: {e}", "err"))

        if errors and all(len(v) == 0 for v in dates_by_ch.values()):
            self._q.put(("list_dates_err", "\n".join(errors)))
        else:
            self._q.put(("list_dates_done", dates_by_ch))

    def _on_list_dates_done(self, dates_by_ch):
        self.btn_list_dates.config(state="normal")
        self.btn_search.config(state="normal")

        total_dates = len({d for v in dates_by_ch.values() for d in v})
        self._q.put(("log",
            f"Found recordings on {total_dates} distinct date(s).",
            "ok"))

        if total_dates == 0:
            messagebox.showinfo("No recordings",
                "No recordings found for the specified channels.")
            return

        def on_pick(date_str):
            self.v_start_date.set(date_str)
            self.v_end_date.set(date_str)
            self._q.put(("log",
                f"Date set to {date_str} — click Search Recordings to load.", "info"))

        AvailableDatesDialog(self, dates_by_ch, on_pick, ch_names=self._ch_names)

    def _on_list_dates_err(self, msg):
        self.btn_list_dates.config(state="normal")
        self.btn_search.config(state="normal")
        messagebox.showerror("Error scanning dates", msg)

    # ── Search ────────────────────────────────────────────────────────────
    def _do_search(self):
        try:
            start_utc, end_utc = self._parse_dates()
        except ValueError as e:
            messagebox.showerror("Bad date", str(e))
            return

        channels = self._parse_channels()
        if not channels:
            messagebox.showerror("No channels", "Enter at least one channel number.")
            return

        self.start_utc = start_utc
        self.end_utc   = end_utc

        self.btn_search.config(state="disabled")
        self.btn_dl.config(state="disabled")
        self._clear_tree()
        self._rows = []

        span = f"{self.v_start_date.get()} → {self.v_end_date.get()}"
        self._q.put(("log",
            f"Searching {span} on channels {channels} …", "info"))

        threading.Thread(
            target=self._search_worker, args=(channels,), daemon=True).start()

    def _search_worker(self, channels):
        rows = []
        for ch in channels:
            try:
                segs = self.client.search_recordings(
                    ch, self.start_utc, self.end_utc, max_results=10000)
                self._q.put(("log",
                    f"Channel {ch}: {len(segs)} segment(s)  "
                    f"(~{sum(s.size_mb for s in segs):.0f} MB)", "info"))
                for seg in segs:
                    rows.append({"seg": seg, "ch": ch, "iid": None})
            except Exception as e:
                self._q.put(("log", f"Channel {ch} error: {e}", "err"))

        self._rows = rows
        self._q.put(("search_done",))

    def _on_search_done(self):
        self._clear_tree()
        self._checked = {}
        for row in self._rows:
            seg = row["seg"]
            dur  = self._fmt_dur(seg.start_time or "", seg.end_time or "")
            size = f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?"
            iid  = self.tree.insert(
                "", "end",
                values=(CHK_ON, self._ch_name(row["ch"]),
                        seg.start_time or "?",
                        seg.end_time   or "?",
                        dur, size),
                tags=("on",))
            row["iid"] = iid
            self._checked[iid] = True

        self._update_sel_label()
        self.btn_search.config(state="normal")

    def _clear_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._checked  = {}
        self._sel_row  = None
        self._thumb_img = None
        self._thumb_label.config(image="", text="Select a\nrecording", fg="#aaaaaa")
        for var in self._detail_vars.values():
            var.set("—")
        self.btn_play.config(state="disabled")
        self._update_sel_label()

    # ── Export CSV ────────────────────────────────────────────────────────
    def _export_csv(self):
        if not self._rows:
            messagebox.showinfo("Nothing to export", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save recording list",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"recordings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Channel", "Channel Name", "Start", "End",
                                 "Duration", "Size (MB)", "Playback URI"])
                for row in self._rows:
                    seg = row["seg"]
                    ch  = row["ch"]
                    writer.writerow([
                        ch,
                        self._ch_name(ch),
                        seg.start_time or "",
                        seg.end_time   or "",
                        self._fmt_dur(seg.start_time, seg.end_time),
                        f"{seg.size_mb:.2f}",
                        seg.playback_uri or "",
                    ])
            self._q.put(("log", f"CSV exported: {path}", "ok"))
            messagebox.showinfo("Exported", f"Saved {len(self._rows)} rows to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ── Download ──────────────────────────────────────────────────────────
    def _do_download(self):
        selected = [r for r in self._rows if self._checked.get(r["iid"], True)]
        if not selected:
            messagebox.showinfo("Nothing selected", "Tick at least one recording.")
            return
        save_dir = self.v_savedir.get().strip()
        if not save_dir:
            messagebox.showerror("No save folder", "Enter a destination folder.")
            return

        try:
            limit_mbps = float(self.v_limit.get()) * 8
        except ValueError:
            limit_mbps = 0.0

        self._stop.clear()
        self.btn_dl.config(state="disabled")
        self.btn_search.config(state="disabled")
        self.btn_connect.config(state="disabled")
        self.btn_list_dates.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pb_file["value"] = 0
        self.pb_overall["value"] = 0
        self.lbl_file.config(text="")
        self.lbl_overall.config(text="")

        threading.Thread(
            target=self._dl_worker,
            args=(selected, save_dir,
                  self.v_convert.get(), self.v_keepraw.get(),
                  limit_mbps, self.v_skip_existing.get()),
            daemon=True).start()

    def _dl_worker(self, selected, save_dir, convert, keep_raw, limit_mbps, skip_existing):
        total  = len(selected)
        done   = 0
        ok_ct  = err_ct = 0

        self._q.put(("log", f"=== Starting download: {total} segment(s) ===", "info"))

        for idx, row in enumerate(selected, 1):
            if self._stop.is_set():
                self._q.put(("log", "Stopped by user.", "warn"))
                break

            seg   = row["seg"]
            ch    = row["ch"]
            safe  = (seg.start_time or "unknown") \
                        .replace(":", "-").replace("T", "_").rstrip("Z")[:19]
            date_folder = (seg.start_time or "unknown")[:10]   # YYYY-MM-DD
            base  = f"ch{ch}_{safe}"
            d_dir = os.path.join(save_dir, date_folder, f"ch{ch}")
            ps    = os.path.join(d_dir, base + ".ps")
            mp4   = os.path.join(d_dir, base + ".mp4")
            final = mp4 if (convert and not keep_raw) else ps

            if os.path.exists(final) and os.path.getsize(final) > 0:
                m = re.search(r'[?&]size=(\d+)', seg.playback_uri)
                exp = int(m.group(1)) if m else 0
                already_complete = (exp == 0 or os.path.getsize(final) >= exp)
                if already_complete or skip_existing:
                    reason = "complete" if already_complete else "exists (skip)"
                    self._q.put(("log",
                        f"[{idx}/{total}] Skip ({reason}): {os.path.basename(final)}",
                        "info"))
                    ok_ct += 1
                    done += 1
                    self._q.put(("overall_prog", done, total))
                    continue

            resuming = (not skip_existing) and os.path.exists(ps) and os.path.getsize(ps) > 0
            action   = "Resuming" if resuming else "Downloading"
            size_s   = f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?"
            self._q.put(("log",
                f"[{idx}/{total}] {action}: {base}.ps  ({size_s})", "info"))

            m = re.search(r'[?&]size=(\d+)', seg.playback_uri)
            exp_bytes = int(m.group(1)) if m else 0

            def prog(total_bytes, speed, _exp=exp_bytes, _b=base):
                pct   = min(100, total_bytes / _exp * 100) if _exp else 0
                label = (f"{_b}.ps    "
                         f"{total_bytes/1_048_576:.1f} MB"
                         f"  @  {speed:.1f} MB/s")
                self._q.put(("file_prog", label, pct))

            os.makedirs(d_dir, exist_ok=True)

            MAX_RETRIES = 3
            RETRY_DELAY = 20   # seconds between attempts
            nb = None
            last_exc = None

            for attempt in range(1, MAX_RETRIES + 1):
                if self._stop.is_set():
                    break
                try:
                    nb = download_with_resume(
                        self.client, seg.playback_uri, ps,
                        progress_cb=prog,
                        limit_mbps=limit_mbps,
                        stop_event=self._stop)
                    last_exc = None
                    break  # success
                except (InterruptedError, DownloadIncompleteError):
                    last_exc = sys.exc_info()[1]
                    break   # don't retry these — handled below
                except Exception as e:
                    last_exc = e
                    if attempt < MAX_RETRIES:
                        self._q.put(("log",
                            f"    Network error (attempt {attempt}/{MAX_RETRIES}): {e}", "warn"))
                        # Countdown — log every 5 s, check stop every second
                        for remaining in range(RETRY_DELAY, 0, -1):
                            if self._stop.is_set():
                                break
                            if remaining % 5 == 0 or remaining <= 3:
                                self._q.put(("log",
                                    f"    Retrying in {remaining}s … (press Stop to cancel)",
                                    "warn"))
                            time.sleep(1)
                    else:
                        self._q.put(("log",
                            f"    Failed after {MAX_RETRIES} attempts: {e}", "err"))

            if self._stop.is_set():
                break

            try:
                if last_exc is not None:
                    raise last_exc

                self._q.put(("log",
                    f"    ✓  {nb/1_048_576:.1f} MB  →  {ps}", "ok"))
                self._q.put(("file_prog",
                    f"{base}.ps  ✓  {nb/1_048_576:.1f} MB", 100))

                if convert:
                    self._q.put(("log", "    Converting to MP4 …", "info"))
                    if self._ps_to_mp4(ps, mp4):
                        self._q.put(("log", f"    ✓  {mp4}", "ok"))
                        if not keep_raw:
                            os.remove(ps)
                    else:
                        self._q.put(("log",
                            "    ffmpeg failed — raw .ps kept", "warn"))

                ok_ct += 1

            except DownloadIncompleteError as e:
                self._q.put(("log",
                    f"    Partial saved (run again to resume): {e}", "warn"))
                err_ct += 1
            except InterruptedError:
                break
            except Exception as e:
                self._q.put(("log", f"    Error: {e}", "err"))
                if os.path.exists(ps) and os.path.getsize(ps) == 0:
                    try:
                        os.remove(ps)
                    except Exception:
                        pass
                err_ct += 1

            done += 1
            self._q.put(("overall_prog", done, total))

        self._q.put(("dl_done", ok_ct, err_ct))

    def _on_dl_done(self, ok, err):
        tag = "ok" if err == 0 else "warn"
        self._q.put(("log",
            f"=== Done: {ok} saved, {err} failed/incomplete ===", tag))
        self.btn_dl.config(state="normal")
        self.btn_search.config(state="normal")
        self.btn_connect.config(state="normal")
        self.btn_list_dates.config(state="normal")
        self.btn_detect.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _do_stop(self):
        self._stop.set()
        self.btn_stop.config(state="disabled")
        self._q.put(("log", "Stop requested — finishing current file…", "warn"))


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
