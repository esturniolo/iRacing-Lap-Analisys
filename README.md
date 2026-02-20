# iRacing Lap Analyzer

> Also available in Spanish: [README_ES.md](README_ES.md)

A desktop app for analyzing your iRacing lap telemetry. Load your `.ibt` files, see all your lap times in a table, compare sessions visually in charts, and keep a history of your performance over time.

---

## Table of Contents

- [What does it do?](#what-does-it-do)
- [Download](#download)
- [Is it safe? Do I need to install anything?](#is-it-safe-do-i-need-to-install-anything)
- [How to use it](#how-to-use-it)
  - [Processing your own telemetry](#processing-your-own-telemetry)
  - [Opening individual IBT files](#opening-individual-ibt-files)
  - [Loading a friend's telemetry](#loading-a-friends-telemetry)
  - [The Table tab](#the-table-tab)
  - [The Charts tab](#the-charts-tab)
- [Where are my telemetry files?](#where-are-my-telemetry-files)
- [FAQ](#faq)

---

## What does it do?

- Reads iRacing `.ibt` telemetry files and extracts lap-by-lap data
- Shows lap times, sector times, off-track incidents, and pit laps in a table
- Highlights your best lap and color-codes deltas between laps
- Saves your session history as CSV files so you can track progress over time
- Lets you compare multiple sessions in charts (lap time evolution, session comparison, off-tracks per lap)
- Supports loading a friend's or teammate's `.ibt` files side by side for comparison

---

## Download

1. Go to the [Releases page](../../releases)
2. Download `iRacing-Lap-Analyzer.exe` from the latest release
3. Put it anywhere you want on your PC — that's it

---

## Is it safe? Do I need to install anything?

**No installation required.** The `.exe` is fully portable — it includes Python and all dependencies packed inside. Just double-click and run.

Windows may show a SmartScreen warning the first time ("Windows protected your PC"). This happens because the app is not signed with a paid certificate. Click **More info → Run anyway** to proceed.

The app does **not** connect to the internet, does not modify any iRacing files, and only reads `.ibt` files that you point it to.

---

## How to use it

### Processing your own telemetry

This is the main workflow. It reads all your `.ibt` files and saves the results as CSV files on disk, so you build a history over time.

1. Click **Browse folder** under *My Telemetry* and select the folder where iRacing saves your telemetry files (see [Where are my telemetry files?](#where-are-my-telemetry-files))
2. Optionally change the *Output folder* where CSV files will be saved (default: `Documents/iRacing-Lap-Analysis`)
3. Check or uncheck **Only complete laps** depending on whether you want to include laps without a recorded time
4. Click **⚙ Process**
5. The app will scan all `.ibt` files, extract lap data, and save one CSV per track+car combination. Sessions already saved are skipped automatically, so you can click Process again after your next session and only new data gets added.

### Opening individual IBT files

Click **Open IBT(s)…** under *My Telemetry* to pick one or more `.ibt` files directly. The data is loaded in memory and shown immediately — no CSV is written to disk.

### Loading a friend's telemetry

Click **Open IBT(s)…** under *External Telemetry* to load `.ibt` files from a friend or teammate. Their sessions appear labeled with `[EXT]` and can be compared side by side in the Table and Charts tabs.

### The Table tab

- Select a session from the list to see all its laps
- Columns: lap number, sector times (S1, S2…), lap time, delta vs previous lap, off-tracks, pit
- The **best lap** is highlighted in purple
- **Pit laps** are shown in grey
- **Delta** cells are color-coded: green = faster, yellow/orange/red = slower
- The stats bar at the bottom shows best lap, average clean lap, total laps, and total off-tracks
- The right panel shows an external session (if loaded) for direct comparison

### The Charts tab

Select one or more sessions (Ctrl+Click or Shift+Click for multiple) and choose a chart type:

- **Lap time evolution** — line chart of lap times per lap number, with hover tooltips
- **Session comparison** — bar chart comparing best lap times across sessions
- **Off-tracks per lap** — bar chart showing where incidents happened

---

## Where are my telemetry files?

iRacing saves `.ibt` files here by default:

```
C:\Users\YourName\Documents\iRacing\telemetry\
```

You can confirm the path in iRacing: *Options → Drive → Telemetry*.

Make sure telemetry recording is enabled in iRacing options.

---

## FAQ

**The app opens but nothing happens when I click Process.**
Make sure you selected a telemetry folder and that it contains `.ibt` files.

**I see "No .ibt files found".**
Check that the folder you selected is the right one (see [Where are my telemetry files?](#where-are-my-telemetry-files)).

**My session is not showing up after processing.**
If the session was already processed before, it is skipped to avoid duplicates. Delete the corresponding CSV in the output folder and process again.

**Sector times are empty.**
Sector times depend on the split data embedded in the `.ibt` file. Some tracks or session types may not have sector splits recorded.

**Windows says the app is unsafe.**
Click **More info → Run anyway**. The app is not malware — see [Is it safe?](#is-it-safe-do-i-need-to-install-anything).
