"""
iracing-gui.py
--------------
All-in-one GUI for iRacing lap telemetry:
  1. Processes .ibt files from a user-selected telemetry folder
  2. Saves per-track/car CSVs to a user-selected output folder
     (default: ~/Documents/iRacing-Lap-Analysis)
  3. Supports loading individual .ibt files in-memory (no CSV written)
  4. Supports loading external .ibt folders (friends/team) in-memory
  5. Visualises the resulting data (table + charts)

Requirements:
    pip install customtkinter matplotlib tksheet
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
from tksheet import Sheet as TkSheet
import iracing_laps


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
    csv_file: str           # source CSV basename (or IBT filename for in-memory sessions)
    date: str               # "YYYY-MM-DD HH:MM"
    car: str
    track: str
    session_type: str       # "PRACTICE", "RACE", "OFFLINE TESTING", etc.
    laps: list = field(default_factory=list)        # list[LapRow]
    best_lap_sec: Optional[float] = None
    best_lap_idx: Optional[int] = None              # index into self.laps
    clean_laps: list = field(default_factory=list)  # list[LapRow], no pit / no outliers
    avg_clean_sec: Optional[float] = None
    source: str = "own"     # "own" = CSV on disk | "memory" = own IBT in-memory | "external" = friend/team IBT

    def label(self) -> str:
        # Keep date+time, truncate track and car so they fit in the ~220 px listbox
        time_part  = self.date[11:16] if len(self.date) > 10 else self.date  # "HH:MM"
        date_part  = self.date[:10]                                           # "YYYY-MM-DD"
        track_s    = (self.track[:24] + "…") if len(self.track) > 25 else self.track
        car_s      = (self.car[:19] + "…")   if len(self.car)   > 20 else self.car
        stype      = self.session_type[:3]    # "PRA", "RAC", "OFF", "QUA" …
        prefix = {"memory": "[MEM] ", "external": "[EXT] "}.get(self.source, "")
        return f"{prefix}{date_part} {time_part}  {stype}  │  {track_s}  │  {car_s}"


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


def _session_from_parsed(parsed, source: str = "memory") -> SessionData:
    """
    Converts a parsed iracing-laps SessionData (from parse_ibt) into a
    GUI SessionData object, so in-memory IBT sessions can be displayed
    alongside CSV-loaded sessions without writing anything to disk.
    """
    laps = []
    for lap in parsed.laps:
        lt_sec = lap.lap_time if (lap.lap_time and lap.lap_time > 0) else None
        laps.append(LapRow(
            lap_num      = lap.lap_num,
            lap_time_str = lap.lap_time_str(),
            lap_time_sec = lt_sec,
            offtracks    = lap.offtracks,
            in_pit       = lap.on_pit_road,
            sector_times = list(lap.sector_times),
        ))

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

    return SessionData(
        csv_file      = parsed.filename,
        date          = parsed.date,
        car           = parsed.car,
        track         = parsed.track,
        session_type  = parsed.session_type,
        laps          = laps,
        best_lap_sec  = best_sec,
        best_lap_idx  = best_idx,
        clean_laps    = clean,
        avg_clean_sec = avg,
        source        = source,
    )


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
# Tab 1 — Table
# ──────────────────────────────────────────────

# Colour constants for delta / sector highlighting
_COL_BETTER   = "#44cc44"   # faster than previous
_COL_WARN_LOW = "#f0c040"   # 1–250 ms slower
_COL_WARN_MID = "#f07030"   # 251–499 ms slower
_COL_WARN_HI  = "#dd2222"   # ≥ 500 ms slower
_COL_BEST_FG  = "#b44fdb"   # best lap foreground (purple)
_COL_PIT_FG   = "#888888"   # pit lap foreground (grey)
_COL_BEST_BG  = "#2a0f3a"   # best lap row background (dark purple tint)
_COL_PIT_BG   = "#252525"   # pit lap row background


def _delta_color(diff: float) -> str | None:
    """Returns a foreground colour for a time delta (seconds), or None if no colour."""
    if diff < -0.0005:
        return _COL_BETTER
    if diff <= 0.0005:
        return None        # essentially equal
    if diff <= 0.250:
        return _COL_WARN_LOW
    if diff < 0.500:
        return _COL_WARN_MID
    return _COL_WARN_HI


def _make_sheet(parent) -> TkSheet:
    """
    Creates and returns a configured TkSheet instance with our dark theme.
    The sheet is read-only (only copy binding enabled).
    """
    sheet = TkSheet(
        parent,
        theme="dark blue",
        show_row_index=False,
        show_top_left=False,
        default_column_width=80,
        default_header_height=28,
        default_row_height=24,
        font=("", 10, "normal"),
        header_font=("", 10, "bold"),
    )
    # Match the app background colours more closely
    sheet.change_theme("dark blue")
    sheet.set_options(
        table_bg="#2b2b2b",
        table_fg="white",
        table_grid_fg="#3d3d3d",
        header_bg="#3a3a3a",
        header_fg="white",
        header_grid_fg="#4d4d4d",
        table_selected_cells_bg="#1f538d",
        table_selected_cells_fg="white",
        vertical_scroll_background="#2b2b2b",
        horizontal_scroll_background="#2b2b2b",
    )
    sheet.enable_bindings("copy")
    return sheet


class TableTab:
    def __init__(self, parent):
        self.sessions: list = []
        self._build(parent)

    # ── Build ──

    def _build(self, parent):
        outer = tk.Frame(parent, bg="#1e1e1e")
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Sessions band (top) — two listboxes side by side ──
        band = tk.Frame(outer, bg="#1e1e1e")
        band.pack(fill=tk.X, padx=4, pady=(4, 2))

        # Left: own + memory sessions
        left_band = ctk.CTkFrame(band, corner_radius=4)
        left_band.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
        ctk.CTkLabel(left_band, text="Sessions",
                     font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(anchor="w", padx=8, pady=(4, 2))
        lb_frame, self._session_lb = make_session_listbox(left_band, selectmode=tk.SINGLE)
        self._session_lb.configure(height=3)
        lb_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
        self._session_lb.bind("<<ListboxSelect>>", self._on_session_select)

        # Right: external sessions
        right_band = ctk.CTkFrame(band, corner_radius=4)
        right_band.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 0))
        ctk.CTkLabel(right_band, text="External",
                     font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(anchor="w", padx=8, pady=(4, 2))
        ext_lb_frame, self._ext_lb = make_session_listbox(right_band, selectmode=tk.SINGLE)
        self._ext_lb.configure(height=3)
        ext_lb_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
        self._ext_lb.bind("<<ListboxSelect>>", self._on_ext_select)

        # ── Two-panel area: own | external ──
        h_pane = tk.PanedWindow(outer, orient=tk.HORIZONTAL,
                                sashwidth=5, sashrelief=tk.FLAT,
                                bg="#3d3d3d", bd=0)
        h_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        # Own panel
        own_pane = tk.Frame(h_pane, bg="#1e1e1e")
        h_pane.add(own_pane, minsize=300)
        self._sheet, self._stat_best, self._stat_avg, self._stat_laps, self._stat_offt = \
            self._build_table_panel(own_pane, label="My Session")
        self._current_nsectors = -1      # force column rebuild on first populate

        # External panel
        ext_pane = tk.Frame(h_pane, bg="#1e1e1e")
        h_pane.add(ext_pane, minsize=300)
        self._sheet_ext, self._stat_best_ext, self._stat_avg_ext, \
            self._stat_laps_ext, self._stat_offt_ext = \
            self._build_table_panel(ext_pane, label="External Session")
        self._current_nsectors_ext = -1

    def _build_table_panel(self, parent, label: str):
        """
        Builds one table panel (tksheet + stats bar) inside parent.
        Returns (sheet, stat_best, stat_avg, stat_laps, stat_offt).
        """
        # Header label
        ctk.CTkLabel(parent, text=label,
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color="#aaaaaa"
                     ).pack(anchor="w", padx=6, pady=(4, 2))

        # tksheet
        sheet = _make_sheet(parent)
        sheet.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 2))

        # Stats bar
        stats = tk.Frame(parent, bg="#1e1e2e", height=48)
        stats.pack(fill=tk.X, side=tk.BOTTOM)
        stats.pack_propagate(False)

        stat_best = self._stat_pair(stats, "Best lap")
        stat_avg  = self._stat_pair(stats, "Avg clean")
        stat_laps = self._stat_pair(stats, "Total laps")
        stat_offt = self._stat_pair(stats, "Off-tracks")

        return sheet, stat_best, stat_avg, stat_laps, stat_offt

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

    # ── Column setup ──

    @staticmethod
    def _build_headers(num_sectors: int) -> list:
        """Returns the list of column header strings for a given sector count."""
        return (["#", "Lap"]
                + [f"S{i+1}" for i in range(num_sectors)]
                + ["Time", "Delta", "Off-track", "Pit"])

    @staticmethod
    def _col_widths(num_sectors: int) -> list:
        """Returns a list of column widths matching _build_headers order."""
        return ([38, 55]
                + [75] * num_sectors
                + [105, 80, 80, 48])

    # ── Data update ──

    def update_sessions(self, sessions: list) -> None:
        self.sessions = sessions
        own = [s for s in sessions if s.source in ("own", "memory")]
        ext = [s for s in sessions if s.source == "external"]

        self._session_lb.delete(0, tk.END)
        for s in own:
            self._session_lb.insert(tk.END, s.label())

        self._ext_lb.delete(0, tk.END)
        for s in ext:
            self._ext_lb.insert(tk.END, s.label())

        self._clear_sheet(self._sheet)
        self._clear_sheet(self._sheet_ext)
        self._clear_stats(self._stat_best, self._stat_avg,
                          self._stat_laps, self._stat_offt)
        self._clear_stats(self._stat_best_ext, self._stat_avg_ext,
                          self._stat_laps_ext, self._stat_offt_ext)

    @staticmethod
    def _clear_sheet(sheet: TkSheet) -> None:
        sheet.set_sheet_data([], reset_col_positions=True, reset_highlights=True)

    @staticmethod
    def _clear_stats(*stat_labels) -> None:
        for lbl in stat_labels:
            lbl.config(text="—")

    # ── Event handlers ──

    def _on_session_select(self, _event=None):
        sel = self._session_lb.curselection()
        if not sel:
            return
        self._ext_lb.selection_clear(0, tk.END)
        own = [s for s in self.sessions if s.source in ("own", "memory")]
        if sel[0] >= len(own):
            return
        session = own[sel[0]]
        self._populate_table(session, self._sheet, "_current_nsectors")
        self._populate_stats(session,
                             self._stat_best, self._stat_avg,
                             self._stat_laps, self._stat_offt)

    def _on_ext_select(self, _event=None):
        sel = self._ext_lb.curselection()
        if not sel:
            return
        self._session_lb.selection_clear(0, tk.END)
        ext = [s for s in self.sessions if s.source == "external"]
        if sel[0] >= len(ext):
            return
        session = ext[sel[0]]
        self._populate_table(session, self._sheet_ext, "_current_nsectors_ext")
        self._populate_stats(session,
                             self._stat_best_ext, self._stat_avg_ext,
                             self._stat_laps_ext, self._stat_offt_ext)

    # ── Table population ──

    def _populate_table(self, session: SessionData,
                        sheet: TkSheet, nsectors_attr: str) -> None:
        """
        Fills `sheet` with data from `session`.
        nsectors_attr: name of the instance attribute tracking current sector count
        for this specific sheet (e.g. "_current_nsectors" or "_current_nsectors_ext").
        """
        num_sectors = max((len(l.sector_times) for l in session.laps), default=0)

        # Rebuild headers/widths if sector count changed
        current_ns = getattr(self, nsectors_attr)
        if num_sectors != current_ns:
            headers = self._build_headers(num_sectors)
            widths  = self._col_widths(num_sectors)
            sheet.headers(headers)
            sheet.set_column_widths(widths)
            setattr(self, nsectors_attr, num_sectors)

        # Build table data
        data = []
        prev_sec        = None
        prev_sectors    = None   # list[Optional[float]] from previous lap

        for i, lap in enumerate(session.laps):
            pit_str = "Yes" if lap.in_pit else ""

            # Sector display values (just the formatted time, colour applied separately)
            sector_vals = []
            for si in range(num_sectors):
                cur = lap.sector_times[si] if si < len(lap.sector_times) else None
                sector_vals.append(format_seconds(cur) if cur is not None else "")

            # Delta vs previous lap
            delta_str = ""
            if prev_sec is not None and lap.lap_time_sec is not None:
                diff = lap.lap_time_sec - prev_sec
                if diff < -0.0005:
                    delta_str = "−" + format_seconds(abs(diff))
                elif diff > 0.0005:
                    delta_str = "+" + format_seconds(diff)

            if lap.lap_time_sec is not None:
                prev_sec = lap.lap_time_sec

            row = ([i + 1, lap.lap_num]
                   + sector_vals
                   + [lap.lap_time_str, delta_str, lap.offtracks, pit_str])
            data.append(row)

        # Load data (reset highlights too)
        sheet.set_sheet_data(data, reset_col_positions=False, reset_highlights=True)

        # ── Apply colours ──
        # Column indices: 0=#, 1=Lap, 2..2+ns-1=sectors, 2+ns=Time, 2+ns+1=Delta
        sector_start = 2
        delta_col    = sector_start + num_sectors + 1   # after Time

        prev_sec     = None
        prev_sectors = None

        for i, lap in enumerate(session.laps):
            is_best = (i == session.best_lap_idx)
            is_pit  = lap.in_pit

            # Row-level background/foreground for best/pit
            if is_best:
                sheet.highlight_rows(
                    rows=[i], bg=_COL_BEST_BG, fg=_COL_BEST_FG,
                    redraw=False, overwrite=True)
            elif is_pit:
                sheet.highlight_rows(
                    rows=[i], bg=_COL_PIT_BG, fg=_COL_PIT_FG,
                    redraw=False, overwrite=True)

            # Delta cell colour (only if there's a valid value and not best/pit)
            if not is_best and not is_pit and prev_sec is not None and lap.lap_time_sec is not None:
                diff  = lap.lap_time_sec - prev_sec
                col_fg = _delta_color(diff)
                if col_fg:
                    sheet.highlight_cells(
                        row=i, column=delta_col,
                        fg=col_fg, redraw=False, overwrite=True)

            # Sector cell colours (individual per cell, even on best/pit rows)
            if prev_sectors is not None:
                for si in range(num_sectors):
                    cur  = lap.sector_times[si]  if si < len(lap.sector_times)  else None
                    prev = prev_sectors[si]       if si < len(prev_sectors)      else None
                    if cur is not None and prev is not None:
                        diff   = cur - prev
                        col_fg = _delta_color(diff)
                        if col_fg:
                            sheet.highlight_cells(
                                row=i, column=sector_start + si,
                                fg=col_fg, redraw=False, overwrite=True)

            # Advance prev state
            if lap.lap_time_sec is not None:
                prev_sec = lap.lap_time_sec
            if lap.sector_times:
                prev_sectors = lap.sector_times

        sheet.redraw()

    def _populate_stats(self, session: SessionData,
                        stat_best, stat_avg, stat_laps, stat_offt) -> None:
        best_str   = format_seconds(session.best_lap_sec) if session.best_lap_sec else "—"
        avg_str    = format_seconds(session.avg_clean_sec) if session.avg_clean_sec else "—"
        total_offt = sum(l.offtracks for l in session.laps)

        stat_best.config(text=best_str)
        stat_avg.config(text=avg_str)
        stat_laps.config(text=str(len(session.laps)))
        stat_offt.config(text=str(total_offt))


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
        outer = tk.Frame(parent, bg="#1e1e1e")
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Sessions band (top) — single unified listbox (own + external) ──
        band = tk.Frame(outer, bg="#1e1e1e")
        band.pack(fill=tk.X, padx=4, pady=(4, 2))

        sessions_frame = ctk.CTkFrame(band, corner_radius=4)
        sessions_frame.pack(fill=tk.BOTH, expand=True)

        # Header row: label + Select All button
        hdr = ctk.CTkFrame(sessions_frame, fg_color="transparent")
        hdr.pack(fill=tk.X, padx=6, pady=(4, 2))
        ctk.CTkLabel(hdr, text="Sessions  (Ctrl+Click or Shift+Click to multi-select)",
                     font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(side=tk.LEFT)
        ctk.CTkButton(hdr, text="Select All", width=90, height=22,
                      font=ctk.CTkFont(size=11),
                      command=lambda: self._session_lb.select_set(0, tk.END)
                      ).pack(side=tk.RIGHT, padx=(8, 0))

        # Single listbox — all sessions together (own labels plain, external with [EXT] prefix)
        lb_frame, self._session_lb = make_session_listbox(sessions_frame, selectmode=tk.EXTENDED)
        self._session_lb.configure(height=4)
        lb_frame.pack(fill=tk.X, padx=6, pady=(0, 4))

        # ── Bottom area: controls row + matplotlib canvas ──
        bottom = tk.Frame(outer, bg="#1e1e1e")
        bottom.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        # Controls row (chart type selector + Plot button)
        ctrl_row = ctk.CTkFrame(bottom, corner_radius=4)
        ctrl_row.pack(fill=tk.X, pady=(0, 4))

        ctk.CTkLabel(ctrl_row, text="Chart type",
                     font=ctk.CTkFont(size=11)).pack(side=tk.LEFT, padx=(10, 6), pady=6)
        self._chart_var = ctk.StringVar(value=CHART_TYPES[0])
        ctk.CTkComboBox(ctrl_row, variable=self._chart_var, values=CHART_TYPES,
                        state="readonly", width=220
                        ).pack(side=tk.LEFT, padx=(0, 10), pady=6)
        ctk.CTkButton(ctrl_row, text="Plot", command=self._plot,
                      font=ctk.CTkFont(size=13, weight="bold"), height=32, width=80
                      ).pack(side=tk.LEFT, pady=6)

        # Matplotlib canvas (fills remaining space)
        canvas_frame = ctk.CTkFrame(bottom, corner_radius=4)
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self._embed_matplotlib(canvas_frame)

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
        for s in sessions:   # all sessions together — own (plain) + external ([EXT] prefix)
            self._session_lb.insert(tk.END, s.label())

    # ── Plot dispatcher ──

    def _get_selected_sessions(self) -> list:
        return [self.sessions[i] for i in self._session_lb.curselection()
                if i < len(self.sessions)]

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
            margin = overall_best * 0.50
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
        self._ibt_path: str  = ""
        self._out_path: str  = str(Path.home() / "Documents" / "iRacing-Lap-Analysis")
        self._only_complete  = ctk.BooleanVar(value=True)
        self._processing     = False
        self._queue: queue.Queue = queue.Queue()

        self._build_top_bar()
        self._build_tabs()

    # ────────────────────────────────────────
    # Top bar
    # ────────────────────────────────────────

    @staticmethod
    def _fmt_path(path: str) -> str:
        """Returns a display-friendly version of a path (home dir collapsed)."""
        if not path:
            return ""
        try:
            return "📁 " + str(Path(path).relative_to(Path.home()).__str__().replace("\\", "/"))
        except ValueError:
            return "📁 " + path

    def _set_path_label(self, lbl: ctk.CTkLabel, path: str, placeholder: str = "") -> None:
        """Updates a path label; shows placeholder text if path is empty."""
        if path:
            lbl.configure(text=self._fmt_path(path), text_color="#aaaaaa")
        else:
            lbl.configure(text=placeholder, text_color="#555555")

    def _build_top_bar(self):
        outer = ctk.CTkFrame(self, corner_radius=6)
        outer.pack(fill=tk.X, padx=10, pady=(10, 4))

        # ── Two-column panel: left = mine, right = external ──
        cols = ctk.CTkFrame(outer, fg_color="transparent")
        cols.pack(fill=tk.X, padx=10, pady=(8, 4))
        cols.columnconfigure(0, weight=1, uniform="col")
        cols.columnconfigure(1, weight=1, uniform="col")

        # ── LEFT column — my telemetry + output ──
        left = ctk.CTkFrame(cols, corner_radius=4)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        ctk.CTkLabel(left, text="My Telemetry",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(8, 4))

        # IBT folder row
        ibt_row = ctk.CTkFrame(left, fg_color="transparent")
        ibt_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        ctk.CTkButton(ibt_row, text="Browse folder", width=120,
                      command=self._browse_ibt).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(ibt_row, text="Open IBT(s)…", width=120,
                      command=self._open_ibt_files).pack(side=tk.LEFT)

        self._ibt_lbl = ctk.CTkLabel(left, text="", text_color="#555555",
                                     anchor="w", font=ctk.CTkFont(size=11))
        self._ibt_lbl.pack(fill=tk.X, padx=12, pady=(0, 6))
        self._set_path_label(self._ibt_lbl, self._ibt_path,
                             placeholder="No folder selected")

        # Output folder row
        ctk.CTkLabel(left, text="Output folder (CSVs)",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(4, 4))

        out_row = ctk.CTkFrame(left, fg_color="transparent")
        out_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        ctk.CTkButton(out_row, text="Browse folder", width=120,
                      command=self._browse_out).pack(side=tk.LEFT)

        self._out_lbl = ctk.CTkLabel(left, text="", text_color="#555555",
                                     anchor="w", font=ctk.CTkFont(size=11))
        self._out_lbl.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._set_path_label(self._out_lbl, self._out_path)

        # ── RIGHT column — external IBT (friends/team, in-memory only) ──
        right = ctk.CTkFrame(cols, corner_radius=4)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        ctk.CTkLabel(right, text="External Telemetry  (in-memory, no CSV saved)",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(8, 4))

        ext_row = ctk.CTkFrame(right, fg_color="transparent")
        ext_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        ctk.CTkButton(ext_row, text="Open IBT(s)…", width=120,
                      command=self._open_ext_files).pack(side=tk.LEFT)

        self._ext_files_lbl = ctk.CTkLabel(right, text="No files loaded",
                                           text_color="#555555",
                                           anchor="w", font=ctk.CTkFont(size=11))
        self._ext_files_lbl.pack(fill=tk.X, padx=12, pady=(0, 8))

        # ── Actions row (full width) ──
        act_row = ctk.CTkFrame(outer, fg_color="transparent")
        act_row.pack(fill=tk.X, padx=10, pady=(0, 4))

        ctk.CTkCheckBox(act_row, text="Only complete laps",
                        variable=self._only_complete).pack(side=tk.LEFT, padx=(0, 14))

        self._process_btn = ctk.CTkButton(
            act_row, text="⚙  Process", width=120,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_processing,
        )
        self._process_btn.pack(side=tk.LEFT, padx=(0, 10))

        ctk.CTkButton(act_row, text="Reload CSVs", width=110,
                      command=self._reload_csvs).pack(side=tk.LEFT)

        # ── Progress row (full width) ──
        prog_row = ctk.CTkFrame(outer, fg_color="transparent")
        prog_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._progress = ctk.CTkProgressBar(prog_row, width=340, height=12)
        self._progress.set(0)
        self._progress.pack(side=tk.LEFT, padx=(0, 12))

        self._status_label = ctk.CTkLabel(
            prog_row, text="Select a telemetry folder and click Process.",
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
    # Folder / file pickers
    # ────────────────────────────────────────

    def _browse_ibt(self):
        d = filedialog.askdirectory(title="Select telemetry folder (.ibt files)")
        if d:
            self._ibt_path = d
            self._set_path_label(self._ibt_lbl, d)

    def _browse_out(self):
        d = filedialog.askdirectory(title="Select output folder (CSVs)")
        if d:
            self._out_path = d
            self._set_path_label(self._out_lbl, d)

    def _open_ibt_files(self):
        """Opens a file picker to select individual .ibt files for in-memory processing (own)."""
        files = filedialog.askopenfilenames(
            title="Select .ibt file(s) to open in memory",
            filetypes=[("iRacing telemetry", "*.ibt"), ("All files", "*.*")],
        )
        if not files:
            return
        self._start_memory_processing([Path(f) for f in files], source="memory")

    def _open_ext_files(self):
        """Opens a file picker to select external .ibt files (friends/team) for in-memory processing."""
        files = filedialog.askopenfilenames(
            title="Select external .ibt file(s) (friends/team)",
            filetypes=[("iRacing telemetry", "*.ibt"), ("All files", "*.*")],
        )
        if not files:
            return
        self._start_memory_processing([Path(f) for f in files], source="external")

    # ────────────────────────────────────────
    # Processing (.ibt → CSVs) in background thread
    # ────────────────────────────────────────

    def _start_processing(self):
        ibt_dir = self._ibt_path
        out_dir = self._out_path

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
            args=(ibt_files, out_dir),
            daemon=True,
        ).start()
        self.after(80, self._poll_queue)

    def _process_worker(self, files, out_dir):
        """Runs in background thread. Parses .ibt files and exports CSVs."""
        only_complete = self._only_complete.get()
        sessions      = []
        n             = len(files)

        for idx, fpath in enumerate(files, 1):
            self._queue.put(("progress", idx, n, fpath.name))
            try:
                session = iracing_laps.parse_ibt(str(fpath))
                if session:
                    sessions.append(session)
            except Exception as e:
                self._queue.put(("log", f"  ERROR {fpath.name}: {e}"))

        def filter_laps(laps):
            if only_complete:
                return [l for l in laps if l.lap_time and l.lap_time > 0]
            return laps

        laps_map = {s.filename: filter_laps(s.laps) for s in sessions}

        try:
            iracing_laps.export_csv_split(sessions, Path(out_dir), laps_map=laps_map)
        except Exception as e:
            self._queue.put(("log", f"  CSV export error: {e}"))

        self._queue.put(("done", len(sessions), sum(len(filter_laps(s.laps)) for s in sessions)))

    # ────────────────────────────────────────
    # In-memory processing (no CSV written)
    # ────────────────────────────────────────

    def _start_memory_processing(self, files: list, source: str):
        """Launches background processing of .ibt files in memory (no CSV export)."""
        self._processing = True
        self._process_btn.configure(state="disabled")
        self._progress.set(0)
        self._set_status(f"Loading {len(files)} file(s) in memory…")

        threading.Thread(
            target=self._process_worker_memory,
            args=(files, source),
            daemon=True,
        ).start()
        self.after(80, self._poll_queue)

    def _process_worker_memory(self, files, source: str):
        """Background worker: parses .ibt files and sends parsed sessions via queue (no CSV)."""
        only_complete = self._only_complete.get()
        sessions      = []
        n             = len(files)

        for idx, fpath in enumerate(files, 1):
            self._queue.put(("progress", idx, n, fpath.name))
            try:
                parsed = iracing_laps.parse_ibt(str(fpath))
                if parsed:
                    # Filter laps if needed
                    if only_complete:
                        parsed.laps = [l for l in parsed.laps
                                       if l.lap_time and l.lap_time > 0]
                    if parsed.laps:
                        sessions.append(_session_from_parsed(parsed, source=source))
            except Exception as e:
                self._queue.put(("log", f"  ERROR {fpath.name}: {e}"))

        self._queue.put(("done_memory", sessions))

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
                    print(msg[1])
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
                elif kind == "done_memory":
                    _, new_sessions = msg
                    self._progress.set(1.0)
                    self._processing = False
                    self._process_btn.configure(state="normal", text="⚙  Process")
                    if new_sessions:
                        # Merge: replace any previous sessions of the same source
                        src = new_sessions[0].source
                        kept = [s for s in self.sessions if s.source != src]
                        merged = kept + new_sessions
                        merged.sort(key=lambda s: s.date, reverse=True)
                        self._on_folder_loaded(merged)
                        noun = "session" if len(new_sessions) == 1 else "sessions"
                        lbl = "[MEM]" if src == "memory" else "[EXT]"
                        self._set_status(
                            f"{lbl} {len(new_sessions)} {noun} loaded in memory.",
                            color="#51b26e",
                        )
                        if src == "external":
                            n = len(new_sessions)
                            self._ext_files_lbl.configure(
                                text=f"{n} session(s) loaded",
                                text_color="#aaaaaa",
                            )
                    else:
                        self._set_status("No valid sessions found in selected files.",
                                         color="#f07746")
                    return
        except queue.Empty:
            pass
        if self._processing:
            self.after(80, self._poll_queue)

    # ────────────────────────────────────────
    # CSV reload (visualisation only)
    # ────────────────────────────────────────

    def _reload_csvs(self):
        out_dir = self._out_path
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
