#!/usr/bin/env python3
"""
Project 17 - one-command live SDN demo (Role 2). Needs Role 1's app/ folder in the same repo.

Terminal 1:  bash scripts/start_controller.sh
Terminal 2:  sudo python3 scripts/sdn_live_demo.py                  # controller ON (dynamic)
             sudo python3 scripts/sdn_live_demo.py --mode both      # baseline (off) then SDN (dynamic), side by side
             sudo python3 scripts/sdn_live_demo.py --mode off       # baseline only

The script builds the Mininet network, registers the players with the controller, races the real
game (Role 1) with bots, floods the bottleneck with iperf, and shows the controller reacting, the
OpenFlow rules, the queue counters and the results. It pauses between stages: press Enter to go on
(--no-pause runs straight through). Results are saved in results/demo/<time>/.

Options: --players N (default 4), --controller-ip IP (Ryu on another laptop), --flood MBPS (20),
         --laps N (2), --cli (open the Mininet prompt at the end).
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from functools import partial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "topo"))
os.chdir(ROOT)

import game_topo as gt                                   # noqa: E402
from mininet.cli import CLI                              # noqa: E402
from mininet.log import setLogLevel                      # noqa: E402
from mininet.net import Mininet                          # noqa: E402
from mininet.node import OVSSwitch, RemoteController     # noqa: E402

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
PAUSE = True


def banner(title, say=""):
    print("\n" + BOLD + "=" * 70 + "\n  " + title + "\n" + "=" * 70 + OFF)
    if say:
        print(DIM + "  Say: " + say + OFF)


def pause():
    if PAUSE:
        try:
            input(DIM + "\n  [Enter] to continue " + OFF)
        except EOFError:
            pass


def api(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode())


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def build(args):
    net = Mininet(topo=gt.GameTopo(players=args.players),
                  switch=partial(OVSSwitch, protocols="OpenFlow13", datapath=args.datapath),
                  controller=None, autoSetMacs=True, autoStaticArp=True)
    net.addController("c0", controller=RemoteController, ip=args.controller_ip, port=6653)
    gt.connect_root_namespace(net)
    net.start()
    gt.run_qos("setup", args.bw, args.queue)
    if args.datapath == "user":                       # only needed without the OVS kernel module
        for h in net.hosts:
            for i in h.intfList():
                h.cmd("ethtool -K %s tx off rx off >/dev/null 2>&1" % i)
        sh("ethtool -K root-eth0 tx off rx off >/dev/null 2>&1")
    time.sleep(3)
    return net


def server_running(h1):
    return "app.server" in h1.cmd("pgrep -af 'app.server' | grep -v pgrep")


def read_bots(out, tag, players):
    rows = []
    for i in range(2, players + 2):
        path = os.path.join(out, "%s_bots_h%d.csv" % (tag, i))
        if os.path.exists(path):
            with open(path, newline="") as f:
                rows += list(csv.DictReader(f))
    return rows


def avg(rows, key):
    vals = []
    for r in rows:
        try:
            vals.append(float(r[key]))
        except (KeyError, ValueError):
            pass
    return sum(vals) / len(vals) if vals else float("nan")


def run_race(net, args, mode, out):
    h1, gen, sink = net["h1"], net["gen"], net["sink"]
    tag = mode
    api(args.base, "/game/mode", {"mode": mode})

    banner("Race with controller mode = %s" % mode.upper(),
           "Four bots race the real game over UDP port 5005; the server is on h1, the players on the other switch.")
    h1.cmd("python3 -m app.server --auto-start %d --exit-after-races 1 --laps %d --label %s "
           "--stats-csv %s/%s_stats.csv > %s/%s_server.log 2>&1 &"
           % (args.players, args.laps, tag, out, tag, out, tag))
    time.sleep(1)
    for i in range(2, args.players + 2):
        net["h%d" % i].cmd("python3 -m app.netclient --server 10.0.0.1:5005 --bots 1 --prefix h%d- "
                           "--linger 0 --csv %s/%s_bots_h%d.csv --label %s > /dev/null 2>&1 &"
                           % (i, out, tag, i, tag))
    print("  Server and %d bots started. Waiting for the race to start..." % args.players)
    time.sleep(9)
    print("  Race is running.")
    pause()

    banner("Flood: %d Mbps of iperf traffic into a %d Mbps link" % (args.flood, args.bw),
           "Now a big download competes with the game on the same link.")
    sink.cmd("iperf -s -u > /dev/null 2>&1 &")
    gen.cmd("iperf -c 10.0.0.102 -u -b %dM -t %d > /dev/null 2>&1 &" % (args.flood, args.flood_secs))
    print("  Flood started for %d s. Giving the controller a few seconds to react..." % args.flood_secs)
    time.sleep(6)

    st = api(args.base, "/game/status")
    banner("What the controller did",
           "The controller measured the link, saw congestion and (in dynamic mode) gave the game priority.")
    print("  controller mode : %s" % st.get("mode"))
    print("  priority_on     : %s" % st.get("priority_on"))
    print("  link use        : %s" % st.get("util"))
    print("  players         : %s" % ", ".join(st.get("players", [])))
    print("\n  OpenFlow rules on s1 that send game traffic to the priority queue:")
    rules = [l.strip() for l in sh("ovs-ofctl -O OpenFlow13 dump-flows s1 table=0").splitlines() if "set_queue" in l]
    for l in rules[:4]:
        print("    " + re.sub(r"cookie=\S+ |duration=\S+ |table=0, ", "", l))
    if not rules:
        print("    (none: priority is off)")
    q = sh("tc -s class show dev s1-eth1")
    m = re.search(r"class htb 1:2 .*?\n\s*Sent (\d+) bytes (\d+) pkt", q)
    if m:
        print("\n  Game queue (class 1:2) has sent %s packets so far" % m.group(2))
    p = net["h2"].cmd("ping -c 5 -i 0.3 10.0.0.1")
    m = re.search(r"= [\d.]+/([\d.]+)/", p)
    if m:
        print("  Ordinary ping h2 -> h1 during the flood: %s ms average (not prioritised)" % m.group(1))
    pause()

    print("\n  Waiting for the race to finish...")
    for _ in range(120):
        if not server_running(h1):
            break
        time.sleep(2)
    gen.cmd("pkill iperf")
    sink.cmd("pkill iperf")
    rows = read_bots(out, tag, args.players)
    return {
        "mode": mode,
        "rtt_mean_ms": avg(rows, "rtt_mean_ms"),
        "rtt_p95_ms": avg(rows, "rtt_p95_ms"),
        "jitter_ms": avg(rows, "jitter_ms"),
        "loss_pct": avg(rows, "rx_loss_pct"),
        "snapshot_hz": avg(rows, "snapshot_hz"),
        "finished": "%d/%d" % (sum(1 for r in rows if r.get("finished") == "1"), args.players),
    }


def show_results(results):
    banner("Results (average over the bots)",
           "Same flood each time; only the controller mode changed.")
    print("  %-9s %9s %9s %8s %7s %9s %9s" % ("mode", "mean ms", "p95 ms", "jitter", "loss %", "snaps/s", "finished"))
    for r in results:
        print("  %-9s %9.1f %9.1f %8.1f %7.1f %9.1f %9s" % (r["mode"], r["rtt_mean_ms"], r["rtt_p95_ms"],
              r["jitter_ms"], r["loss_pct"], r["snapshot_hz"], r["finished"]))


def main():
    global PAUSE
    ap = argparse.ArgumentParser(description="Project 17 one-command SDN demo")
    ap.add_argument("--mode", choices=["dynamic", "off", "static", "both"], default="dynamic")
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--laps", type=int, default=2)
    ap.add_argument("--flood", type=int, default=20, help="iperf flood, Mbps")
    ap.add_argument("--flood-secs", type=int, default=40)
    ap.add_argument("--bw", type=int, default=10)
    ap.add_argument("--queue", type=int, default=100)
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--datapath", default="kernel", choices=["kernel", "user"])
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--cli", action="store_true", help="open the Mininet prompt before stopping")
    args = ap.parse_args()
    PAUSE = not args.no_pause
    args.base = "http://%s:8080" % args.controller_ip
    setLogLevel("warning")

    try:
        st = api(args.base, "/game/status")
    except OSError:
        sys.exit("Cannot reach the controller at %s. Start it first: bash scripts/start_controller.sh" % args.base)

    out = os.path.join("results", "demo", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    sh("chmod -R a+rwX results")

    banner("Building the network",
           "Two Open vSwitch switches, a %d Mbps bottleneck between them, game server h1, players h2-h%d."
           % (args.bw, args.players + 1))
    net = build(args)
    results = []
    try:
        loss = net.pingAll(timeout="1")
        print("  Connectivity check (pingall): %s%% dropped" % int(loss))
        pause()

        banner("Registering the players with the controller (REST API)",
               "The controller only prioritises traffic of registered game players.")
        for i in range(2, args.players + 2):
            api(args.base, "/game/register", {"ip": "10.0.0.%d" % i, "port": 0})
        print("  Registered: %s" % ", ".join(api(args.base, "/game/status").get("players", [])))
        pause()

        for mode in (["off", "dynamic"] if args.mode == "both" else [args.mode]):
            results.append(run_race(net, args, mode, out))
            time.sleep(8)                     # let the controller switch priority off between races
        show_results(results)
        print("\n  Saved in %s/" % out)
        if args.cli:
            CLI(net)
    finally:
        for h in net.hosts:
            h.cmd("pkill -f app.netclient; pkill -f app.server; pkill iperf")
        gt.run_qos("clear", args.bw)
        net.stop()
        sh("chmod -R a+rwX results")


if __name__ == "__main__":
    main()
