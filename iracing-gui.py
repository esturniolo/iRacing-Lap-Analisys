"""
iracing-gui.py
--------------
All-in-one GUI for iRacing lap telemetry:
  1. Processes .ibt files from a user-selected telemetry folder
     (uses iracing-laps.py logic — must be in the same directory)
  2. Saves per-track/car CSVs to a user-selected output folder
  3. Visualises the resulting data (table + 4 chart types)

Requirements:
    pip install customtkinter matplotlib
"""

# matplotlib backend MUST be set before any pyplot import
import matplotlib
matplotlib.use("TkAgg")

import csv
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.ticker import FuncFormatter

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ──────────────────────────────────────────────
# iracing-laps.py importer
# ──────────────────────────────────────────────

def _import_iracing_laps():
    """
    Imports parse_ibt, export_csv_split, load_registry, save_registry
    from iracing-laps.py (same directory as this script).
    Returns the module, or None if not found.
    """
    import importlib.util
    script_dir = Path(__file__).parent
    laps_path  = script_dir / "iracing-laps.py"
    if not laps_path.exists():
        return None
    spec   = importlib.util.spec_from_file_location("iracing_laps", laps_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class LapRow:
    lap_num: int                    # relative lap counter (column "lap")
    lap_time_str: str               # original string: "2:42.385", "31.274s", "INCOMPLETE"
    lap_time_sec: Optional[float]   # parsed to seconds; None for INCOMPLETE
    offtracks: int
    in_pit: bool
    sector_times: list = field(default_factory=list)  # [Optional[float], ...] in seconds


@dataclass
class SessionData:
    csv_file: str           # source CSV basename
    date: str               # "YYYY-MM-DD HH:MM"
    car: str
    track: str
    session_type: str       # "PRACTICE", "RACE", "OFFLINE TESTING", etc.
    laps: list = field(default_factory=list)        # list[LapRow]
    best_lap_sec: Optional[float] = None
    best_lap_idx: Optional[int] = None              # index into self.laps
    clean_laps: list = field(default_factory=list)  # list[LapRow], no pit / no outliers
    avg_clean_sec: Optional[float] = None

    def label(self) -> str:
        # Keep date+time, truncate track and car so they fit in the ~220 px listbox
        time_part  = self.date[11:16] if len(self.date) > 10 else self.date  # "HH:MM"
        date_part  = self.date[:10]                                           # "YYYY-MM-DD"
        track_s    = (self.track[:24] + "…") if len(self.track) > 25 else self.track
        car_s      = (self.car[:19] + "…")   if len(self.car)   > 20 else self.car
        stype      = self.session_type[:3]    # "PRA", "RAC", "OFF", "QUA" …
        return f"{date_part} {time_part}  {stype}  │  {track_s}  │  {car_s}"


# ──────────────────────────────────────────────
# Parsing utilities
# ──────────────────────────────────────────────

def parse_lap_time(s: str) -> Optional[float]:
    """
    Converts a lap time string to float seconds.
        "2:42.385"  -> 162.385
        "31.274s"   -> 31.274
        "INCOMPLETE" -> None
    """
    if not s:
        return None
    s = s.strip()
    if s.upper() == "INCOMPLETE":
        return None
    # M:SS.mmm
    m = re.fullmatch(r'(\d+):(\d+\.\d+)', s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    # SS.mmms  (trailing lowercase s)
    m = re.fullmatch(r'(\d+\.\d+)s', s)
    if m:
        return float(m.group(1))
    # Bare float (future-proofing)
    try:
        return float(s)
    except ValueError:
        return None


def format_seconds(sec: float) -> str:
    """Converts float seconds to a display string for axis labels."""
    if sec <= 0:
        return ""
    minutes = int(sec // 60)
    remaining = sec % 60
    if minutes > 0:
        return f"{minutes}:{remaining:06.3f}"
    return f"{remaining:.3f}s"


def is_clean_lap(lap: LapRow, best_sec: float, outlier_factor: float = 2.5) -> bool:
    """
    A lap is 'clean' for averaging purposes when:
      - lap_time_sec is not None
      - in_pit is False
      - lap_time_sec <= best_sec * outlier_factor  (excludes parking laps)
    """
    if lap.lap_time_sec is None:
        return False
    if lap.in_pit:
        return False
    return lap.lap_time_sec <= best_sec * outlier_factor


def load_folder(folder_path: str) -> list:
    """
    Scans folder_path for *.csv files.
    Groups rows by (csv_file, date, session_type) to produce distinct sessions.
    Returns list[SessionData] sorted by date descending (newest first).
    """
    folder = Path(folder_path)
    raw: dict[tuple, dict] = {}

    for fpath in sorted(folder.glob("*.csv")):
        try:
            with open(fpath, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Guard against repeated header rows in appended files
                    if row.get("date", "").strip() == "date":
                        continue
                    date         = row.get("date", "").strip()
                    session_type = row.get("session_type", "").strip()
                    key = (fpath.name, date, session_type)

                    if key not in raw:
                        raw[key] = {
                            "csv_file":     fpath.name,
                            "date":         date,
                            "car":          row.get("car", "").strip(),
                            "track":        row.get("track", "").strip(),
                            "session_type": session_type,
                            "laps":         [],
                        }

                    lt_sec = parse_lap_time(row.get("lap_time", ""))
                    # Read sector columns (sector1, sector2, ...) if present in this CSV
                    sector_times = []
                    for si in range(1, 30):   # up to 29 sectors (more than any real circuit)
                        key_s = f"sector{si}"
                        if key_s not in reader.fieldnames:
                            break
                        val = row.get(key_s, "").strip()
                        sector_times.append(parse_lap_time(val) if val else None)
                    raw[key]["laps"].append(LapRow(
                        lap_num      = int(row.get("lap", 0)),
                        lap_time_str = row.get("lap_time", "").strip(),
                        lap_time_sec = lt_sec,
                        offtracks    = int(row.get("offtracks", 0)),
                        in_pit       = row.get("in_pit", "0").strip() == "1",
                        sector_times = sector_times,
                    ))
        except Exception:
            continue  # skip malformed files silently

    result = []
    for data in raw.values():
        laps = data["laps"]
        valid_times = [l.lap_time_sec for l in laps
                       if l.lap_time_sec is not None and not l.in_pit]
        best_sec = min(valid_times) if valid_times else None
        best_idx = next(
            (i for i, l in enumerate(laps)
             if l.lap_time_sec == best_sec and not l.in_pit),
            None
        ) if best_sec is not None else None

        clean = [l for l in laps if is_clean_lap(l, best_sec)] if best_sec else []
        avg   = (sum(l.lap_time_sec for l in clean) / len(clean)) if clean else None

        result.append(SessionData(
            csv_file      = data["csv_file"],
            date          = data["date"],
            car           = data["car"],
            track         = data["track"],
            session_type  = data["session_type"],
            laps          = laps,
            best_lap_sec  = best_sec,
            best_lap_idx  = best_idx,
            clean_laps    = clean,
            avg_clean_sec = avg,
        ))

    result.sort(key=lambda s: s.date, reverse=True)
    return result


# ──────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────

def configure_treeview_style() -> None:
    """Apply a dark theme to ttk.Treeview (CTk does not do this automatically)."""
    style = ttk.Style()
    style.theme_use("default")
    style.configure("Treeview",
        background    = "#2b2b2b",
        foreground    = "white",
        fieldbackground = "#2b2b2b",
        bordercolor   = "#3d3d3d",
        rowheight     = 24,
        font          = ("", 10),
    )
    style.configure("Treeview.Heading",
        background = "#3a3a3a",
        foreground = "white",
        relief     = "flat",
        font       = ("", 10, "bold"),
    )
    style.map("Treeview",
        background = [("selected", "#1f538d")],
        foreground = [("selected", "white")],
    )
    style.map("Treeview.Heading",
        background = [("active", "#4d4d4d")],
    )


def make_session_listbox(parent, selectmode=tk.SINGLE):
    """
    Creates a styled dark tk.Listbox with a scrollbar.
    Returns (outer_frame, listbox).
    """
    frame = tk.Frame(parent, bg="#2b2b2b")
    scrollbar = tk.Scrollbar(frame, orient=tk.VERTICAL, bg="#3d3d3d",
                             troughcolor="#2b2b2b", highlightthickness=0)
    listbox = tk.Listbox(
        frame,
        selectmode          = selectmode,
        yscrollcommand      = scrollbar.set,
        bg                  = "#2b2b2b",
        fg                  = "white",
        selectbackground    = "#1f538d",
        selectforeground    = "white",
        activestyle         = "none",
        font                = ("", 10),
        borderwidth         = 0,
        highlightthickness  = 1,
        highlightbackground = "#3d3d3d",
        relief              = tk.FLAT,
    )
    scrollbar.config(command=listbox.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return frame, listbox


# ──────────────────────────────────────────────
# Track map helpers
# ──────────────────────────────────────────────

def track_to_filename(track: str) -> str:
    """Normalises a track name to a safe PNG filename. e.g. 'Autodromo Nazionale Monza Combined' → 'autodromo-nazionale-monza-combined.png'"""
    import unicodedata
    s = unicodedata.normalize("NFKD", track).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s + ".png"


def _trackmaps_dir() -> Path:
    """Returns the trackmaps/ folder next to this script."""
    return Path(__file__).parent / "trackmaps"


# ──────────────────────────────────────────────
# Tab 1 — Table
# ──────────────────────────────────────────────

class TableTab:
    def __init__(self, parent):
        self.sessions: list = []
        self._build(parent)

    def _build(self, parent):
        # ── Horizontal PanedWindow: left (sessions) │ right (table + map) ──
        h_pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL,
                                sashwidth=5, sashrelief=tk.FLAT,
                                bg="#3d3d3d", bd=0)
        h_pane.pack(fill=tk.BOTH, expand=True)

        # ── Left panel — session list ──
        left = ctk.CTkFrame(h_pane, corner_radius=6)
        h_pane.add(left, minsize=150, width=270)

        ctk.CTkLabel(left, text="Sessions", font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(8, 4))

        lb_frame, self._session_lb = make_session_listbox(left, selectmode=tk.SINGLE)
        lb_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self._session_lb.bind("<<ListboxSelect>>", self._on_session_select)

        # ── Right panel — vertical PanedWindow: table (top) │ track map (bottom) ──
        right = ctk.CTkFrame(h_pane, corner_radius=6)
        h_pane.add(right, minsize=400)

        v_pane = tk.PanedWindow(right, orient=tk.VERTICAL,
                                sashwidth=5, sashrelief=tk.FLAT,
                                bg="#3d3d3d", bd=0)
        v_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Table frame (top)
        table_outer = tk.Frame(v_pane, bg="#2b2b2b")
        v_pane.add(table_outer, minsize=120)

        # Treeview — inside table_outer
        tree_frame = tk.Frame(table_outer, bg="#2b2b2b")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        # Initial columns (no sectors yet — rebuilt dynamically on session select)
        columns = ("num", "lap", "time", "offtrack", "pit", "_spacer")  # temporary; rebuilt by _apply_tree_columns
        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                   selectmode="browse")
        self._tree_frame    = tree_frame   # kept for vsb re-packing if needed
        self._current_nsectors = 0         # track how many sector columns are active

        self._apply_tree_columns(0)        # build with 0 sectors

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._tree.tag_configure("best", foreground="#f0c040",
                                  font=("", 10, "bold"))
        self._tree.tag_configure("pit",  foreground="#888888")

        # Stats bar (still inside table_outer, below the treeview)
        stats = tk.Frame(table_outer, bg="#1e1e2e", height=48)
        stats.pack(fill=tk.X, side=tk.BOTTOM)
        stats.pack_propagate(False)

        self._stat_best  = self._stat_pair(stats, "Best lap")
        self._stat_avg   = self._stat_pair(stats, "Avg clean")
        self._stat_laps  = self._stat_pair(stats, "Total laps")
        self._stat_offt  = self._stat_pair(stats, "Off-tracks")

        # ── Track map panel (bottom of vertical pane) ──
        map_outer = tk.Frame(v_pane, bg="#1a1a2e")
        v_pane.add(map_outer, minsize=80, height=200)

        self._map_canvas = tk.Canvas(map_outer, bg="#1a1a2e",
                                     highlightthickness=0)
        self._map_canvas.pack(fill=tk.BOTH, expand=True)
        self._map_canvas.bind("<Configure>", self._on_map_resize)

        self._map_track  = None   # current track name
        self._map_image  = None   # PhotoImage reference (prevent GC)

    def _stat_pair(self, parent, label: str):
        """Creates a label+value pair in the stats bar, returns the value label."""
        frame = tk.Frame(parent, bg="#1e1e2e")
        frame.pack(side=tk.LEFT, padx=18, pady=6)
        tk.Label(frame, text=label, font=("", 9),
                 fg="#aaaaaa", bg="#1e1e2e").pack(anchor="w")
        val = tk.Label(frame, text="—", font=("", 11, "bold"),
                       fg="white", bg="#1e1e2e")
        val.pack(anchor="w")
        return val

    def _apply_tree_columns(self, num_sectors: int) -> None:
        """
        Rebuilds Treeview column definitions to include num_sectors sector columns.
        Column order: # | Lap | S1..SN | Time | Off-track | Pit | (spacer)
        Called once at build time (0 sectors) and again when a session with a different
        sector count is selected.
        """
        sector_cols = tuple(f"s{i+1}" for i in range(num_sectors))
        # Sectors sit between Lap and Time
        columns = ("num", "lap") + sector_cols + ("time", "offtrack", "pit", "_spacer")
        self._tree.configure(columns=columns)

        # Fixed column definitions
        self._tree.heading("num",      text="#")
        self._tree.column ("num",      width=38,  anchor="center", stretch=False, minwidth=38)
        self._tree.heading("lap",      text="Lap")
        self._tree.column ("lap",      width=55,  anchor="center", stretch=False, minwidth=55)

        for i in range(num_sectors):
            col = f"s{i+1}"
            self._tree.heading(col, text=f"S{i+1}")
            self._tree.column(col, width=75, anchor="center", stretch=False, minwidth=60)

        self._tree.heading("time",     text="Time")
        self._tree.column ("time",     width=105, anchor="center", stretch=False, minwidth=105)
        self._tree.heading("offtrack", text="Off-track")
        self._tree.column ("offtrack", width=80,  anchor="center", stretch=False, minwidth=80)
        self._tree.heading("pit",      text="Pit")
        self._tree.column ("pit",      width=48,  anchor="center", stretch=False, minwidth=48)

        self._tree.heading("_spacer", text="")
        self._tree.column("_spacer", width=1, anchor="center", stretch=True, minwidth=1)

        self._current_nsectors = num_sectors

        # Re-register tag styles (reset on column rebuild)
        self._tree.tag_configure("best", foreground="#f0c040", font=("", 10, "bold"))
        self._tree.tag_configure("pit",  foreground="#888888")

    # ── Data update ──

    def update_sessions(self, sessions: list) -> None:
        self.sessions = sessions
        self._session_lb.delete(0, tk.END)
        for s in sessions:
            self._session_lb.insert(tk.END, s.label())
        self._clear_table()
        self._clear_stats()

    def _clear_table(self):
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _clear_stats(self):
        for lbl in (self._stat_best, self._stat_avg, self._stat_laps, self._stat_offt):
            lbl.config(text="—")

    # ── Event handlers ──

    def _on_session_select(self, _event=None):
        sel = self._session_lb.curselection()
        if not sel:
            return
        session = self.sessions[sel[0]]
        self._populate_table(session)
        self._populate_stats(session)

    def _populate_table(self, session: SessionData):
        self._clear_table()

        # Determine sector count for this session
        num_sectors = max((len(l.sector_times) for l in session.laps), default=0)
        if num_sectors != self._current_nsectors:
            self._apply_tree_columns(num_sectors)

        for i, lap in enumerate(session.laps):
            pit_str = "Yes" if lap.in_pit else ""

            # Sector time display values
            sector_vals = []
            for si in range(num_sectors):
                if len(lap.sector_times) > si and lap.sector_times[si] is not None:
                    sector_vals.append(format_seconds(lap.sector_times[si]))
                else:
                    sector_vals.append("")

            if i == session.best_lap_idx:
                tag = "best"
            elif lap.in_pit:
                tag = "pit"
            else:
                tag = ""

            self._tree.insert("", tk.END,
                values=(i + 1, lap.lap_num, *sector_vals,
                        lap.lap_time_str, lap.offtracks, pit_str, ""),
                tags=(tag,) if tag else ())

    def _populate_stats(self, session: SessionData):
        best_str   = format_seconds(session.best_lap_sec) if session.best_lap_sec else "—"
        avg_str    = format_seconds(session.avg_clean_sec) if session.avg_clean_sec else "—"
        total_offt = sum(l.offtracks for l in session.laps)

        self._stat_best.config(text=best_str)
        self._stat_avg.config(text=avg_str)
        self._stat_laps.config(text=str(len(session.laps)))
        self._stat_offt.config(text=str(total_offt))

        self._load_track_map(session.track)

    # ── Track map ──

    def _load_track_map(self, track: str):
        """Loads and displays the track map PNG, or shows track name as fallback."""
        self._map_track = track
        self._render_map()

    def _render_map(self):
        """Renders the current track map (or fallback text) onto the canvas."""
        canvas = self._map_canvas
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        if _PIL_AVAILABLE and self._map_track:
            img_path = _trackmaps_dir() / track_to_filename(self._map_track)
            if img_path.exists():
                try:
                    img = Image.open(img_path).convert("RGBA")
                    # Scale to fit canvas keeping aspect ratio
                    img_w, img_h = img.size
                    scale = min(w / img_w, h / img_h) * 0.92
                    new_w = max(1, int(img_w * scale))
                    new_h = max(1, int(img_h * scale))
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    self._map_image = ImageTk.PhotoImage(img)
                    canvas.create_image(w // 2, h // 2,
                                        anchor=tk.CENTER,
                                        image=self._map_image)
                    return
                except Exception:
                    pass

        # Fallback: track name + expected filename centred
        name = self._map_track or ""
        expected = track_to_filename(name) if name else ""
        lines = name
        if expected:
            lines = f"{name}\n\ntrackmaps/{expected}"
        canvas.create_text(w // 2, h // 2, text=lines,
                           fill="#555555", font=("", 12, "italic"),
                           justify=tk.CENTER, width=w - 20)

    def _on_map_resize(self, _event=None):
        """Re-renders the map when the canvas is resized."""
        self._render_map()


# ──────────────────────────────────────────────
# Tab 2 — Charts
# ──────────────────────────────────────────────

CHART_TYPES = [
    "Lap time evolution",
    "Session comparison",
    "Off-tracks per lap",
]

# Colour palette for multi-session overlays
_COLORS = [
    "#4e9af1", "#f07746", "#51b26e", "#e05c8b",
    "#a97df2", "#e0c050", "#4ecdc4", "#ff6b6b",
    "#b8e986", "#c9b9ff",
]


class ChartsTab:
    def __init__(self, parent):
        self.sessions: list = []
        self._hover_annot = None   # current floating annotation (cleared on each _plot)
        self._build(parent)

    def _build(self, parent):
        # ── Horizontal PanedWindow: left (controls) │ right (chart) ──
        h_pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL,
                                sashwidth=5, sashrelief=tk.FLAT,
                                bg="#3d3d3d", bd=0)
        h_pane.pack(fill=tk.BOTH, expand=True)

        # ── Left panel — controls ──
        left = ctk.CTkFrame(h_pane, corner_radius=6)
        h_pane.add(left, minsize=150, width=270)

        ctk.CTkLabel(left, text="Sessions (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(8, 4))

        lb_frame, self._session_lb = make_session_listbox(left, selectmode=tk.EXTENDED)
        lb_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 8))

        ctk.CTkLabel(left, text="Chart type",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10)
        self._chart_var = ctk.StringVar(value=CHART_TYPES[0])
        ctk.CTkComboBox(left, variable=self._chart_var, values=CHART_TYPES,
                        state="readonly", width=248
                        ).pack(padx=10, pady=(4, 10))

        ctk.CTkButton(left, text="Plot", command=self._plot,
                      font=ctk.CTkFont(size=13, weight="bold"), height=36
                      ).pack(padx=10, pady=(0, 10), fill=tk.X)

        # ── Right panel — matplotlib canvas ──
        right = ctk.CTkFrame(h_pane, corner_radius=6)
        h_pane.add(right, minsize=400)

        self._embed_matplotlib(right)

    def _embed_matplotlib(self, parent):
        self._fig = plt.figure(figsize=(9, 6), dpi=100)
        self._fig.patch.set_facecolor("#1e1e2e")

        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))

        toolbar_frame = tk.Frame(parent, bg="#1e1e2e")
        toolbar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        toolbar = NavigationToolbar2Tk(self._canvas, toolbar_frame)
        toolbar.config(background="#1e1e2e")
        toolbar.update()

        # Hover tooltip — connected once; checks chart type at runtime
        self._canvas.mpl_connect("motion_notify_event", self._on_hover)

    # ── Data update ──

    def update_sessions(self, sessions: list) -> None:
        self.sessions = sessions
        self._session_lb.delete(0, tk.END)
        for s in sessions:
            self._session_lb.insert(tk.END, s.label())

    # ── Plot dispatcher ──

    def _get_selected_sessions(self) -> list:
        indices = self._session_lb.curselection()
        return [self.sessions[i] for i in indices]

    def _plot(self):
        self._hover_annot = None   # clf() destroys all artists; reset reference
        self._fig.clf()
        ax = self._fig.add_subplot(111)
        ax.set_facecolor("#1e1e2e")
        self._style_ax(ax)

        selected = self._get_selected_sessions()
        if not selected:
            ax.text(0.5, 0.5, "No sessions selected.\nSelect one or more sessions and click Plot.",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888", fontsize=12, linespacing=1.8)
            self._canvas.draw()
            return

        chart_type = self._chart_var.get()
        dispatch = {
            CHART_TYPES[0]: self._chart_lap_time_evolution,
            CHART_TYPES[1]: self._chart_session_comparison,
            CHART_TYPES[2]: self._chart_offtracks_per_lap,
        }
        dispatch[chart_type](ax, selected)

        self._fig.tight_layout(pad=1.5)
        self._canvas.draw()

    # ── Axis styling helper ──

    def _style_ax(self, ax):
        ax.tick_params(colors="white", labelsize=9)
        ax.xaxis.label.set_color("white")
        ax.yaxis.label.set_color("white")
        ax.title.set_color("white")
        for spine in ax.spines.values():
            spine.set_edgecolor("#444444")

    # ── Hover tooltip ──

    def _on_hover(self, event):
        """Shows a floating tooltip when the cursor is near a data point."""
        # Only active when an axes exists and the chart is "Lap time evolution"
        if not self._fig.axes:
            return
        ax = self._fig.axes[0]
        if event.inaxes != ax or self._chart_var.get() != CHART_TYPES[0]:
            if self._hover_annot and self._hover_annot.get_visible():
                self._hover_annot.set_visible(False)
                self._canvas.draw_idle()
            return

        found = False
        for line in ax.get_lines():
            # Skip internal lines (legend lines, grid, etc.)
            if line.get_label().startswith("_"):
                continue
            cont, ind = line.contains(event)
            if cont and len(ind["ind"]) > 0:
                idx = ind["ind"][0]
                x   = line.get_xdata()[idx]
                y   = line.get_ydata()[idx]
                # Skip NaN points (INCOMPLETE laps)
                try:
                    if y != y:   # NaN check
                        continue
                except TypeError:
                    continue

                if self._hover_annot is None:
                    self._hover_annot = ax.annotate(
                        "",
                        xy=(x, y), xytext=(14, 14),
                        textcoords="offset points",
                        bbox=dict(boxstyle="round,pad=0.45", fc="#1e1e2e",
                                  ec="#666666", lw=0.8, alpha=0.95),
                        arrowprops=dict(arrowstyle="->", color="#888888", lw=0.8),
                        color="white", fontsize=9, zorder=10,
                    )

                self._hover_annot.xy = (x, y)
                self._hover_annot.set_text(f"Lap {int(x)}\n{format_seconds(y)}")
                self._hover_annot.set_visible(True)
                found = True
                break

        if not found and self._hover_annot and self._hover_annot.get_visible():
            self._hover_annot.set_visible(False)

        self._canvas.draw_idle()

    # ── Chart: Lap time evolution ──

    def _chart_lap_time_evolution(self, ax, selected: list):
        time_formatter = FuncFormatter(lambda val, _: format_seconds(val) if val > 0 else "")
        ax.yaxis.set_major_formatter(time_formatter)

        for i, session in enumerate(selected):
            xs = [l.lap_num for l in session.laps]
            ys = [l.lap_time_sec if l.lap_time_sec else float("nan")
                  for l in session.laps]
            short = f"{session.date}  {session.session_type}"
            ax.plot(xs, ys, marker="o", markersize=4, linewidth=1.8,
                    label=short, color=_COLORS[i % len(_COLORS)])

        # Y-range: best_lap ± 5%.
        # Pit laps and outliers are drawn but clipped outside the visible area.
        best_times = [s.best_lap_sec for s in selected if s.best_lap_sec]
        if best_times:
            overall_best = min(best_times)
            margin = overall_best * 0.05
            ax.set_ylim(overall_best - margin, overall_best + margin)

        ax.set_xlabel("Lap number")
        ax.set_ylabel("Lap time")
        ax.set_title("Lap time evolution")
        ax.legend(fontsize=8, facecolor="#2b2b2b", labelcolor="white",
                  edgecolor="#444444", framealpha=0.9)
        ax.grid(True, linestyle="--", linewidth=0.4, color="#444444", alpha=0.6)

    # ── Chart: Session comparison ──

    def _chart_session_comparison(self, ax, selected: list):
        labels = [f"{s.date}\n{s.session_type}" for s in selected]
        values = [s.best_lap_sec if s.best_lap_sec else 0 for s in selected]
        colors = [_COLORS[i % len(_COLORS)] for i in range(len(selected))]

        bars = ax.bar(labels, values, color=colors, width=0.5, edgecolor="#444444",
                      linewidth=0.8)

        time_formatter = FuncFormatter(lambda val, _: format_seconds(val) if val > 0 else "")
        ax.yaxis.set_major_formatter(time_formatter)

        for bar, sec in zip(bars, values):
            if sec:
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max(values) * 0.01,
                        format_seconds(sec),
                        ha="center", va="bottom", color="white", fontsize=9)

        # Narrow Y range so bars don't start from 0 (too compressed for similar times)
        valid = [v for v in values if v > 0]
        if valid:
            margin = (max(valid) - min(valid)) * 0.3 or max(valid) * 0.05
            ax.set_ylim(min(valid) - margin * 2, max(valid) + margin * 5)

        ax.set_ylabel("Best lap time")
        ax.set_title("Session comparison — best lap")
        ax.tick_params(axis="x", labelsize=8)
        ax.grid(True, axis="y", linestyle="--", linewidth=0.4,
                color="#444444", alpha=0.6)

    # ── Chart: Off-tracks per lap ──

    def _chart_offtracks_per_lap(self, ax, selected: list):
        import numpy as np

        n = len(selected)
        # Build a common lap-number axis spanning all sessions
        all_lap_nums = sorted({l.lap_num for s in selected for l in s.laps})
        if not all_lap_nums:
            return

        x = np.arange(len(all_lap_nums))
        width = max(0.1, 0.8 / n)

        for i, session in enumerate(selected):
            lap_dict = {l.lap_num: l.offtracks for l in session.laps}
            heights = [lap_dict.get(ln, 0) for ln in all_lap_nums]
            offset = (i - n / 2 + 0.5) * width
            short = f"{session.date}  {session.session_type}"
            ax.bar(x + offset, heights, width=width,
                   label=short, color=_COLORS[i % len(_COLORS)],
                   edgecolor="#222222", linewidth=0.5)

        ax.set_xticks(x)
        ax.set_xticklabels(all_lap_nums, fontsize=8)
        ax.yaxis.get_major_locator().set_params(integer=True)
        ax.set_xlabel("Lap number")
        ax.set_ylabel("Off-track count")
        ax.set_title("Off-tracks per lap")
        ax.legend(fontsize=8, facecolor="#2b2b2b", labelcolor="white",
                  edgecolor="#444444", framealpha=0.9)
        ax.grid(True, axis="y", linestyle="--", linewidth=0.4,
                color="#444444", alpha=0.6)


# ──────────────────────────────────────────────
# Main application
# ──────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("iRacing Lap Analyzer")
        self.geometry("1200x820")
        self.minsize(1000, 720)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        configure_treeview_style()

        self.sessions: list = []
        self._ibt_var        = ctk.StringVar()
        self._out_var        = ctk.StringVar()
        self._only_complete  = ctk.BooleanVar(value=True)
        self._processing     = False
        self._queue: queue.Queue = queue.Queue()

        self._build_top_bar()
        self._build_tabs()

    # ────────────────────────────────────────
    # Top bar  (two rows)
    # ────────────────────────────────────────

    def _build_top_bar(self):
        outer = ctk.CTkFrame(self, corner_radius=6)
        outer.pack(fill=tk.X, padx=10, pady=(10, 4))

        # ── Row 1: telemetry folder (.ibt) ──
        row1 = ctk.CTkFrame(outer, fg_color="transparent")
        row1.pack(fill=tk.X, padx=10, pady=(8, 2))

        ctk.CTkLabel(row1, text="Telemetry folder (.ibt):",
                     width=170, anchor="w").pack(side=tk.LEFT)
        ctk.CTkEntry(row1, textvariable=self._ibt_var, width=400,
                     placeholder_text="Folder with .ibt files…"
                     ).pack(side=tk.LEFT, padx=(0, 6))
        ctk.CTkButton(row1, text="Browse", width=80,
                      command=self._browse_ibt).pack(side=tk.LEFT)

        # ── Row 2: output folder + options + Process ──
        row2 = ctk.CTkFrame(outer, fg_color="transparent")
        row2.pack(fill=tk.X, padx=10, pady=(2, 8))

        ctk.CTkLabel(row2, text="Output folder (CSVs):",
                     width=170, anchor="w").pack(side=tk.LEFT)
        ctk.CTkEntry(row2, textvariable=self._out_var, width=400,
                     placeholder_text="Folder to save CSVs…"
                     ).pack(side=tk.LEFT, padx=(0, 6))
        ctk.CTkButton(row2, text="Browse", width=80,
                      command=self._browse_out).pack(side=tk.LEFT, padx=(0, 12))

        ctk.CTkCheckBox(row2, text="Only complete laps",
                        variable=self._only_complete).pack(side=tk.LEFT, padx=(0, 14))

        self._process_btn = ctk.CTkButton(
            row2, text="⚙  Process", width=120,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_processing,
        )
        self._process_btn.pack(side=tk.LEFT, padx=(0, 10))

        ctk.CTkButton(row2, text="Reload CSVs", width=110,
                      command=self._reload_csvs).pack(side=tk.LEFT)

        # ── Row 3: progress bar + status ──
        row3 = ctk.CTkFrame(outer, fg_color="transparent")
        row3.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._progress = ctk.CTkProgressBar(row3, width=340, height=12)
        self._progress.set(0)
        self._progress.pack(side=tk.LEFT, padx=(0, 12))

        self._status_label = ctk.CTkLabel(
            row3, text="Select a telemetry folder and click Process.",
            text_color="#aaaaaa", font=ctk.CTkFont(size=11),
        )
        self._status_label.pack(side=tk.LEFT)

    # ── Tabs ──

    def _build_tabs(self):
        tabview = ctk.CTkTabview(self, corner_radius=6)
        tabview.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._table_tab  = TableTab(tabview.add("  Table  "))
        self._charts_tab = ChartsTab(tabview.add("  Charts  "))

    # ────────────────────────────────────────
    # Folder pickers
    # ────────────────────────────────────────

    def _browse_ibt(self):
        d = filedialog.askdirectory(title="Select telemetry folder (.ibt files)")
        if d:
            self._ibt_var.set(d)
            # Auto-fill output folder with same path if empty
            if not self._out_var.get().strip():
                self._out_var.set(d)

    def _browse_out(self):
        d = filedialog.askdirectory(title="Select output folder (CSVs)")
        if d:
            self._out_var.set(d)

    # ────────────────────────────────────────
    # Processing (.ibt → CSVs) in background thread
    # ────────────────────────────────────────

    def _start_processing(self):
        ibt_dir = self._ibt_var.get().strip()
        out_dir = self._out_var.get().strip()

        if not ibt_dir:
            messagebox.showwarning("Missing folder",
                                   "Please select a telemetry folder (.ibt files).")
            return
        if not out_dir:
            messagebox.showwarning("Missing folder",
                                   "Please select an output folder for the CSVs.")
            return
        if not Path(ibt_dir).exists():
            messagebox.showerror("Not found", f"Folder not found:\n{ibt_dir}")
            return

        laps_mod = _import_iracing_laps()
        if laps_mod is None:
            messagebox.showerror(
                "iracing-laps.py not found",
                "iracing-laps.py must be in the same folder as iracing-gui.py.",
            )
            return

        ibt_files = sorted(Path(ibt_dir).glob("*.ibt"))
        if not ibt_files:
            self._set_status("No .ibt files found in the selected folder.", color="#f07746")
            return

        Path(out_dir).mkdir(parents=True, exist_ok=True)

        self._processing = True
        self._process_btn.configure(state="disabled", text="Processing…")
        self._progress.set(0)
        self._set_status(f"Processing 0 / {len(ibt_files)} files…")

        threading.Thread(
            target=self._process_worker,
            args=(laps_mod, ibt_files, out_dir),
            daemon=True,
        ).start()
        self.after(80, self._poll_queue)

    def _process_worker(self, laps_mod, files, out_dir):
        """Runs in background thread. Sends progress updates via self._queue."""
        only_complete = self._only_complete.get()
        sessions      = []
        n             = len(files)

        for idx, fpath in enumerate(files, 1):
            self._queue.put(("progress", idx, n, fpath.name))
            try:
                session = laps_mod.parse_ibt(str(fpath))
                if session:
                    sessions.append(session)
            except Exception as e:
                self._queue.put(("log", f"  ERROR {fpath.name}: {e}"))

        # Filter laps
        def filter_laps(laps):
            if only_complete:
                return [l for l in laps if l.lap_time and l.lap_time > 0]
            return laps

        laps_map = {s.filename: filter_laps(s.laps) for s in sessions}

        # Export CSVs
        try:
            laps_mod.export_csv_split(sessions, Path(out_dir), laps_map=laps_map)
        except Exception as e:
            self._queue.put(("log", f"  CSV export error: {e}"))

        self._queue.put(("done", len(sessions), sum(len(filter_laps(s.laps)) for s in sessions)))

    def _poll_queue(self):
        """Called every 80 ms from the main thread to drain the worker queue."""
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, idx, total, name = msg
                    self._progress.set(idx / total)
                    self._set_status(f"Processing {idx} / {total}  —  {name}")
                elif kind == "log":
                    print(msg[1])  # visible in terminal if launched from one
                elif kind == "done":
                    _, n_sessions, n_laps = msg
                    self._progress.set(1.0)
                    self._processing = False
                    self._process_btn.configure(state="normal", text="⚙  Process")
                    self._set_status(
                        f"Done — {n_sessions} session(s) processed, {n_laps} laps exported.",
                        color="#51b26e",
                    )
                    self._reload_csvs()
                    return  # stop polling
        except queue.Empty:
            pass
        if self._processing:
            self.after(80, self._poll_queue)

    # ────────────────────────────────────────
    # CSV reload (visualisation only)
    # ────────────────────────────────────────

    def _reload_csvs(self):
        out_dir = self._out_var.get().strip()
        if not out_dir:
            self._set_status("No output folder selected.", color="#f07746")
            return
        try:
            sessions = load_folder(out_dir)
            self._on_folder_loaded(sessions)
        except Exception as e:
            self._set_status(f"Error loading CSVs: {e}", color="#f07746")

    def _on_folder_loaded(self, sessions: list):
        self.sessions = sessions
        self._table_tab.update_sessions(sessions)
        self._charts_tab.update_sessions(sessions)
        n          = len(sessions)
        total_laps = sum(len(s.laps) for s in sessions)
        noun       = "session" if n == 1 else "sessions"
        self._set_status(f"{n} {noun} loaded — {total_laps} laps", color="white")

    def _set_status(self, text: str, color: str = "#aaaaaa"):
        self._status_label.configure(text=text, text_color=color)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
