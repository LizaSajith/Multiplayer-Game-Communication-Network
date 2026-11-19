"""export_results.py - the results as spreadsheet files: CSV (always) and one Excel workbook (Role 3).

    python3 -m analysis.export_results              # after analysis/plot.py; scripts/demo.py calls it by itself

Written into ``results/``:

    comparison.csv            one row per case and player count: the mean over the repetitions of every key metric
    test_cases.csv            the demo's test cases TC01 ... with PASS / FAIL (written by scripts/demo.py)
    summary.csv               one row per run, every metric (written by analysis/plot.py, unchanged)
    project17_results.xlsx    the same in one workbook, ready for Excel or LibreOffice Calc:
        Comparison       the comparison table (4 cases x 2 / 4 / 8 players)
        SDN vs No SDN    per player count: no SDN, static and dynamic side by side, improvement as formulas
        Test cases       TC01 ... with result and the pass / fail counts as formulas
        All runs         every run, every metric (= summary.csv)
        Live run         the race measured with demo.py --live, if there is one
        Notes            what each column means and where it was measured

The CSV files need nothing but Python.  The workbook needs openpyxl (``pip install openpyxl`` or
``sudo apt install python3-openpyxl``); without it the CSV files are still written and a hint is printed.
"""
from __future__ import annotations

import csv
import math
import os
import sys
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):                       # script use: python3 analysis/export_results.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import metrics as M                   # noqa: E402

CASE_ORDER = ["baseline", "congested_nosdn", "congested_static", "congested_sdn", "idle_sdn"]
CASE_LABEL = {"baseline": "No congestion",
              "congested_nosdn": "Congested, no SDN (baseline)",
              "congested_static": "Congested, static priority",
              "congested_sdn": "Congested, dynamic SDN",
              "idle_sdn": "No congestion, dynamic SDN"}

# (column key in summary.csv, header in the files, number format in Excel)
COMPARISON = [
    ("rtt_mean_ms", "RTT mean (ms)", "0.0"),
    ("rtt_p95_ms", "RTT p95 (ms)", "0.0"),
    ("rtt_p99_ms", "RTT p99 (ms)", "0.0"),
    ("jitter_ms", "Jitter (ms)", "0.0"),
    ("loss_down_pct", "Loss server->players (%)", "0.0"),
    ("loss_up_pct", "Loss players->server (%)", "0.0"),
    ("updates_per_s", "Snapshots per second", "0.0"),
    ("players_finished", "Players finished", "0.0"),
    ("server_timeouts", "Players timed out", "0.0"),
    ("sdn_reaction_s", "Controller reaction (s)", "0.0"),
    ("priority_on_pct", "Priority ON (% of polls)", "0"),
    ("bottleneck_mbps_mean", "Bottleneck load (Mbit/s)", "0.0"),
    ("iperf_got_mbps", "Background traffic got (Mbit/s)", "0.0"),
]
TEST_COLUMNS = ["ID", "Area", "What is checked", "Result", "Seconds", "Detail"]

NOTES = [
    ("RTT mean / p95 / p99 (ms)", "Round-trip time from PING/PONG, once a second per player, only while the race runs; "
                                  "p95 = 95 % of the samples were at or below this value"),
    ("Jitter (ms)", "RFC 3550 estimate at each player, mean over the race"),
    ("Loss server->players (%)", "Lost SNAPSHOT packets, from the sequence numbers at the players (race only)"),
    ("Loss players->server (%)", "Lost INPUT packets, measured by the server (race only)"),
    ("Snapshots per second", "SNAPSHOTs received per player and second; the server sends 20, so 20 = no loss"),
    ("Players finished", "Players who completed the race (server results)"),
    ("Players timed out", "Players the server removed after 5 s of silence"),
    ("Controller reaction (s)", "Time from the start of the congestion to the controller's priority_on "
                                "(dynamic SDN only)"),
    ("Priority ON (% of polls)", "Share of the controller's 2-second polls with game priority switched on"),
    ("Bottleneck load (Mbit/s)", "Traffic on the 10 Mbit/s bottleneck port, measured by the controller"),
    ("Background traffic got (Mbit/s)", "What the iperf receiver got: shows the priority does not starve other traffic"),
    ("SDN check", "ok = the switch's queue counters match what the mode promises (verify.txt in each run folder)"),
    ("Repetitions", "Values are means over this many runs of the same case"),
    ("Cases", "No congestion; congested without SDN (the baseline); congested with static priority (always on); "
              "congested with dynamic SDN (on only while the link is busy)"),
]


# ---------------------------------------------------------------------- the numbers
def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _clean(v):
    """NaN -> None (an empty cell), floats rounded for the files."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(v, 3) if isinstance(v, float) else v


def comparison_rows(summary: Sequence[Dict[str, str]]) -> List[dict]:
    """Mean over the valid repetitions, one dict per (case, players), in report order."""
    groups: Dict[tuple, List[dict]] = {}
    for r in summary:
        if str(r.get("valid")) == "1" and r.get("case"):
            groups.setdefault((r["case"], int(_num(r.get("players")))), []).append(r)
    order = {c: i for i, c in enumerate(CASE_ORDER)}
    out = []
    for (case, n) in sorted(groups, key=lambda k: (k[1], order.get(k[0], 99), k[0])):
        rs = groups[(case, n)]
        row = {"case": case, "case_label": CASE_LABEL.get(case, case), "players": n, "repetitions": len(rs)}
        for key, _, _ in COMPARISON:
            vals = [v for v in (_num(r.get(key)) for r in rs) if not math.isnan(v)]
            row[key] = sum(vals) / len(vals) if vals else math.nan
        p95 = [v for v in (_num(r.get("rtt_p95_ms")) for r in rs) if not math.isnan(v)]
        if len(p95) > 1:
            m = sum(p95) / len(p95)
            row["rtt_p95_sd"] = math.sqrt(sum((v - m) ** 2 for v in p95) / (len(p95) - 1))
        else:
            row["rtt_p95_sd"] = math.nan
        checks = [str(r.get("verify_ok", "")) for r in rs]
        row["sdn_check"] = ("FAILED" if "0" in checks else "ok" if checks and all(c == "1" for c in checks) else "-")
        out.append(row)
    return out


def _write_csv(path: str, header: Sequence[str], rows: Sequence[Sequence]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if _clean(v) is None else _clean(v) for v in r])


def write_comparison_csv(rows: List[dict], path: str) -> None:
    header = ["Case", "Case key", "Players", "Repetitions"] + [h for _, h, _ in COMPARISON] + \
             ["RTT p95 std dev (ms)", "SDN check"]
    _write_csv(path, header, [[r["case_label"], r["case"], r["players"], r["repetitions"]] +
                              [r[k] for k, _, _ in COMPARISON] + [r["rtt_p95_sd"], r["sdn_check"]] for r in rows])


def write_test_cases_csv(tests: Sequence[tuple], path: str) -> None:
    _write_csv(path, TEST_COLUMNS, [[t[0], t[1], t[2], t[3], round(t[4], 2), t[5]] for t in tests])


def read_test_cases_csv(path: str) -> List[tuple]:
    out = []
    for r in M.read_csv(path):
        out.append((r.get("ID", ""), r.get("Area", ""), r.get("What is checked", ""), r.get("Result", ""),
                    _num(r.get("Seconds")) if not math.isnan(_num(r.get("Seconds"))) else 0.0, r.get("Detail", "")))
    return out


# ---------------------------------------------------------------------- the workbook
def write_workbook(path: str, comparison: List[dict], summary: List[Dict[str, str]], tests: Sequence[tuple],
                   plots_dir: Optional[str] = None, live: Optional[List[Dict[str, str]]] = None,
                   warning: str = "") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    font = Font(name="Arial", size=10)
    bold = Font(name="Arial", size=10, bold=True)
    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F3864")
    title_font = Font(name="Arial", size=14, bold=True, color="1F3864")
    note_font = Font(name="Arial", size=9, italic=True, color="595959")
    thin = Side(style="thin", color="BFBFBF")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    green, red = PatternFill("solid", fgColor="C6EFCE"), PatternFill("solid", fgColor="FFC7CE")

    wb = Workbook()
    wb.calculation.fullCalcOnLoad = True            # Excel / Calc compute the formulas when the file opens

    def header(ws, row, names, widths=None):
        for i, name in enumerate(names, 1):
            c = ws.cell(row=row, column=i, value=name)
            c.font, c.fill, c.border, c.alignment = head_font, head_fill, box, Alignment(wrap_text=True,
                                                                                       vertical="center")
        ws.row_dimensions[row].height = 30
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def cell(ws, row, col, value, fmt=None, f=None):
        c = ws.cell(row=row, column=col, value=_clean(value))
        c.font, c.border = f or font, box
        if fmt:
            c.number_format = fmt
        return c

    def title(ws, text, sub):
        ws["A1"] = text
        ws["A1"].font = title_font
        ws["A2"] = sub
        ws["A2"].font = note_font

    # ---- Comparison
    ws = wb.active
    ws.title = "Comparison"
    title(ws, "Project 17 - baseline vs SDN, mean over the repetitions",
          "Every number measured in Mininet. Rows with the same player count share the same congestion.")
    names = ["Case", "Players", "Repetitions"] + [h for _, h, _ in COMPARISON] + ["RTT p95 std dev (ms)", "SDN check"]
    header(ws, 4, names, [30, 9, 12] + [14] * len(COMPARISON) + [13, 11])
    where: Dict[tuple, str] = {}                     # (case, players, key) -> cell address, for the formulas
    r = 5
    for row in comparison:
        cell(ws, r, 1, row["case_label"])
        cell(ws, r, 2, row["players"], "0")
        cell(ws, r, 3, row["repetitions"], "0")
        for j, (key, _, fmt) in enumerate(COMPARISON, 4):
            cell(ws, r, j, row[key], fmt)
            where[(row["case"], row["players"], key)] = "Comparison!$%s$%d" % (get_column_letter(j), r)
        cell(ws, r, 4 + len(COMPARISON), row["rtt_p95_sd"], "0.0")
        chk = cell(ws, r, 5 + len(COMPARISON), row["sdn_check"])
        chk.fill = green if row["sdn_check"] == "ok" else red if row["sdn_check"] == "FAILED" else PatternFill()
        r += 1
    if not comparison:
        ws.cell(row=5, column=1, value="No measured runs yet: sudo python3 scripts/demo.py --prepare").font = bold
    if warning:                                      # local or synthetic data must never pass for a measurement
        ws["A3"] = warning
        ws["A3"].font = Font(name="Arial", size=11, bold=True, color="C00000")
    ws.freeze_panes = "B5"

    # ---- SDN vs No SDN (formulas pointing at the Comparison sheet)
    ws = wb.create_sheet("SDN vs No SDN")
    title(ws, "What the SDN changes under the same congestion",
          "Values link to the Comparison sheet. Improvement = 1 - SDN / no SDN (higher is better).")
    metrics = [("rtt_p95_ms", "RTT p95 (ms)"), ("rtt_mean_ms", "RTT mean (ms)"),
               ("loss_down_pct", "Loss server->players (%)"), ("jitter_ms", "Jitter (ms)"),
               ("updates_per_s", "Snapshots per second")]
    header(ws, 4, ["Players", "Metric", "No congestion", "No SDN (baseline)", "Static priority", "Dynamic SDN",
                   "Improvement, static", "Improvement, dynamic"], [9, 26, 15, 17, 15, 14, 18, 20])
    r = 5
    for n in sorted({row["players"] for row in comparison}):
        for key, label in metrics:
            cell(ws, r, 1, n, "0")
            cell(ws, r, 2, label)
            for j, case in enumerate(["baseline", "congested_nosdn", "congested_static", "congested_sdn"], 3):
                ref = where.get((case, n, key))
                c = cell(ws, r, j, None, "0.0")
                if ref:
                    c.value = "=IF(ISNUMBER(%s),%s,\"\")" % (ref, ref)
            for j, col in ((7, "E"), (8, "F")):
                c = cell(ws, r, j, None, "0%")
                if key == "updates_per_s":           # more is better: the gain against no SDN
                    c.value = "=IF(AND(ISNUMBER(D%d),ISNUMBER(%s%d),D%d>0),%s%d/D%d-1,\"\")" % (r, col, r, r, col, r, r)
                else:                                # less is better: how much lower than no SDN
                    c.value = "=IF(AND(ISNUMBER(D%d),ISNUMBER(%s%d),D%d>0),1-%s%d/D%d,\"\")" % (r, col, r, r, col, r, r)
            r += 1
        # one more row per player count: the controller's reaction time
        reaction = where.get(("congested_sdn", n, "sdn_reaction_s"))
        cell(ws, r, 1, n, "0")
        cell(ws, r, 2, "Controller reaction (s)")
        c = cell(ws, r, 6, None, "0.0")
        if reaction:
            c.value = "=IF(ISNUMBER(%s),%s,\"\")" % (reaction, reaction)
        for j in (3, 4, 5, 7, 8):
            cell(ws, r, j, None)
        r += 2
    ws.cell(row=r, column=1, value="Improvement for snapshots per second is the gain (more is better); for the other "
                                    "rows it is how much lower than without SDN.").font = note_font
    ws.freeze_panes = "C5"

    # ---- Test cases
    ws = wb.create_sheet("Test cases")
    title(ws, "Automated test cases (python3 scripts/demo.py)", "Each row runs one named unit test.")
    header(ws, 4, TEST_COLUMNS, [8, 16, 52, 10, 10, 60])
    r = 5
    for t in tests:
        cell(ws, r, 1, t[0])
        cell(ws, r, 2, t[1])
        cell(ws, r, 3, t[2])
        res = cell(ws, r, 4, t[3], f=bold)
        res.fill = green if t[3] == "PASS" else red if t[3] == "FAIL" else PatternFill()
        cell(ws, r, 5, t[4], "0.0")
        cell(ws, r, 6, t[5]).alignment = wrap
        r += 1
    last = max(5, r - 1)
    r += 1
    for label, formula in (("Passed", '=COUNTIF(D5:D%d,"PASS")' % last), ("Failed", '=COUNTIF(D5:D%d,"FAIL")' % last),
                           ("Total", "=COUNTA(A5:A%d)" % last), ("Total time (s)", "=SUM(E5:E%d)" % last)):
        ws.cell(row=r, column=3, value=label).font = bold
        c = ws.cell(row=r, column=4, value=formula)
        c.font = bold
        c.number_format = "0.0" if "time" in label else "0"
        r += 1
    if not tests:
        ws.cell(row=5, column=1, value="Run python3 scripts/demo.py to fill this sheet.").font = bold
    ws.freeze_panes = "A5"

    # ---- All runs and the live run (summary.csv as it is)
    def table_sheet(name, rows, intro):
        s = wb.create_sheet(name)
        title(s, name, intro)
        cols = list(M.SUMMARY_COLUMNS)
        header(s, 4, cols, [24] + [12] * (len(cols) - 1))
        for i, row in enumerate(rows, 5):
            for j, key in enumerate(cols, 1):
                v = row.get(key, "")
                n = _num(v)
                cell(s, i, j, v if (v == "" or math.isnan(n) or key in ("run", "case", "mode", "queue")) else n,
                     None if math.isnan(n) else "General")
        s.freeze_panes = "B5"

    table_sheet("All runs", summary, "One row per run (results/summary.csv). Run folders: results/runs/<run>.")
    if live:
        table_sheet("Live run", live, "Measured with demo.py --live (results/live); not part of the Comparison.")

    # ---- Notes
    ws = wb.create_sheet("Notes")
    title(ws, "What the columns mean", "Loss, jitter and RTT count only while the race runs (not lobby or countdown).")
    header(ws, 4, ["Column", "Meaning"], [32, 100])
    for i, (k, v) in enumerate(NOTES, 5):
        cell(ws, i, 1, k, f=bold)
        cell(ws, i, 2, v).alignment = wrap

    # ---- printing: landscape, as wide as one page
    for sheet in wb.worksheets:
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb.save(path)


# ---------------------------------------------------------------------- entry points
def export(results_dir: str, tests: Optional[Sequence[tuple]] = None, quiet: bool = False) -> dict:
    """Write comparison.csv, test_cases.csv (when ``tests`` is given) and project17_results.xlsx.

    Returns {"csv": [...paths], "xlsx": path or None, "warning": text or None}.  Never raises for a missing
    openpyxl: the CSV files are the fallback."""
    summary = M.read_csv(os.path.join(results_dir, "summary.csv"))
    comp = comparison_rows(summary)
    out = {"csv": [], "xlsx": None, "warning": None}
    p = os.path.join(results_dir, "comparison.csv")
    write_comparison_csv(comp, p)
    out["csv"].append(p)
    tc_path = os.path.join(results_dir, "test_cases.csv")
    if tests:
        write_test_cases_csv(tests, tc_path)
        out["csv"].append(tc_path)
    else:
        tests = read_test_cases_csv(tc_path)          # keep the last demo's results in the workbook
    live = M.read_csv(os.path.join(results_dir, "live", "summary.csv"))
    metas = [M.load_meta(os.path.join(results_dir, "runs", r.get("run", ""))) for r in summary]
    warning = ""
    if any(m.get("synthetic") for m in metas):
        warning = "SYNTHETIC DEMO DATA - NOT A MEASUREMENT. Run sudo python3 scripts/demo.py --prepare on the Mininet VM."
    elif any(m.get("backend") == "local" for m in metas):
        warning = "LOCAL PIPELINE TEST - no real network. Run sudo python3 scripts/demo.py --prepare on the Mininet VM."

    xlsx = os.path.join(results_dir, "project17_results.xlsx")
    try:
        write_workbook(xlsx, comp, summary, tests, os.path.join(results_dir, "plots"), live, warning)
        out["xlsx"] = xlsx
    except ImportError:
        out["warning"] = ("openpyxl is missing, so no Excel file was written (the CSV files were). "
                          "Install it with: pip install openpyxl   or   sudo apt install python3-openpyxl")
    except OSError as exc:                            # e.g. the workbook is open in Excel on Windows
        out["warning"] = "could not write %s (%s) - close it if it is open and run again" % (xlsx, exc)
    if not quiet:
        for f in out["csv"] + ([out["xlsx"]] if out["xlsx"] else []):
            print("  " + f)
        if out["warning"]:
            print("  WARNING: " + out["warning"])
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Write the results as CSV files and an Excel workbook.")
    ap.add_argument("--results", default="results", help="the results folder (default: results)")
    args = ap.parse_args(argv)
    if not os.path.exists(os.path.join(args.results, "summary.csv")):
        print("no %s - run python3 analysis/plot.py (or python3 scripts/demo.py) first"
              % os.path.join(args.results, "summary.csv"), file=sys.stderr)
        return 1
    res = export(args.results)
    return 0 if res["xlsx"] else 1


if __name__ == "__main__":
    sys.exit(main())
