"""metrics.py - turn one experiment run folder into the numbers of the report (Role 3).

Standard library only, so it also runs under ``sudo python3`` inside the experiment runner.

A run folder (written by ``scripts/run_experiment.py``) contains:

    meta.json                              what was run (case, players, mode, times of iperf ...)
    server_stats.csv / server_results.csv / server_events.csv    the game server's own logs (Role 1)
    sdn_calls.csv                          every call from the server to the SDN controller (app/sdn_client.py)
    h2_bots.csv, h2_timeline.csv, h2_rtt.csv ...   one set per player host (app/bot.py)
    controller/link_stats.csv, controller/events.csv   the Ryu controller's logs (Role 2)
    iperf_sink.txt                         what the background traffic received

All client numbers use only the *race window* (``game_state == 2``): the lobby and the countdown never count.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import re
from typing import Dict, Iterable, List, Optional

NAN = float("nan")
STATE_RACING = 2                    # app.protocol.ST_RACING
SNAPSHOT_RATE = 20.0                # SNAPSHOTs per second the server sends to each player
EV_TIMEOUT = 1                      # PLAYER_LEAVE reason DISC_TIMEOUT (a column of the event log)
BAD_BOT_STATES = ("lost", "failed", "rejected")


# ---------------------------------------------------------------------- small helpers
def read_csv(path: str) -> List[Dict[str, str]]:
    """Rows of a CSV file as dicts; a missing or empty file gives ``[]``."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def num(row: Dict[str, str], key: str, default: float = NAN) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


def percentile(values: Iterable[float], p: float) -> float:
    """p-th percentile (0..100) with linear interpolation, like numpy's default; NaN for no data."""
    data = sorted(v for v in values if not math.isnan(v))
    if not data:
        return NAN
    if len(data) == 1:
        return data[0]
    k = (len(data) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def mean(values: Iterable[float]) -> float:
    data = [v for v in values if not math.isnan(v)]
    return sum(data) / len(data) if data else NAN


def load_meta(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def read_csv_glob(run_dir: str, pattern: str) -> List[Dict[str, str]]:
    """All rows of every CSV in ``run_dir`` matching ``pattern`` (one file per player host)."""
    return _glob_rows(run_dir, pattern)


def _glob_rows(run_dir: str, pattern: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in sorted(glob.glob(os.path.join(run_dir, pattern))):
        rows.extend(read_csv(path))
    return rows


def _window_loss(rows: List[Dict[str, str]], key: str, expected: str, lost: str) -> float:
    """Pooled loss in percent over the rows of the race window: per player (``key``) the growth of the cumulative
    counters between its first and its last row, summed over players."""
    first: Dict[str, Dict[str, str]] = {}
    last: Dict[str, Dict[str, str]] = {}
    for r in rows:
        first.setdefault(r[key], r)
        last[r[key]] = r
    tot_exp = tot_lost = 0.0
    for k, a in first.items():
        b = last[k]
        de, dl = num(b, expected, 0.0) - num(a, expected, 0.0), num(b, lost, 0.0) - num(a, lost, 0.0)
        if de > 0:
            tot_exp += de
            tot_lost += max(0.0, dl)
    return 100.0 * tot_lost / tot_exp if tot_exp > 0 else NAN


# ---------------------------------------------------------------------- iperf (the background traffic)
_IPERF = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s+sec\s+[\d.]+\s+\w*Bytes\s+([\d.]+)\s+(\w*)bits/sec"
                    r"\s+([\d.]+)\s+ms\s+(\d+)\s*/\s*(\d+)")
_UNITS = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


def parse_iperf_sink(text: str) -> List[dict]:
    """One dict per 1-second interval of an ``iperf -s -u -i 1`` log: t0, t1, mbps, jitter_ms, lost, total."""
    out, seen = [], set()
    for m in _IPERF.finditer(text):
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 - t0 > 1.5 or (t0, t1) in seen:           # skip the final whole-run summary line, and repeats
            continue
        seen.add((t0, t1))
        out.append({"t0": t0, "t1": t1, "mbps": float(m.group(3)) * _UNITS.get(m.group(4).upper(), 1.0),
                    "jitter_ms": float(m.group(5)), "lost": int(m.group(6)), "total": int(m.group(7))})
    return out


# ---------------------------------------------------------------------- the summary of one run
def summarize_run(run_dir: str) -> dict:
    """Every number of one run in one flat dict (a row of ``results/summary.csv``)."""
    meta = load_meta(run_dir)
    out: dict = {"run": os.path.basename(os.path.normpath(run_dir)), "case": meta.get("case", ""),
                 "players": meta.get("players", 0), "rep": meta.get("rep", 1), "mode": meta.get("mode", ""),
                 "congestion": int(bool(meta.get("congestion", False))), "queue": meta.get("queue", ""),
                 "bw_mbps": meta.get("bw_mbps", ""), "iperf_mbps": meta.get("iperf_mbps", 0)}

    # ---- client side: RTT, jitter, snapshots per second, downlink loss (race window only)
    rtt = [num(r, "rtt_ms") for r in _glob_rows(run_dir, "*_rtt.csv") if num(r, "game_state") == STATE_RACING]
    timeline = [r for r in _glob_rows(run_dir, "*_timeline.csv") if num(r, "game_state") == STATE_RACING]
    ups = [num(r, "snap_per_s") for r in timeline]
    jit = [num(r, "jitter_ms") for r in timeline]
    out.update({"rtt_n": len(rtt), "rtt_mean_ms": mean(rtt), "rtt_p50_ms": percentile(rtt, 50),
                "rtt_p95_ms": percentile(rtt, 95), "rtt_p99_ms": percentile(rtt, 99),
                "rtt_max_ms": max(rtt) if rtt else NAN,
                "jitter_ms": mean(jit), "jitter_p95_ms": percentile(jit, 95),
                "updates_per_s": mean(ups), "updates_p5": percentile(ups, 5),
                "snapshot_delivery_pct": min(100.0, 100.0 * mean(ups) / SNAPSHOT_RATE) if ups else NAN,
                "loss_down_pct": _window_loss(timeline, "bot", "rx_expected", "rx_lost")})

    # ---- how the bots ended
    bots = _glob_rows(run_dir, "*_bots.csv")
    out["bots"] = len(bots)
    out["players_lost"] = sum(1 for b in bots if b.get("state") in BAD_BOT_STATES)
    out["players_finished_client"] = sum(1 for b in bots if num(b, "finished", 0) == 1)

    # ---- server side: uplink loss / jitter in the race, results, timeouts
    stats = [r for r in read_csv(os.path.join(run_dir, "server_stats.csv")) if r.get("state") == "RACING"]
    out["loss_up_pct"] = _window_loss(stats, "player_id", "rx_expected", "rx_lost")
    out["jitter_up_ms"] = mean(num(r, "jitter_ms") for r in stats)
    out["server_tx_kbps_total"] = _sum_per_second(stats, "tx_kbps")
    results = read_csv(os.path.join(run_dir, "server_results.csv"))
    fin = [num(r, "finish_s") for r in results if num(r, "finished", 0) == 1]
    out["players_finished"] = len(fin)
    out["finish_mean_s"] = mean(fin)
    out["finish_max_s"] = max(fin) if fin else NAN
    events = read_csv(os.path.join(run_dir, "server_events.csv"))
    out["server_timeouts"] = sum(1 for e in events if e.get("event") == "PLAYER_LEAVE" and num(e, "a") == EV_TIMEOUT)
    start = [num(e, "t_s") for e in events if e.get("event") == "RACE_START"]
    end = [num(e, "t_s") for e in events if e.get("event") == "RACE_END"]
    out["race_s"] = (end[0] - start[0]) if start and end else NAN

    # ---- SDN: controller logs, reaction time, registration
    t_iperf = meta.get("t_iperf_start")
    cev = read_csv(os.path.join(run_dir, "controller", "events.csv"))
    ons = [num(e, "time") for e in cev if e.get("event") == "priority_on"]
    offs = [num(e, "time") for e in cev if e.get("event") == "priority_off"]
    t_run0 = meta.get("t_start", 0) or 0
    ons = [t for t in ons if t >= t_run0]
    offs = [t for t in offs if t >= t_run0]
    out["priority_on_events"], out["priority_off_events"] = len(ons), len(offs)
    out["sdn_reaction_s"] = NAN
    if meta.get("mode") == "dynamic" and t_iperf:
        later = [t for t in ons if t >= t_iperf - 1.0]
        if later:
            out["sdn_reaction_s"] = max(0.0, later[0] - t_iperf)
    links = [r for r in read_csv(os.path.join(run_dir, "controller", "link_stats.csv"))
             if r.get("dpid") == "1" and num(r, "time") >= t_run0]
    out["bottleneck_mbps_mean"] = mean(num(r, "tx_mbps") for r in links)
    out["bottleneck_util_max"] = max((num(r, "util", 0.0) for r in links), default=NAN)
    out["priority_on_pct"] = 100.0 * mean(num(r, "priority_on", 0.0) for r in links) if links else NAN
    calls = read_csv(os.path.join(run_dir, "sdn_calls.csv"))
    ok_reg = [c for c in calls if c.get("action") == "register" and c.get("ok") == "1"]
    out["sdn_registrations"] = len(ok_reg)
    out["sdn_register_ms"] = mean(num(c, "latency_ms") for c in ok_reg)
    out["sdn_call_failures"] = sum(1 for c in calls if c.get("ok") == "0")
    verify = meta.get("verify") or {}
    out["game_queue_pkts"] = verify.get("game_queue_pkts", "")
    out["other_queue_pkts"] = verify.get("other_queue_pkts", "")
    out["priority_flows"] = verify.get("priority_flows", "")              # after the race: 0, the players have left
    out["priority_flows_race"] = verify.get("priority_flows_race", "")    # during the race (run_experiment.sample_flows)
    out["game_queue_dropped"] = verify.get("game_queue_dropped", "")
    out["other_queue_dropped"] = verify.get("other_queue_dropped", "")
    out["verify_ok"] = verify.get("ok", "") if verify.get("ok") is not None else ""
    if isinstance(out["verify_ok"], bool):
        out["verify_ok"] = int(out["verify_ok"])

    # ---- the background traffic: does it keep its share?
    try:
        with open(os.path.join(run_dir, "iperf_sink.txt"), encoding="utf-8", errors="replace") as f:
            ip = parse_iperf_sink(f.read())
    except OSError:
        ip = []
    out["iperf_got_mbps"] = mean(i["mbps"] for i in ip) if ip else NAN
    tot = sum(i["total"] for i in ip)
    out["iperf_loss_pct"] = 100.0 * sum(i["lost"] for i in ip) / tot if tot else NAN

    out["valid"] = int(len(rtt) > 0 and len(timeline) > 0)
    return {k: (round(v, 3) if isinstance(v, float) and not math.isnan(v) else ("" if isinstance(v, float) else v))
            for k, v in out.items()}


def _sum_per_second(rows: List[Dict[str, str]], key: str) -> float:
    """Mean over the seconds of the race of the sum over players (the server's total rate)."""
    per_t: Dict[str, float] = {}
    for r in rows:
        per_t[r["t_s"]] = per_t.get(r["t_s"], 0.0) + num(r, key, 0.0)
    return mean(per_t.values()) if per_t else NAN


SUMMARY_COLUMNS = [
    "run", "case", "players", "rep", "mode", "congestion", "queue", "bw_mbps", "iperf_mbps",
    "rtt_n", "rtt_mean_ms", "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms", "rtt_max_ms",
    "jitter_ms", "jitter_p95_ms", "updates_per_s", "updates_p5", "snapshot_delivery_pct",
    "loss_down_pct", "loss_up_pct", "jitter_up_ms", "server_tx_kbps_total",
    "bots", "players_lost", "players_finished", "players_finished_client", "server_timeouts",
    "finish_mean_s", "finish_max_s", "race_s",
    "sdn_reaction_s", "priority_on_events", "priority_off_events", "priority_on_pct", "bottleneck_mbps_mean",
    "bottleneck_util_max", "sdn_registrations", "sdn_register_ms", "sdn_call_failures",
    "game_queue_pkts", "other_queue_pkts", "game_queue_dropped", "other_queue_dropped", "priority_flows",
    "priority_flows_race", "verify_ok",
    "iperf_got_mbps", "iperf_loss_pct", "valid",
]


def write_summary(runs_dir: str, out_csv: str) -> List[dict]:
    """Summarise every run folder under ``runs_dir`` into one CSV (rewritten each time); returns the rows."""
    rows = []
    for d in sorted(glob.glob(os.path.join(runs_dir, "*"))):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "meta.json")):
            rows.append(summarize_run(d))
    folder = os.path.dirname(out_csv)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return rows


if __name__ == "__main__":                     # python3 -m analysis.metrics results/runs results/summary.csv
    import sys
    runs = sys.argv[1] if len(sys.argv) > 1 else os.path.join("results", "runs")
    dest = sys.argv[2] if len(sys.argv) > 2 else os.path.join("results", "summary.csv")
    done = write_summary(runs, dest)
    print("%d run(s) summarised into %s" % (len(done), dest))
