"""synthetic.py - FAKE experiment data for testing the analysis and for previewing the plots (Role 3).

Nothing here was measured.  The numbers only imitate the *shape* of what Role 1's namespace lab showed (empty
network: about 5 ms and 20 snapshots/s; congested without priority: about 120 ms RTT, heavy loss, players lost;
priority: back to normal).  Every run folder gets ``"synthetic": true`` in ``meta.json`` and the plots print a
SYNTHETIC watermark, so these files can never be mistaken for results.  Real data comes from
``scripts/run_experiment.py``.

    python3 analysis/plot.py --demo        # writes fake runs to a temp folder and plots them
"""
from __future__ import annotations

import csv
import json
import os
import random
import zlib

T0 = 1_800_000_000.0                   # an arbitrary epoch start time
IPERF_AT = 8.0                         # seconds after T0 when the fake congestion starts
RACE_AT = 6.5
RACE_LEN = 35.0

CASES = {                              # case -> (mode, congestion)
    "baseline": ("off", False), "congested_nosdn": ("off", True),
    "congested_static": ("static", True), "congested_sdn": ("dynamic", True),
}


def _profile(case: str, t_since_iperf: float):
    """(rtt_ms, snapshots/s, loss_probability) at a moment of the fake race."""
    if case == "baseline" or t_since_iperf < 0:
        return 5.0, 20.0, 0.0
    if case == "congested_nosdn":
        return 120.0, 2.5, 0.85
    if case == "congested_static":
        return 5.5, 20.0, 0.0
    return (110.0, 4.0, 0.7) if t_since_iperf < 3.0 else (5.5, 20.0, 0.0)      # dynamic: reacts after ~3 s


def make_run(run_dir: str, case: str, players: int, rep: int = 1, seed: int = 0) -> None:
    rnd = random.Random(seed * 1000 + players * 10 + rep + zlib.crc32(case.encode()) % 97)
    mode, cong = CASES[case]
    os.makedirs(os.path.join(run_dir, "controller"), exist_ok=True)
    meta = {"case": case, "players": players, "rep": rep, "mode": mode, "congestion": cong, "queue": 100,
            "bw_mbps": 10, "iperf_mbps": 20 if cong else 0, "t_start": T0 - 5, "t_iperf_start": T0 + IPERF_AT,
            "synthetic": True,
            "verify": {"game_queue_pkts": 0 if mode == "off" else 9000 * players, "other_queue_pkts": 1000,
                       "priority_flows": 0, "priority_flows_race": 0 if mode == "off" else 2 * players,
                       "ok": True}}
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    # bots: one host file with all players
    tl, rt, bots = [], [], []
    lost_bots = 0
    for b in range(players):
        name = "h%d-1" % (b + 2)
        exp = lost = 0
        dead = case == "congested_nosdn" and rnd.random() < 0.6
        lost_bots += dead
        for sec in range(int(RACE_LEN)):
            t = T0 + RACE_AT + sec
            rtt, ups, p = _profile(case, t - (T0 + IPERF_AT))
            if dead and t - (T0 + IPERF_AT) > 5:
                ups, p = 0.0, 1.0
            n = 20
            exp += n if ups > 0 else 0
            lost += int(n * p) if ups > 0 else 0
            ups_j = max(0.0, ups + rnd.uniform(-1, 1)) if ups > 5 else ups
            tl.append(["x", name, "%.3f" % t, "joined", 2, "%.1f" % ups_j,
                       "%.3f" % (rtt * rnd.uniform(0.9, 1.3)), "%.3f" % (2.0 if p == 0 else 2.0 + 10 * p), exp, lost])
            if ups > 0:
                rt.append(["x", name, "%.3f" % t, "%.3f" % (rtt * rnd.uniform(0.9, 1.4)), 2])
        bots.append(["x", name, "lost" if dead else "left", int(not dead), 0, 564.5, 100, len(rt), 0, 0, 0, 0, 0,
                     exp, lost, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    _write(os.path.join(run_dir, "h2_timeline.csv"),
           ["label", "bot", "t_epoch", "state", "game_state", "snap_per_s", "rtt_ms", "jitter_ms", "rx_expected",
            "rx_lost"], tl)
    _write(os.path.join(run_dir, "h2_rtt.csv"), ["label", "bot", "t_epoch", "rtt_ms", "game_state"], rt)
    _write(os.path.join(run_dir, "h2_bots.csv"),
           ["label", "bot", "state", "finished", "place", "progress_m", "health", "rtt_n", "rtt_mean_ms",
            "rtt_p50_ms", "rtt_p95_ms", "rtt_max_ms", "jitter_ms", "rx_expected", "rx_lost", "rx_loss_pct",
            "rx_reordered", "rx_duplicates", "snapshots", "snapshot_hz", "events", "bad_datagrams", "rx_packets",
            "rx_bytes", "tx_packets", "tx_bytes"], bots)
    # server side
    st = []
    for sec in range(int(RACE_LEN)):
        for pid in range(1, players + 1):
            st.append(["x", sec + 7, sec, "RACING", pid, "p%d" % pid, 30, 20, 7, 40, sec * 30, 0, 0, 0, 0, 0.4])
    _write(os.path.join(run_dir, "server_stats.csv"),
           ["label", "t_s", "race_t_s", "state", "player_id", "name", "rx_pps", "tx_pps", "rx_kbps", "tx_kbps",
            "rx_expected", "rx_lost", "rx_loss_pct", "rx_reordered", "rx_duplicates", "jitter_ms"], st)
    _write(os.path.join(run_dir, "server_results.csv"),
           ["label", "race", "place", "player_id", "name", "shape", "finished", "finish_s", "laps_done",
            "progress_m", "health", "hits", "wrecks", "best_lap_s", "dnf"],
           [["x", 1, i + 1, i + 1, "p%d" % (i + 1), "circle", int(i >= lost_bots), 30 + i, 1, 564.5, 100, 5, 1, 30,
             0] for i in range(players)])
    ev = [["x", 6.0, 0, 1, "RACE_START", 0, 0, 0], ["x", 41.0, 35, 2, "RACE_END", 0, 0, 0]]
    ev += [["x", 15.0 + i, 8 + i, 3 + i, "PLAYER_LEAVE", i + 1, 1, 0] for i in range(lost_bots)]
    _write(os.path.join(run_dir, "server_events.csv"), ["label", "t_s", "race_t_s", "event_id", "event", "player_id",
                                                        "a", "b"], ev)
    _write(os.path.join(run_dir, "sdn_calls.csv"),
           ["label", "t_epoch", "action", "ip", "port", "ok", "latency_ms", "attempt", "detail"],
           [["x", "%.3f" % (T0 + 1), "register", "10.0.0.%d" % (i + 2), 40000, 1, "3.1", 1, "{}"]
            for i in range(players)])
    # controller
    lk = []
    for sec in range(0, 45, 2):
        t = T0 + sec
        cong_now = cong and sec >= IPERF_AT
        prio = (mode == "static") or (mode == "dynamic" and cong_now and sec >= IPERF_AT + 3)
        mbps = 9.9 if cong_now else 0.1
        lk.append(["%.3f" % t, 1, 1, mbps, mbps / 10, 0, int(prio), mode])
    _write(os.path.join(run_dir, "controller", "link_stats.csv"),
           ["time", "dpid", "port", "tx_mbps", "util", "tx_dropped_delta", "priority_on", "mode"], lk)
    cev = [["%.3f" % (T0 - 4), "start", "mode=%s" % mode]]
    if mode == "dynamic" and cong:
        cev.append(["%.3f" % (T0 + IPERF_AT + 2.4), "priority_on", "util=0.99 >= 0.70"])
    if mode == "static":
        cev.append(["%.3f" % (T0 - 1), "priority_on", "mode=static"])
    _write(os.path.join(run_dir, "controller", "events.csv"), ["time", "event", "detail"], cev)
    if cong:
        got = 9.4 if mode != "off" else 8.6
        lines = ["[  3]  %.1f- %.1f sec  1.12 MBytes  %.2f Mbits/sec   0.05 ms  %d/  850 (%d%%)"
                 % (i, i + 1, got, 850 * (1 - got / 20), 100 * (1 - got / 20)) for i in range(30)]
        with open(os.path.join(run_dir, "iperf_sink.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")


def make_matrix(root: str, players=(2, 4, 8), reps: int = 2) -> str:
    runs = os.path.join(root, "runs")
    for n in players:
        for case in CASES:
            for rep in range(1, reps + 1):
                make_run(os.path.join(runs, "%s_n%d_r%d" % (case, n, rep)), case, n, rep)
    return runs


def _write(path: str, header, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
