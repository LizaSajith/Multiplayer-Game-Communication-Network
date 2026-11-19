#!/usr/bin/env python3
"""A small network lab without Mininet: the game server and the bots run on separate (virtual) hosts.

Every host is a Linux network namespace with its own network stack.  The picture is the usual "dumbbell":

      srv   ib                                   h2   h3   ...   ia
        \\   |                                    |    |         /
       [ left switch ] ====== bottleneck ====== [ right switch ]

    srv = game server    h2, h3 ... = hosts running the bots (the players)
    ib  = iperf3 server  ia = iperf3 client      (both only with --iperf)

The bottleneck link can be limited to a bandwidth with a queue, an iperf3 flow can fill it, and the game's UDP port
can get a priority queue.  It is the same idea as the Mininet experiments of Roles 2 and 3, small enough to read
in one file, and it checks that the game works across real network stacks.

Needs Linux, root, iproute2 (ip, tc) and, for --iperf, iperf3.  Python 3.9+.  Run it from the project folder:

    sudo python3 tools/netns_lab.py --case baseline --players 8
    sudo python3 tools/netns_lab.py --case congested --players 8 --iperf 14
    sudo python3 tools/netns_lab.py --case priority  --players 8 --iperf 14 --priority

One race is played per run.  Files written to --out (default results/lab): CASE_bots_HOST.csv (what the players
saw; one file per bot host, because two programs must not append to one file at the same moment), CASE_stats.csv,
CASE_results.csv and CASE_events.csv (what the server saw), and the logs of every program.
Whatever happens, the namespaces, the switches and the programs are removed again at the end.
"""
import argparse
import csv
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 5005
LEFT, RIGHT = "t17-bl", "t17-br"        # the two switches (Linux bridges)
LINK_L, LINK_R = "t17x-l", "t17x-r"     # the two ends of the bottleneck link between them
SERVER_HOST, IPERF_SERVER, IPERF_CLIENT = "srv", "ib", "ia"
SUBNET = "10.17.0"                      # srv .1, bot hosts .2 .3 ..., iperf client .100, iperf server .101


def ns(host: str) -> str:
    return "t17-" + host


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and done.returncode != 0:
        raise RuntimeError("%s\n%s" % (" ".join(cmd), done.stdout.strip()))
    return done


def in_ns(host: str, *cmd: str) -> List[str]:
    return ["ip", "netns", "exec", ns(host)] + list(cmd)


# ---------------------------------------------------------------------- the lab
def remove_lab(hosts: List[str]) -> None:
    for host in hosts:
        run("ip", "netns", "del", ns(host), check=False)      # also removes the host's end of its veth pair ...
        run("ip", "link", "del", "t17p-" + host, check=False)  # ... in the background, so remove the switch end too
    run("ip", "link", "del", LINK_L, check=False)
    for bridge in (LEFT, RIGHT):
        run("ip", "link", "del", bridge, check=False)
    for _ in range(50):                                        # the kernel finishes asynchronously: wait for the names
        names = run("ip", "-br", "link", check=False).stdout
        if "t17" not in names and not [h for h in hosts if ns(h) in run("ip", "netns", "list", check=False).stdout]:
            return
        time.sleep(0.1)


def build_lab(left: Dict[str, str], right: Dict[str, str]) -> None:
    for bridge in (LEFT, RIGHT):
        run("ip", "link", "add", bridge, "type", "bridge")
        run("ip", "link", "set", bridge, "up")
    run("ip", "link", "add", LINK_L, "type", "veth", "peer", "name", LINK_R)
    for port, bridge in ((LINK_L, LEFT), (LINK_R, RIGHT)):
        run("ip", "link", "set", port, "master", bridge)
        run("ip", "link", "set", port, "up")
    for bridge, hosts in ((LEFT, left), (RIGHT, right)):
        for host, address in hosts.items():
            run("ip", "netns", "add", ns(host))
            port = "t17p-" + host
            run("ip", "link", "add", "t17v-" + host, "type", "veth", "peer", "name", port)
            run("ip", "link", "set", "t17v-" + host, "netns", ns(host), "name", "eth0")
            run("ip", "link", "set", port, "master", bridge)
            run("ip", "link", "set", port, "up")
            run(*in_ns(host, "ip", "addr", "add", "%s/24" % address, "dev", "eth0"))
            run(*in_ns(host, "ip", "link", "set", "eth0", "up"))
            run(*in_ns(host, "ip", "link", "set", "lo", "up"))
    # Fixed ARP entries, like Mininet's staticArp: on an overloaded bottleneck the ARP replies are dropped like every
    # other packet, and a host could then fail to find the server before the first JOIN.  We measure the game here.
    addresses = dict(left)
    addresses.update(right)
    macs = {h: run(*in_ns(h, "cat", "/sys/class/net/eth0/address")).stdout.strip() for h in addresses}
    for host in addresses:
        for other, address in addresses.items():
            if other != host:
                run(*in_ns(host, "ip", "neigh", "replace", address, "lladdr", macs[other], "dev", "eth0",
                           "nud", "permanent"))


def shape(device: str, port_match: str, mbit: float, queue: int, priority: bool) -> None:
    """Limit what leaves ``device`` to ``mbit`` with a queue of ``queue`` packets (a full queue drops the newest).
    With ``priority`` the game's UDP port (``port_match`` = sport or dport) goes into a class that is served first."""
    rate = "%gmbit" % mbit
    run("tc", "qdisc", "add", "dev", device, "root", "handle", "1:", "htb", "default", "20")
    run("tc", "class", "add", "dev", device, "parent", "1:", "classid", "1:1", "htb", "rate", rate, "ceil", rate)
    other = max(0.5, mbit - 1.0) if priority else mbit
    run("tc", "class", "add", "dev", device, "parent", "1:1", "classid", "1:20", "htb", "rate", "%gmbit" % other,
        "ceil", rate, "prio", "1")
    run("tc", "qdisc", "add", "dev", device, "parent", "1:20", "handle", "20:", "pfifo", "limit", str(queue))
    if priority:
        run("tc", "class", "add", "dev", device, "parent", "1:1", "classid", "1:10", "htb", "rate", "1mbit",
            "ceil", rate, "prio", "0")
        run("tc", "qdisc", "add", "dev", device, "parent", "1:10", "handle", "10:", "pfifo", "limit", str(queue))
        run("tc", "filter", "add", "dev", device, "parent", "1:", "protocol", "ip", "prio", "1", "u32",
            "match", "ip", "protocol", "17", "0xff", "match", "ip", port_match, str(PORT), "0xffff", "flowid", "1:10")


# ---------------------------------------------------------------------- results
def case_files(out: str, case: str) -> List[str]:
    """The files of exactly this case (the case "congested" must not touch the files of "congested_q1000")."""
    own = re.compile(r"^%s_(bots_h\d+\.csv|h\d+_bots\.log|stats\.csv|results\.csv|events\.csv|server\.log|"
                     r"iperf_server\.log|iperf_client\.log)$" % re.escape(case))
    return [os.path.join(out, n) for n in sorted(os.listdir(out)) if own.match(n)]


def read_rows(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def summarize(out: str, case: str, players: int) -> dict:
    bots: List[dict] = []
    for path in case_files(out, case):
        if re.search(r"_bots_h\d+\.csv$", path):
            bots += read_rows(path)
    stats = read_rows(os.path.join(out, case + "_stats.csv"))
    results = read_rows(os.path.join(out, case + "_results.csv"))
    last: Dict[str, dict] = {}
    for row in stats:
        last[row["player_id"]] = row                 # cumulative uplink counters: the last row per player
    f = lambda rows, key: [float(r[key]) for r in rows]
    return {
        "case": case, "players": players, "bots_reporting": len(bots),
        "rtt_mean_ms": mean(f(bots, "rtt_mean_ms")), "rtt_p95_ms": mean(f(bots, "rtt_p95_ms")),
        "rtt_max_ms": max(f(bots, "rtt_max_ms")) if bots else float("nan"),
        "jitter_ms": mean(f(bots, "jitter_ms")),
        "down_loss_pct": mean(f(bots, "rx_loss_pct")), "up_loss_pct": mean(f(list(last.values()), "rx_loss_pct")),
        "snapshot_hz": mean(f(bots, "snapshot_hz")),
        "finished": sum(1 for r in results if r["finished"] == "1"),
        "race_s": max([float(r["finish_s"]) for r in results if r["finished"] == "1" and r["finish_s"]] or [0.0]),
    }


def print_table(rows: List[dict]) -> None:
    head = ("case", "players", "RTT mean", "RTT p95", "RTT max", "jitter", "down loss", "up loss", "snap/s", "finished")
    print("  ".join("%-10s" % h for h in head))
    for r in rows:
        cells = (r["case"], r["players"], "%.1f ms" % r["rtt_mean_ms"], "%.1f ms" % r["rtt_p95_ms"],
                 "%.0f ms" % r["rtt_max_ms"], "%.1f ms" % r["jitter_ms"], "%.1f %%" % r["down_loss_pct"],
                 "%.1f %%" % r["up_loss_pct"], "%.1f" % r["snapshot_hz"], "%d / %d" % (r["finished"], r["players"]))
        print("  ".join("%-10s" % c for c in cells))


# ---------------------------------------------------------------------- one run
def play(args) -> dict:
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    for path in case_files(out, args.case):          # a rerun of the same case starts from empty files
        os.remove(path)
    bot_hosts = ["h%d" % (i + 2) for i in range(args.hosts)]
    left = {SERVER_HOST: SUBNET + ".1"}
    right = {h: "%s.%d" % (SUBNET, i + 2) for i, h in enumerate(bot_hosts)}
    if args.iperf:
        left[IPERF_SERVER] = SUBNET + ".101"
        right[IPERF_CLIENT] = SUBNET + ".100"
    everything = list(left) + list(right)
    remove_lab(everything)                           # leftovers of a crashed earlier run
    procs: List[subprocess.Popen] = []
    logs = []

    def start(host: str, name: str, *cmd: str) -> subprocess.Popen:
        log = open(os.path.join(out, "%s_%s.log" % (args.case, name)), "w")
        logs.append(log)
        proc = subprocess.Popen(in_ns(host, *cmd), stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        procs.append(proc)
        return proc

    try:
        build_lab(left, right)
        if args.bw:
            shape(LINK_L, "sport", args.bw, args.queue, args.priority)      # server -> players: the snapshots
            shape(LINK_R, "dport", args.bw, args.queue, args.priority)      # players -> server: the inputs
        prefix = os.path.join(out, args.case)
        server = start(SERVER_HOST, "server", sys.executable, "-m", "app.server", "--port", str(PORT),
                       "--auto-start", str(args.players), "--auto-start-delay", "2", "--exit-after-races", "1",
                       "--laps", str(args.laps), "--label", args.case,
                       "--stats-csv", prefix + "_stats.csv", "--results-csv", prefix + "_results.csv",
                       "--event-log", prefix + "_events.csv")
        time.sleep(1.0)
        if args.iperf:
            start(IPERF_SERVER, "iperf_server", "iperf3", "-s")
            time.sleep(0.5)
            reverse = [] if args.iperf_dir == "up" else ["-R"]       # -R: the iperf server sends, toward the players
            start(IPERF_CLIENT, "iperf_client", "iperf3", "-c", SUBNET + ".101", "-u", "-b", "%gM" % args.iperf,
                  "-t", str(args.max_seconds), *reverse)
            time.sleep(2.0)                                          # let the queue fill before the game starts
        bots = []
        per_host = [args.players // args.hosts + (1 if i < args.players % args.hosts else 0) for i in range(args.hosts)]
        for host, count in zip(bot_hosts, per_host):
            bots.append(start(host, host + "_bots", sys.executable, "-m", "app.netclient",
                              "--server", "%s.1:%d" % (SUBNET, PORT), "--bots", str(count), "--prefix", host + "-",
                              "--linger", "0", "--duration", str(args.max_seconds),
                              "--csv", "%s_bots_%s.csv" % (prefix, host), "--label", args.case))
        deadline = time.time() + args.max_seconds + 20
        for proc in bots + [server]:
            try:
                proc.wait(timeout=max(1.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                print("a program did not finish in time; stopping it", file=sys.stderr)
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log in logs:
            log.close()
        remove_lab(everything)
    return summarize(out, args.case, args.players)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Game server and bots on separate virtual hosts (Linux namespaces).")
    ap.add_argument("--case", default="baseline", help="a name for this run (it only names the files and the "
                    "label column): baseline, congested, priority ...")
    ap.add_argument("--priority", action="store_true", help="serve the game's UDP port first in the bottleneck "
                    "(a priority class), the way an SDN controller could")
    ap.add_argument("--players", type=int, default=8, help="bots in the race (default 8)")
    ap.add_argument("--hosts", type=int, default=2, help="bot hosts the players are spread over (default 2)")
    ap.add_argument("--laps", type=int, default=1)
    ap.add_argument("--bw", type=float, default=10.0, metavar="MBIT", help="bottleneck limit in Mbit/s, both ways "
                    "(default 10; 0 = no limit)")
    ap.add_argument("--queue", type=int, default=100, metavar="PACKETS", help="queue length of the bottleneck "
                    "(default 100, about 120 ms when full of full-size packets; Mininet's TCLink default is 1000)")
    ap.add_argument("--iperf", type=float, default=0.0, metavar="MBIT", help="also send this much UDP iperf3 traffic "
                    "across the bottleneck (0 = none)")
    ap.add_argument("--iperf-dir", choices=("down", "up"), default="down",
                    help="down: server side to players (where the snapshots go, default); up: players to server side")
    ap.add_argument("--max-seconds", type=int, default=120, help="give up after this many seconds (default 120)")
    ap.add_argument("--out", default=os.path.join("results", "lab"))
    args = ap.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        print("This needs Linux and root: sudo python3 tools/netns_lab.py ...", file=sys.stderr)
        return 2
    missing = [tool for tool in ("ip", "tc") + (("iperf3",) if args.iperf else ()) if shutil.which(tool) is None]
    if missing:
        print("missing program(s): %s  (Ubuntu: sudo apt install iproute2 iperf3)" % ", ".join(missing), file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))    # a killed lab still removes its namespaces
    summary = play(args)
    print_table([summary])
    return 0 if summary["finished"] else 1


if __name__ == "__main__":
    sys.exit(main())
