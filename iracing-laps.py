"""
iracing-laps.py
---------------
Parses all .ibt telemetry files from iRacing in a given directory
and produces a lap-by-lap summary with times and off-track incidents.

Usage:
    python iracing-laps.py
    python iracing-laps.py --dir "C:/Users/YourName/Documents/iRacing/telemetry"
    python iracing-laps.py --dir . --csv results.csv

Requirements:
    pip install pyirsdk pyyaml
"""

import argparse
import csv
import datetime
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml

try:
    import irsdk
except ImportError:
    print("ERROR: pyirsdk is not installed.")
    print("Install it with: pip install pyirsdk")
    sys.exit(1)


# ──────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────

@dataclass
class LapData:
    lap_num: int
    lap_time: Optional[float]   # None if the lap is incomplete
    offtracks: int = 0
    on_pit_road: bool = False   # True if the lap was driven through the pit lane
    sector_times: list = field(default_factory=list)  # [float|None, ...] one per sector; empty if no split data
    # Internal: maps sector boundary index → LapCurrentLapTime at crossing; cleared after lap closes
    _sector_cross: dict = field(default_factory=dict, repr=False, compare=False)

    def lap_time_str(self) -> str:
        if self.lap_time is None or self.lap_time <= 0:
            return "INCOMPLETE"
        minutes = int(self.lap_time // 60)
        seconds = self.lap_time % 60
        if minutes > 0:
            return f"{minutes}:{seconds:06.3f}"
        return f"{seconds:.3f}s"

    def offtrack_str(self) -> str:
        if self.offtracks == 0:
            return "No"
        return f"Yes (x{self.offtracks})"


@dataclass
class SessionData:
    filename: str
    session_type: str           # RACE, PRACTICE, QUALIFY, etc.
    track: str
    car: str
    date: str
    laps: list = field(default_factory=list)

    def best_lap(self) -> Optional[LapData]:
        valid = [l for l in self.laps if l.lap_time and l.lap_time > 0 and not l.on_pit_road]
        if not valid:
            return None
        return min(valid, key=lambda l: l.lap_time)

    def total_offtracks(self) -> int:
        return sum(l.offtracks for l in self.laps)


# ──────────────────────────────────────────────
# Sector time helpers
# ──────────────────────────────────────────────

def _compute_sector_times(lap: 'LapData', sector_boundaries: list) -> None:
    """
    Converts the raw crossing-time dict on `lap._sector_cross` into
    `lap.sector_times` (list of floats or None, one per sector).

    sector_boundaries: sorted list of LapDistPct fractions from SplitTimeInfo,
        e.g. [0.0, 0.14, 0.33, 0.88]  (boundaries[0] == 0.0 == S/F line)
    _sector_cross: {boundary_index: LapCurrentLapTime at crossing}
        Key  1 → time when car crossed boundaries[1]  (start of sector 2)
        Key  2 → time when car crossed boundaries[2]  (start of sector 3)
        ...
        (boundary_index 0 is the S/F line and is never stored — lap starts at t=0)

    Number of sectors = len(sector_boundaries).
    Sector k (0-based) runs from boundaries[k] to boundaries[k+1] for k < last,
    and from boundaries[-1] to S/F line for the last sector.

    Sector durations:
        sector 0  = _sector_cross[1]                         (0 → split 1)
        sector k  = _sector_cross[k+1] - _sector_cross[k]   (split k → split k+1)
        last      = lap.lap_time - _sector_cross[last_idx]   (last split → finish)
    """
    num_sectors = len(sector_boundaries)  # e.g. 4 boundaries → 4 sectors
    times = []
    cross = lap._sector_cross

    for k in range(num_sectors):
        if k == 0:
            # Sector 0: from lap start (t=0) to first split crossing
            t = cross.get(1)
            times.append(round(t, 3) if t is not None else None)
        elif k == num_sectors - 1:
            # Last sector: from last split crossing to lap finish
            t_start = cross.get(k)
            if t_start is not None and lap.lap_time and lap.lap_time > 0:
                times.append(round(lap.lap_time - t_start, 3))
            else:
                times.append(None)
        else:
            # Middle sectors: difference between consecutive split crossings
            t_end   = cross.get(k + 1)
            t_start = cross.get(k)
            if t_end is not None and t_start is not None:
                times.append(round(t_end - t_start, 3))
            else:
                times.append(None)

    lap.sector_times = times
    lap._sector_cross.clear()


# ──────────────────────────────────────────────
# Parsing logic
# ──────────────────────────────────────────────

def parse_ibt(filepath: str) -> Optional[SessionData]:
    """
    Parses a single .ibt file by iterating sample by sample (60 Hz).
    Detects lap changes to record lap times and rising edges on
    PlayerTrackSurface to count off-track incidents.
    """
    try:
        ir = irsdk.IBT()
        ir.open(filepath)
    except Exception as e:
        print(f"  [ERROR] Could not open {os.path.basename(filepath)}: {e}")
        return None

    # ── Session metadata from the YAML block embedded in the mmap ──
    try:
        start  = ir._header.session_info_offset
        length = ir._header.session_info_len
        raw    = ir._shared_mem[start:start + length].rstrip(b'\x00').decode('cp1252', errors='replace')
        # Strip non-printable characters that break the YAML parser
        raw = re.sub(r'[^\x09\x0A\x0D\x20-\x7E\x80-\xFF]', '', raw)
        session_info = yaml.safe_load(raw) or {}

        weekend    = session_info.get('WeekendInfo', {})
        track_name = weekend.get('TrackDisplayName', 'Unknown')
        track_cfg  = weekend.get('TrackConfigName', '')
        if track_cfg and track_cfg.lower() not in ('', 'oval', track_name.lower()):
            track_name = f"{track_name} – {track_cfg}"

        car_name    = 'Unknown'
        driver_info = session_info.get('DriverInfo', {})
        drivers     = driver_info.get('Drivers', [])
        driver_idx  = driver_info.get('DriverCarIdx', -1)
        if drivers:
            # Look up the local driver's car by CarIdx (reliable in online sessions)
            for d in drivers:
                if d.get('CarIdx', -1) == driver_idx:
                    car_name = d.get('CarScreenNameShort', d.get('CarScreenName', 'Unknown'))
                    break
            else:
                # Fallback: first non-pace-car entry
                for d in drivers:
                    if d.get('CarIsPaceCar', 1) == 0:
                        car_name = d.get('CarScreenNameShort', d.get('CarScreenName', 'Unknown'))
                        break

        sessions     = session_info.get('SessionInfo', {}).get('Sessions', [{}])
        session_type = sessions[0].get('SessionType', 'UNKNOWN').upper() if sessions else 'UNKNOWN'

        # Sector split boundaries from SplitTimeInfo (0.0–1.0 as LapDistPct fractions)
        split_info = session_info.get('SplitTimeInfo', {})
        sectors_raw = split_info.get('Sectors', [])
        # Collect all SectorStartPct values, sorted by SectorNum
        sector_boundaries = sorted(
            float(s['SectorStartPct']) for s in sectors_raw if 'SectorStartPct' in s
        )
        # boundaries[0] == 0.0 (S/F line), boundaries[1..n] == split lines
        # Number of actual sectors = len(boundaries) - 1  (need at least 2 to have 1 sector split)
        if len(sector_boundaries) < 2:
            sector_boundaries = []   # no split data for this track
    except Exception:
        track_name       = 'Unknown'
        car_name         = 'Unknown'
        session_type     = 'UNKNOWN'
        sector_boundaries = []

    # Date from DiskSubHeader timestamp (more reliable than the YAML field)
    try:
        ts       = ir._disk_header.session_start_date
        date_str = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    except Exception:
        date_str = ''

    filename = os.path.basename(filepath)

    # ── Sample-by-sample iteration ──
    # In pyirsdk 1.3.5, get_all(key) returns a list of values for that variable.
    # Each channel is loaded separately and then iterated by index.
    #
    # Off-track detection uses PlayerTrackSurface (iRSurfaceType enum):
    #   0=NotInWorld, 1=OffTrack, 2=InPitStall, 3=ApproachingPits, 4=OnTrack
    # CarLeftTrack may not be present in all .ibt files.
    try:
        laps_arr         = ir.get_all('Lap')                  # list[int]
        lap_times_arr    = ir.get_all('LapLastLapTime')       # list[float]
        surface_arr      = ir.get_all('PlayerTrackSurface')   # list[int], 1=OffTrack
        pit_arr          = ir.get_all('OnPitRoad')            # list[bool]
        dist_pct_arr     = ir.get_all('LapDistPct')           # list[float], 0.0–1.0 around the lap
        cur_lap_time_arr = ir.get_all('LapCurrentLapTime')    # list[float], elapsed time in current lap
        n                = ir._disk_header.session_record_count
    except Exception as e:
        print(f"  [ERROR] Could not read channels from {os.path.basename(filepath)}: {e}")
        ir.close()
        return None

    if not laps_arr or not lap_times_arr:
        ir.close()
        return None

    laps: dict[int, LapData] = {}
    prev_lap_num   = -1
    prev_offtrack  = False   # used to detect rising edge (entered off-track zone)
    prev_dist_pct: dict[int, float] = {}  # lap_num → last LapDistPct, for sector crossing detection

    for i in range(n):
        try:
            lap_num       = int(laps_arr[i] or 0)
            lap_last_time = float(lap_times_arr[i] or -1.0)
            is_offtrack   = int(surface_arr[i] or 0) == 1  # iRSurfaceType::OffTrack
            on_pit_road   = bool(pit_arr[i] if pit_arr else False)
            dist_pct      = float(dist_pct_arr[i] or 0.0)     if dist_pct_arr     else 0.0
            cur_lap_time  = float(cur_lap_time_arr[i] or 0.0) if cur_lap_time_arr else 0.0
        except Exception:
            continue

        # Create an entry for this lap if it doesn't exist yet
        if lap_num not in laps:
            laps[lap_num] = LapData(lap_num=lap_num, lap_time=None)

        current_lap = laps[lap_num]

        # Mark the lap as a pit-lane lap if the car was on pit road at any point
        if on_pit_road:
            current_lap.on_pit_road = True

        # Capture lap time: LapLastLapTime is updated on the sample where Lap changes
        # (i.e., when the car crosses the start/finish line)
        if lap_num != prev_lap_num and prev_lap_num >= 0:
            prev_lap = laps.get(prev_lap_num)
            if prev_lap and lap_last_time > 0:
                prev_lap.lap_time = lap_last_time
                # Compute sector times for the completed lap using boundary crossing data
                if sector_boundaries and prev_lap._sector_cross:
                    _compute_sector_times(prev_lap, sector_boundaries)

        # Count off-track events on rising edge (car just entered an off-track zone)
        if is_offtrack and not prev_offtrack:
            current_lap.offtracks += 1

        # Detect sector boundary crossings (rising edge on each split line)
        if sector_boundaries and cur_lap_time > 0:
            prev_d = prev_dist_pct.get(lap_num, 0.0)
            for si, boundary in enumerate(sector_boundaries):
                if si == 0:
                    continue  # skip S/F line (sector 0 start = lap start)
                # Rising-edge crossing: were behind this boundary, now at or past it
                if prev_d < boundary <= dist_pct:
                    # Only record the first crossing per sector boundary per lap
                    if si not in current_lap._sector_cross:
                        current_lap._sector_cross[si] = cur_lap_time
            prev_dist_pct[lap_num] = dist_pct

        prev_lap_num  = lap_num
        prev_offtrack = is_offtrack

    ir.close()

    if not laps:
        return None

    # Sort laps. Lap 0 is the out/formation lap and is included only if it has
    # a recorded time (some files record data starting from lap 0).
    sorted_laps = [laps[k] for k in sorted(laps.keys()) if k >= 0]

    if not sorted_laps:
        return None

    return SessionData(
        filename=filename,
        session_type=session_type,
        track=track_name,
        car=car_name,
        date=date_str,
        laps=sorted_laps,
    )


# ──────────────────────────────────────────────
# Deduplication helper
# ──────────────────────────────────────────────

def _read_existing_dates(fpath: Path) -> set:
    """
    Returns the set of 'date' values already written in an existing CSV file.
    Used by export_csv_split to skip sessions that were already exported.
    Returns an empty set if the file doesn't exist or can't be read.
    """
    if not fpath.exists():
        return set()
    try:
        with open(fpath, encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            return {
                row['date'] for row in reader
                if 'date' in row and row.get('date', '').strip() not in ('', 'date')
            }
    except Exception:
        return set()


def _read_existing_sector_count(fpath: Path) -> int:
    """
    Returns the number of sector columns in an existing CSV file.
    Reads the header row only. Returns 0 if the file doesn't exist or has no sectors.
    """
    if not fpath.exists():
        return 0
    try:
        with open(fpath, encoding='utf-8', newline='') as f:
            reader = csv.reader(f)
            header = next(reader, [])
        return sum(1 for col in header if col.startswith('sector'))
    except Exception:
        return 0


def _rewrite_csv_with_more_sectors(fpath: Path, new_max_sectors: int) -> None:
    """
    Rewrites an existing CSV to add extra empty sector columns up to new_max_sectors.
    Called when a new session has more sectors than the existing CSV.
    """
    try:
        with open(fpath, encoding='utf-8', newline='') as f:
            rows = list(csv.reader(f))
        if not rows:
            return
        old_header = rows[0]
        old_sectors = sum(1 for c in old_header if c.startswith('sector'))
        if old_sectors >= new_max_sectors:
            return  # nothing to do

        new_header = CSV_HEADER_BASE + _sector_header(new_max_sectors)
        extra = new_max_sectors - old_sectors
        new_rows = [new_header]
        for row in rows[1:]:
            # Append empty strings for the additional sector columns
            new_rows.append(row + [''] * extra)

        with open(fpath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(new_rows)
    except Exception:
        pass  # leave the file as-is on any error


# ──────────────────────────────────────────────
# Console output
# ──────────────────────────────────────────────

def print_session(session: SessionData, laps: list | None = None) -> None:
    """laps allows passing a filtered list; if None, all laps from the session are used."""
    laps = laps if laps is not None else session.laps
    sep = "─" * 66
    print(f"\n{'═' * 66}")
    print(f"  File    : {session.filename}")
    print(f"  Type    : {session.session_type}")
    print(f"  Track   : {session.track}")
    print(f"  Car     : {session.car}")
    if session.date:
        print(f"  Date    : {session.date}")
    print(sep)
    print(f"  {'#':>4}  {'Lap':>6}  {'Time':>12}  {'Off-track':>12}  {'Pit':>6}")
    print(sep)

    best = session.best_lap()
    for rel, lap in enumerate(laps, 1):
        pit_flag  = "Yes" if lap.on_pit_road else "   "
        best_mark = "*" if best and lap.lap_num == best.lap_num else " "
        print(f"  {rel:>4}{best_mark} {lap.lap_num:>6}  {lap.lap_time_str():>12}  {lap.offtrack_str():>12}  {pit_flag:>6}")

    print(sep)
    if best:
        print(f"  Best lap      : {best.lap_time_str()} (lap {best.lap_num})")
    print(f"  Total laps    : {len(laps)}")
    print(f"  Total off-track: {sum(l.offtracks for l in laps)}")


# ──────────────────────────────────────────────
# CSV export
# ──────────────────────────────────────────────

CSV_HEADER_BASE = [
    'date', 'car', 'track', 'session_type',
    'lap', 'lap_time', 'offtracks', 'in_pit'
]
# Kept for backwards-compatibility when no sector data is present
CSV_HEADER = CSV_HEADER_BASE


def _sector_header(max_sectors: int) -> list:
    """Returns sector column names: ['sector1', 'sector2', ...]"""
    return [f'sector{i+1}' for i in range(max_sectors)]


def _sector_vals(lap: 'LapData', max_sectors: int) -> list:
    """Returns formatted sector time strings, padded to max_sectors with ''."""
    vals = []
    for t in lap.sector_times[:max_sectors]:
        if t is None:
            vals.append('')
        else:
            mins = int(t // 60)
            secs = t % 60
            vals.append(f"{mins}:{secs:06.3f}" if mins > 0 else f"{secs:.3f}s")
    # Pad if fewer sectors recorded than max
    vals += [''] * (max_sectors - len(vals))
    return vals


def export_csv(sessions: list[SessionData], output_path: str,
               laps_map: dict | None = None) -> None:
    """laps_map: {session.filename: [LapData, ...]} to pass filtered lap lists."""
    all_laps = [l for s in sessions for l in (laps_map or {}).get(s.filename, s.laps)]
    max_sectors = max((len(l.sector_times) for l in all_laps), default=0)
    header = CSV_HEADER_BASE + _sector_header(max_sectors)

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for session in sessions:
            laps = (laps_map or {}).get(session.filename, session.laps)
            for rel, lap in enumerate(laps, 1):
                writer.writerow([
                    session.date,
                    session.car,
                    session.track,
                    session.session_type,
                    rel,
                    lap.lap_time_str(),
                    lap.offtracks,
                    '1' if lap.on_pit_road else '0',
                    *_sector_vals(lap, max_sectors),
                ])
    print(f"\n✔ CSV exported: {output_path}")


# ──────────────────────────────────────────────
# Split CSV export (one file per track + car)
# ──────────────────────────────────────────────

def csv_filename(track: str, car: str) -> str:
    """Generates a safe filename from track and car names."""
    name = f"{track} - {car}"
    # Remove characters invalid in filenames on Windows and macOS
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # Collapse multiple spaces / underscores
    name = re.sub(r'_+', '_', name).strip('_ ')
    return name + '.csv'


def export_csv_split(sessions: list, output_dir: Path,
                     laps_map: dict | None = None) -> list:
    """
    Writes one CSV per track+car combination into output_dir.
    Appends rows to an existing file without repeating the header.
    Skips any session whose 'date' value is already present in the CSV
    (deduplication: safe to call multiple times without creating duplicates).

    If the new session has more sectors than the existing CSV, the CSV is
    transparently rewritten to add the extra (empty) sector columns before
    appending, so every row always has the same number of columns.

    Returns the list of files written to.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written_files = []

    for session in sessions:
        laps = (laps_map or {}).get(session.filename, session.laps)
        if not laps:
            continue

        fpath = output_dir / csv_filename(session.track, session.car)

        # Skip this session if it was already written (deduplication by date)
        existing_dates = _read_existing_dates(fpath)
        if session.date in existing_dates:
            continue

        session_sectors = max((len(l.sector_times) for l in laps), default=0)
        existing_sectors = _read_existing_sector_count(fpath)
        max_sectors = max(session_sectors, existing_sectors)

        # If the CSV exists but has fewer sector columns than this session,
        # rewrite it with extra empty columns so all rows stay aligned.
        if fpath.exists() and session_sectors > existing_sectors:
            _rewrite_csv_with_more_sectors(fpath, max_sectors)

        file_exists = fpath.exists()
        header = CSV_HEADER_BASE + _sector_header(max_sectors)

        with open(fpath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)
            for rel, lap in enumerate(laps, 1):
                writer.writerow([
                    session.date,
                    session.car,
                    session.track,
                    session.session_type,
                    rel,
                    lap.lap_time_str(),
                    lap.offtracks,
                    '1' if lap.on_pit_road else '0',
                    *_sector_vals(lap, max_sectors),
                ])

        if fpath not in written_files:
            written_files.append(fpath)

    return written_files


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    default_dir = str(Path.home() / "Documents" / "iRacing" / "telemetry")

    parser = argparse.ArgumentParser(
        description="Parse iRacing .ibt files and display lap times and off-track incidents"
    )
    parser.add_argument(
        '--dir', '-d',
        default=default_dir,
        help=f'Directory containing .ibt files (default: {default_dir})'
    )
    parser.add_argument(
        '--output-dir', '-O',
        default=None,
        help='Directory where CSVs are saved (default: same as --dir)'
    )
    parser.add_argument(
        '--csv', '-c',
        default=None,
        help='Also export a single CSV file containing all sessions (optional)'
    )
    parser.add_argument(
        '--recursive', '-r',
        action='store_true',
        help='Search for .ibt files in subdirectories as well'
    )
    parser.add_argument(
        '--only-complete', '-o',
        action='store_true',
        help='Only show/export laps with a recorded time (excludes INCOMPLETE laps)'
    )
    args = parser.parse_args()

    # Resolve directories
    base       = Path(args.dir)
    output_dir = Path(args.output_dir) if args.output_dir else base

    if not base.exists():
        print(f"ERROR: Directory not found: {base}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Find .ibt files
    pattern   = '**/*.ibt' if args.recursive else '*.ibt'
    ibt_files = sorted(base.glob(pattern))

    if not ibt_files:
        print(f"No .ibt files found in: {base}")
        sys.exit(0)

    print(f"Processing {len(ibt_files)} file(s)…")
    print("(Sessions already in a CSV are skipped automatically — delete the CSV to reprocess.)")

    sessions = []
    for i, filepath in enumerate(ibt_files, 1):
        print(f"[{i}/{len(ibt_files)}] {filepath.name}...", end=' ', flush=True)
        session = parse_ibt(str(filepath))
        if session:
            sessions.append(session)
            print(f"OK ({len(session.laps)} laps)")
        else:
            print("SKIPPED (no valid data)")

    if not sessions:
        print("\nCould not extract data from any file.")
        sys.exit(0)

    # Filter incomplete laps if requested
    def filter_laps(laps):
        if args.only_complete:
            return [l for l in laps if l.lap_time and l.lap_time > 0]
        return laps

    laps_map = {s.filename: filter_laps(s.laps) for s in sessions}

    # Print results to console
    for session in sessions:
        print_session(session, laps=laps_map[session.filename])

    # Global summary
    total_laps      = sum(len(laps_map[s.filename]) for s in sessions)
    total_offtracks = sum(sum(l.offtracks for l in laps_map[s.filename]) for s in sessions)
    print(f"\n{'═' * 66}")
    print(f"  GLOBAL SUMMARY")
    print(f"  Sessions processed : {len(sessions)}")
    print(f"  Total laps         : {total_laps}")
    print(f"  Total off-tracks   : {total_offtracks}")
    print(f"{'═' * 66}\n")

    # Export per-track/car CSVs (always active)
    written = export_csv_split(sessions, output_dir, laps_map=laps_map)
    if written:
        print(f"✔ Per-track/car CSVs saved to: {output_dir}")
        for f in written:
            print(f"    {f.name}")

    # Optional single CSV (for bulk import / compatibility)
    if args.csv:
        export_csv(sessions, args.csv, laps_map=laps_map)


if __name__ == '__main__':
    main()
