#!/usr/bin/env python3
"""plot.py - graphs and tables for the performance section of the report (Role 3).

    python3 analysis/plot.py                       # reads results/runs, writes results/summary.csv, results/plots/*.png
    python3 analysis/plot.py --runs results/runs --out results/plots --timeline-players 8
    python3 analysis/plot.py --demo                # FAKE data, only to preview the figures (watermarked SYNTHETIC)

Needs matplotlib (``pip install matplotlib`` or ``sudo apt install python3-matplotlib``).  Written files:

    results/summary.csv            one row per run, every metric (analysis/metrics.py)
    results/summary_table.md       tables for the report, and "dynamic SDN vs no SDN" in numbers
    results/plots/fig_latency.png      RTT mean and 95th percentile vs number of players, every case
    results/plots/fig_loss.png         downlink and uplink loss
    results/plots/fig_jitter.png       jitter
    results/plots/fig_updates.png      snapshots per second received (20 = perfect) and players that finished
    results/plots/fig_timeline_nN.png  RTT, snapshots/s and bottleneck load over time, congestion start marked
    results/plots/fig_cdf_nN.png       distribution (CDF) of the RTT
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from collections import defaultdict
from typing import Dict, List

if __package__ in (None, ""):                       # script use: python3 analysis/plot.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import metrics as M                   # noqa: E402

CASE_ORDER = ["baseline", "congested_nosdn", "congested_static", "congested_sdn", "idle_sdn"]
CASE_LABEL = {"baseline": "No congestion",
              "congested_nosdn": "Congested, no SDN (baseline)",
              "congested_static": "Congested, static priority",
              "congested_sdn": "Congested, dynamic SDN",
              "idle_sdn": "No congestion, dynamic SDN"}
CASE_COLOR = {"baseline": "#8c8c8c", "congested_nosdn": "#d62728", "congested_static": "#1f77b4",
              "congested_sdn": "#2ca02c", "idle_sdn": "#98df8a"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _f(row: dict, key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return math.nan


def aggregate(rows: List[dict]) -> Dict[tuple, dict]:
    """(case, players) -> {metric: (mean, std, n)} over the repetitions."""
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        if str(r.get("valid")) == "1":
            groups[(r["case"], int(r["players"]))].append(r)
    out = {}
    for key, rs in groups.items():
        agg = {}
        for metric in M.SUMMARY_COLUMNS:
            vals = [_f(r, metric) for r in rs]
            vals = [v for v in vals if not math.isnan(v)]
            if vals:
                m = sum(vals) / len(vals)
                sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0
                agg[metric] = (m, sd, len(vals))
            else:
                agg[metric] = (math.nan, 0.0, 0)
        out[key] = agg
    return out


def _watermark(fig, synthetic) -> None:
    """``synthetic`` is False, or the warning text to print across the figure."""
    if synthetic:
        fig.text(0.5, 0.5, synthetic if isinstance(synthetic, str) else "SYNTHETIC DEMO DATA - NOT A MEASUREMENT", fontsize=22, color="red", alpha=0.18,
                 ha="center", va="center", rotation=25)


def grouped_bars(ax, agg, cases, players, metric, title, ylabel, log=False, hline=None):
    width = 0.8 / max(1, len(cases))
    for i, case in enumerate(cases):
        xs, ys, es = [], [], []
        for j, n in enumerate(players):
            m, sd, k = agg.get((case, n), {}).get(metric, (math.nan, 0.0, 0))
            if k:
                xs.append(j + (i - (len(cases) - 1) / 2) * width)
                ys.append(m)
                es.append(sd)
        ax.bar(xs, ys, width * 0.92, yerr=es if any(es) else None, capsize=2, label=CASE_LABEL.get(case, case),
               color=CASE_COLOR.get(case, None))
    ax.set_xticks(range(len(players)))
    ax.set_xticklabels([str(n) for n in players])
    ax.set_xlabel("players")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.3)
    if log:
        ax.set_yscale("log")
    if hline is not None:
        ax.axhline(hline, color="k", ls="--", lw=0.8)


def bar_figures(agg, cases, players, out_dir, synthetic):
    plt = _plt()
    specs = {
        "fig_latency.png": [("rtt_mean_ms", "Round-trip time, mean", "ms", True),
                            ("rtt_p95_ms", "Round-trip time, 95th percentile", "ms", True)],
        "fig_loss.png": [("loss_down_pct", "Packet loss server -> players (downlink)", "% of packets", False),
                         ("loss_up_pct", "Packet loss players -> server (uplink)", "% of packets", False)],
        "fig_jitter.png": [("jitter_ms", "Jitter at the players (mean)", "ms", False),
                           ("jitter_p95_ms", "Jitter at the players (95th percentile)", "ms", False)],
        "fig_updates.png": [("updates_per_s", "Snapshots received per second (20 = healthy)", "snapshots/s", False),
                            ("players_finished", "Players that finished the race", "players", False)],
    }
    for name, panels in specs.items():
        fig, axes = plt.subplots(1, len(panels), figsize=(6.2 * len(panels), 4.4))
        for ax, (metric, title, ylabel, log) in zip(axes, panels):
            grouped_bars(ax, agg, cases, players, metric, title, ylabel, log, hline=20 if metric == "updates_per_s" else None)
        h, l = axes[0].get_legend_handles_labels()
        fig.legend(h, l, loc="lower center", ncol=len(l), fontsize=8, frameon=False)
        _watermark(fig, synthetic)
        fig.tight_layout(rect=(0, 0.07, 1, 1))
        fig.savefig(os.path.join(out_dir, name), dpi=150)
        plt.close(fig)


def _series(run_dir: str, meta: dict):
    """Per-second series of one run, time measured from the start of the congestion."""
    t0 = meta.get("t_iperf_start") or 0.0
    rtt, ups = defaultdict(list), defaultdict(list)
    for r in M.read_csv_glob(run_dir, "*_rtt.csv"):
        if M.num(r, "game_state") == M.STATE_RACING:
            rtt[int(math.floor(M.num(r, "t_epoch") - t0))].append(M.num(r, "rtt_ms"))
    for r in M.read_csv_glob(run_dir, "*_timeline.csv"):
        if M.num(r, "game_state") == M.STATE_RACING:
            ups[int(math.floor(M.num(r, "t_epoch") - t0))].append(M.num(r, "snap_per_s"))
    links = [r for r in M.read_csv(os.path.join(run_dir, "controller", "link_stats.csv")) if r.get("dpid") == "1"]
    link_t = [M.num(r, "time") - t0 for r in links]
    link_mbps = [M.num(r, "tx_mbps") for r in links]
    prio = [M.num(r, "priority_on", 0.0) for r in links]
    ev = M.read_csv(os.path.join(run_dir, "controller", "events.csv"))
    ons = [M.num(e, "time") - t0 for e in ev if e.get("event") == "priority_on" and M.num(e, "time") >= (meta.get("t_start") or 0)]
    return ({k: M.mean(v) for k, v in sorted(rtt.items())}, {k: M.mean(v) for k, v in sorted(ups.items())},
            link_t, link_mbps, prio, ons)


def timeline_figure(runs_dir, players, cases, out_dir, synthetic, rep=1):
    plt = _plt()
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    any_data = False
    for case in cases:
        run_dir = os.path.join(runs_dir, "%s_n%d_r%d" % (case, players, rep))
        meta = M.load_meta(run_dir)
        if not meta:
            continue
        rtt, ups, lt, lm, prio, ons = _series(run_dir, meta)
        if not rtt:
            continue
        any_data = True
        c, lab = CASE_COLOR.get(case), CASE_LABEL.get(case, case)
        a1.plot(list(rtt), list(rtt.values()), color=c, label=lab, lw=1.6)
        a2.plot(list(ups), list(ups.values()), color=c, label=lab, lw=1.6)
        if lt:
            a3.plot(lt, lm, color=c, label=lab, lw=1.6, marker="o", ms=3)
        for t in ons:
            if case in ("congested_sdn", "idle_sdn"):
                for ax in (a1, a2, a3):
                    ax.axvline(t, color=c, ls="--", lw=1.0)
                a1.text(t + 0.4, 0.5, "controller turns\npriority ON", transform=a1.get_xaxis_transform(),
                        fontsize=8, color=c)
    if not any_data:
        plt.close(fig)
        return False
    for ax in (a1, a2, a3):
        ax.axvline(0, color="k", ls=":", lw=1.0)
        ax.grid(alpha=0.3)
    a1.set_yscale("log")
    a1.set_ylabel("RTT (ms, log)")
    a1.set_title("%d players: what the game feels over time (dotted = congestion starts)" % players)
    a2.set_ylabel("snapshots/s received")
    a2.axhline(20, color="k", ls="--", lw=0.7)
    a3.set_ylabel("bottleneck load (Mbit/s)\nmeasured by the controller")
    a3.set_xlabel("seconds since the congestion started")
    h, l = a1.get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=2, fontsize=8, frameon=False)
    _watermark(fig, synthetic)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(os.path.join(out_dir, "fig_timeline_n%d.png" % players), dpi=150)
    plt.close(fig)
    return True


def cdf_figure(runs_dir, players, cases, out_dir, synthetic, rep=1):
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4.6))
    any_data = False
    for case in cases:
        run_dir = os.path.join(runs_dir, "%s_n%d_r%d" % (case, players, rep))
        vals = sorted(M.num(r, "rtt_ms") for r in M.read_csv_glob(run_dir, "*_rtt.csv")
                      if M.num(r, "game_state") == M.STATE_RACING)
        vals = [v for v in vals if not math.isnan(v)]
        if not vals:
            continue
        any_data = True
        ax.plot(vals, [100.0 * (i + 1) / len(vals) for i in range(len(vals))], color=CASE_COLOR.get(case),
                label=CASE_LABEL.get(case, case), lw=1.8)
    if not any_data:
        plt.close(fig)
        return False
    ax.set_xscale("log")
    ax.set_xlabel("round-trip time (ms, log)")
    ax.set_ylabel("% of samples at or below")
    ax.set_title("%d players: distribution of the round-trip time" % players)
    ax.axhline(95, color="k", ls=":", lw=0.8)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="lower right")
    _watermark(fig, synthetic)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "fig_cdf_n%d.png" % players), dpi=150)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------- the markdown tables
def _fmt(v, digits=1):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else ("%.*f" % (digits, v))


def table_markdown(agg, cases, players, synthetic) -> str:
    out = ["# Results summary (generated by analysis/plot.py)", ""]
    if synthetic:
        out += ["**%s - these numbers must not be used in the report.**" % (
            synthetic if isinstance(synthetic, str) else "SYNTHETIC DEMO DATA"), ""]
    g = lambda c, n, k: agg.get((c, n), {}).get(k, (math.nan, 0, 0))[0]
    for n in players:
        out += ["## %d players" % n, "",
                "| Case | RTT mean (ms) | RTT p95 (ms) | Jitter (ms) | Loss down (%) | Loss up (%) | Snapshots/s | "
                "Finished | Timeouts | iperf got (Mbit/s) |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for c in cases:
            if (c, n) not in agg:
                continue
            out.append("| %s | %s | %s | %s | %s | %s | %s | %s / %s | %s | %s |" % (
                CASE_LABEL.get(c, c), _fmt(g(c, n, "rtt_mean_ms")), _fmt(g(c, n, "rtt_p95_ms")),
                _fmt(g(c, n, "jitter_ms")), _fmt(g(c, n, "loss_down_pct")), _fmt(g(c, n, "loss_up_pct")),
                _fmt(g(c, n, "updates_per_s")), _fmt(g(c, n, "players_finished"), 0), n,
                _fmt(g(c, n, "server_timeouts"), 0), _fmt(g(c, n, "iperf_got_mbps"))))
        base, sdn, static = "congested_nosdn", "congested_sdn", "congested_static"
        if (base, n) in agg and (sdn, n) in agg:
            b95, s95 = g(base, n, "rtt_p95_ms"), g(sdn, n, "rtt_p95_ms")
            out += ["", "**Dynamic SDN vs no SDN under the same congestion:** RTT p95 %s ms -> %s ms (%s times lower); "
                    "downlink loss %s %% -> %s %%; snapshots/s %s -> %s; players finished %s -> %s of %d; "
                    "controller reaction time %s s." % (
                        _fmt(b95), _fmt(s95), _fmt(b95 / s95 if s95 and not math.isnan(s95) and s95 > 0 else math.nan),
                        _fmt(g(base, n, "loss_down_pct")), _fmt(g(sdn, n, "loss_down_pct")),
                        _fmt(g(base, n, "updates_per_s")), _fmt(g(sdn, n, "updates_per_s")),
                        _fmt(g(base, n, "players_finished"), 0), _fmt(g(sdn, n, "players_finished"), 0), n,
                        _fmt(g(sdn, n, "sdn_reaction_s")))]
        if (static, n) in agg and (sdn, n) in agg:
            out += ["", "**Dynamic vs static priority:** RTT p95 %s ms vs %s ms; the controller kept priority on for "
                    "%s %% of the polls (static: always); background iperf got %s vs %s Mbit/s." % (
                        _fmt(g(sdn, n, "rtt_p95_ms")), _fmt(g(static, n, "rtt_p95_ms")),
                        _fmt(g(sdn, n, "priority_on_pct"), 0), _fmt(g(sdn, n, "iperf_got_mbps")),
                        _fmt(g(static, n, "iperf_got_mbps")))]
        out.append("")
    out += ["## SDN verification (did the traffic really use the priority queue?)", "",
            "| Case | Players | Packets in game queue 1 | Packets in other queue 0 | Priority flows on s1 during the race "
            "| Check |",
            "|---|---|---|---|---|---|"]
    for n in players:
        for c in cases:
            if (c, n) in agg:
                ok = g(c, n, "verify_ok")           # mean over the repetitions: 1.0 only when every one passed
                out.append("| %s | %d | %s | %s | %s | %s |" % (
                    CASE_LABEL.get(c, c), n, _fmt(g(c, n, "game_queue_pkts"), 0), _fmt(g(c, n, "other_queue_pkts"), 0),
                    _fmt(g(c, n, "priority_flows_race"), 0),
                    "-" if math.isnan(ok) else ("ok" if ok >= 1.0 else "FAILED")))
    out += ["", "The packet counts are read from the bottleneck port of s1 after the race (`verify.txt`). The check "
            "passes when the game queue carried the game's packets in the cases with priority and stayed unused in the "
            "cases without. The priority flows are the `set_queue:1` rules counted about 10 s after the congestion "
            "starts (`flows_in_race.txt`); with `--match registered` there are two per player address, one for each "
            "direction. '-' means that the run has no such sample. After the race the switch holds none, because the "
            "controller removes a player's rules when the player leaves.",
            "", "Notes: loss and jitter are measured only while the race is running. 'Loss down' comes from the packet "
            "sequence numbers and cannot see a total blackout; 'Snapshots/s' can (a silent player receives 0). "
            "Values are means over the repetitions of each case.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plots and tables for the Project 17 experiments (Role 3).")
    ap.add_argument("--runs", default=os.path.join("results", "runs"), help="folder with one sub-folder per run")
    ap.add_argument("--summary", default=os.path.join("results", "summary.csv"))
    ap.add_argument("--out", default=os.path.join("results", "plots"))
    ap.add_argument("--table", default=os.path.join("results", "summary_table.md"))
    ap.add_argument("--timeline-players", type=int, action="append", help="draw the timeline for this player count "
                    "(default: every count found)")
    ap.add_argument("--demo", action="store_true", help="use FAKE synthetic data in a temp folder (preview only)")
    args = ap.parse_args(argv)

    synthetic = "SYNTHETIC DEMO DATA - NOT A MEASUREMENT" if args.demo else ""
    if args.demo:
        from analysis import synthetic as S
        root = tempfile.mkdtemp(prefix="p17_demo_")
        args.runs = S.make_matrix(root)
        args.summary = os.path.join(root, "summary.csv")
        args.out = os.path.join(root, "plots")
        args.table = os.path.join(root, "summary_table.md")
        print("SYNTHETIC demo data in %s" % root)
    rows = M.write_summary(args.runs, args.summary)
    if not rows:
        print("no runs found in %s - run scripts/run_experiment.py first (or try --demo)" % args.runs, file=sys.stderr)
        return 1
    try:
        _plt()
    except ImportError:
        print("matplotlib is missing: pip install matplotlib   (the CSV and the table were still written)",
              file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    metas = [M.load_meta(os.path.join(args.runs, r["run"])) for r in rows]
    if not synthetic and any(m.get("synthetic") for m in metas):
        synthetic = "SYNTHETIC DEMO DATA - NOT A MEASUREMENT"     # fake data must never pass for a result
    elif not synthetic and any(m.get("backend") == "local" for m in metas):
        synthetic = "LOCAL PIPELINE TEST - NO REAL NETWORK"       # loopback runs have no bottleneck: never report them
    agg = aggregate(rows)
    cases = [c for c in CASE_ORDER if any(k[0] == c for k in agg)] + sorted(
        {k[0] for k in agg} - set(CASE_ORDER))
    players = sorted({k[1] for k in agg})
    if not agg:
        print("runs exist but none is valid (no RTT samples in the race window); look at the logs in %s" % args.runs,
              file=sys.stderr)
        return 1
    bar_figures(agg, cases, players, args.out, synthetic)
    for n in (args.timeline_players or players):
        timeline_figure(args.runs, n, cases, args.out, synthetic)
        cdf_figure(args.runs, n, cases, args.out, synthetic)
    os.makedirs(os.path.dirname(args.table) or ".", exist_ok=True)
    with open(args.table, "w", encoding="utf-8") as f:
        f.write(table_markdown(agg, cases, players, synthetic))
    print("summary: %s\ntable:   %s\nplots:   %s" % (args.summary, args.table, args.out))
    for name in sorted(os.listdir(args.out)):
        print("  " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
