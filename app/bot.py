"""bot.py - headless autopilot players that record everything an experiment needs (Role 3).

This is ``app.netclient.run_bots`` plus a recorder.  It imports no pygame, so it runs on any Mininet host::

    python3 -m app.bot --server 10.0.0.1:5005 --bots 3 --prefix h2- --name h2 --out results/runs/demo --label demo

Three files are written into ``--out`` (all rows carry ``label``, all times are epoch seconds so that they line up
with the controller's CSVs and with the iperf start time, because every Mininet host shares one clock):

``NAME_bots.csv``      one row per bot at the end (the standard bot CSV of ``netclient.write_bot_csv``: RTT mean / p95,
                       jitter, downlink loss, snapshot rate, result).
``NAME_timeline.csv``  one row per bot per ``--sample-interval`` seconds: ``label, bot, t_epoch, state, game_state,
                       snap_per_s, rtt_ms, jitter_ms, rx_expected, rx_lost`` - the behaviour over time (the dip when
                       congestion starts, the recovery when the controller prioritises the game).
``NAME_rtt.csv``      every PING/PONG round trip: ``label, bot, t_epoch, rtt_ms, game_state`` - the raw samples for the
                       pooled mean / 95th percentile and the CDF plot.

``game_state`` is the protocol's state (0 lobby, 1 countdown, 2 racing, 3 finished): the analysis only looks at rows
where it is 2, so the lobby and the countdown never dilute the numbers.  SIGTERM stops the bots cleanly and still
writes the files.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import os
import signal
import sys
import threading
import time
from typing import List, Optional

try:                                    # normal use: python -m app.bot
    from . import protocol as P
    from .console import tolerant_output
    from .netclient import parse_server, run_bots, write_bot_csv
except ImportError:                     # script use: python app/bot.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import protocol as P
    from app.console import tolerant_output
    from app.netclient import parse_server, run_bots, write_bot_csv

TIMELINE_COLUMNS = ("label", "bot", "t_epoch", "state", "game_state", "snap_per_s", "rtt_ms", "jitter_ms",
                    "rx_expected", "rx_lost")
RTT_COLUMNS = ("label", "bot", "t_epoch", "rtt_ms", "game_state")


class Recorder:
    """Call it once per bot-loop pass (``run_bots(on_tick=recorder)``); it writes the timeline and the RTT samples."""

    def __init__(self, timeline_path: str, rtt_path: str, label: str = "", interval: float = 1.0,
                 clock=time.time) -> None:
        for path in (timeline_path, rtt_path):
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
        self.label, self.interval, self.clock = label, interval, clock
        self._tl_file = open(timeline_path, "w", newline="", encoding="utf-8")
        self._rtt_file = open(rtt_path, "w", newline="", encoding="utf-8")
        self._tl, self._rtt = csv.writer(self._tl_file), csv.writer(self._rtt_file)
        self._tl.writerow(TIMELINE_COLUMNS)
        self._rtt.writerow(RTT_COLUMNS)
        self._seen = {}                 # bot name -> number of RTT samples already written
        self._next_sample = 0.0
        self.timeline_rows = self.rtt_rows = 0

    def __call__(self, clients) -> None:
        now = self.clock()
        for c in clients:                                       # new PING/PONG round trips since the last pass
            n = len(c.rtt_ms)
            done = self._seen.get(c.name, 0)
            if n > done:
                for rtt in itertools.islice(c.rtt_ms, done, n):
                    self._rtt.writerow([self.label, c.name, "%.3f" % now, "%.3f" % rtt, c.game_state])
                    self.rtt_rows += 1
                self._seen[c.name] = n
        if now >= self._next_sample:
            self._next_sample = now + self.interval
            for c in clients:
                last_rtt = c.rtt_ms[-1] if c.rtt_ms else ""
                self._tl.writerow([self.label, c.name, "%.3f" % now, c.state, c.game_state,
                                   "%.1f" % c.updates_per_s(), ("%.3f" % last_rtt) if last_rtt != "" else "",
                                   "%.3f" % c.jitter.value, c.tracker.expected, c.tracker.lost])
                self.timeline_rows += 1
            self._tl_file.flush()
            self._rtt_file.flush()

    def close(self) -> None:
        for f in (self._tl_file, self._rtt_file):
            if not f.closed:
                f.close()


def run(server, count: int, *, prefix: str, name: str, out: str, label: str, skill: float = 0.85,
        duration: float = 0.0, linger: float = 0.0, interval: float = 1.0, start: bool = False,
        stop: Optional[threading.Event] = None, verbose: bool = True):
    """Run the bots and write the three files; returns the finished NetClient objects."""
    os.makedirs(out, exist_ok=True)
    rec = Recorder(os.path.join(out, "%s_timeline.csv" % name), os.path.join(out, "%s_rtt.csv" % name),
                   label=label, interval=interval)
    try:
        clients = run_bots(server, count, skill=skill, start=start, duration=duration, verbose=verbose,
                           linger=linger, stop=stop, prefix=prefix, on_tick=rec)
    finally:
        rec.close()
    write_bot_csv(os.path.join(out, "%s_bots.csv" % name), label, clients)
    return clients


def main(argv: Optional[List[str]] = None) -> int:
    tolerant_output()
    ap = argparse.ArgumentParser(prog="python3 -m app.bot",
                                 description="Autopilot players that record RTT / loss / snapshot rate over time.")
    ap.add_argument("--server", default="127.0.0.1:%d" % P.DEFAULT_PORT, metavar="HOST:PORT")
    ap.add_argument("--bots", type=int, default=4, help="players run by this process (default 4)")
    ap.add_argument("--prefix", default="bot", help="player names are PREFIX1, PREFIX2 ...")
    ap.add_argument("--name", help="file name stem inside --out (default: the prefix without a trailing dash)")
    ap.add_argument("--out", default="results/bots", metavar="DIR", help="folder for the three CSV files")
    ap.add_argument("--label", default="", help="text copied into every CSV row (the experiment case)")
    ap.add_argument("--skill", type=float, default=0.85, help="0.5 .. 1.0, scales bot speed")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (safety limit)")
    ap.add_argument("--linger", type=float, default=0.0, help="seconds to stay after the race (default 0)")
    ap.add_argument("--sample-interval", type=float, default=1.0, help="seconds between timeline rows")
    ap.add_argument("--start", action="store_true", help="the first bot (host) starts the race")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if args.bots < 1:
        ap.error("--bots must be at least 1")
    name = args.name or args.prefix.rstrip("-_") or "bots"

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())        # the experiment runner ends stragglers with SIGTERM
    try:
        clients = run(parse_server(args.server), args.bots, prefix=args.prefix, name=name, out=args.out,
                      label=args.label, skill=args.skill, duration=args.duration, linger=args.linger,
                      interval=args.sample_interval, start=args.start, stop=stop, verbose=not args.quiet)
    except (OSError, ValueError) as exc:
        print("cannot run bots: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    joined = sum(1 for c in clients if c.snapshot_count > 0)
    print("%s: %d of %d bots received snapshots; files in %s" % (name, joined, len(clients), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
