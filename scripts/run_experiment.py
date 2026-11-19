#!/usr/bin/env python3
"""run_experiment.py - automated, repeatable baseline-vs-SDN experiments (Role 3).

Real experiment (needs Linux, Mininet, Open vSwitch, Ryu, iperf and root)::

    sudo python3 scripts/run_experiment.py --quick                    # 4 players, 3 cases, about 5 minutes
    sudo python3 scripts/run_experiment.py                            # the full matrix: 2, 4, 8 players x 4 cases
    sudo python3 scripts/run_experiment.py --players 8 --reps 3       # 3 repetitions, for error bars

Pipeline test on any laptop (no root, no Mininet; the network is the loopback and the controller is a fake, so there
is NO congestion and the numbers say nothing about SDN - it only proves that the scripts, files and plots work)::

    python3 scripts/run_experiment.py --backend local --players 4 --cases baseline

What one run does (``run_case``):

    1. start the controller (Ryu, or use the one you started) and build the Mininet network (topo/game_topo.py)
    2. set the controller mode:  off | static | dynamic   (the case decides)
    3. h1: the game server (app.server) with --auto-start N and --sdn-url, so every player that joins is registered
       with the controller (app/sdn_client.py)
    4. h2..: the players, ``app.bot`` (RTT / loss / snapshot-rate recorder)
    5. a few seconds into the race: iperf gen -> sink floods the bottleneck (the congestion), unless the case has none
    6. about 10 s later, still mid-race: count the priority OpenFlow rules on s1 (``sample_flows``, flows_in_race.txt).
       This has to happen during the race: the controller removes a player's rules when that player leaves
    7. wait for the race to end, then record evidence that the SDN really did what it should (``verify``):
       the packet counters of the two queues of the bottleneck port (verify.txt)
    8. stop everything, save ``meta.json`` and compute the metrics (analysis/metrics.py)

The cases (the experiment matrix of the role breakdown):

    baseline          no congestion, no SDN                      - the best the game can get on this network
    congested_nosdn   iperf congestion, controller mode off       - what the players suffer without SDN  (BASELINE)
    congested_static  iperf congestion, priority always on       - a fixed, non-adaptive QoS rule
    congested_sdn     iperf congestion, priority only when busy  - the proposed dynamic SDN
    idle_sdn          no congestion, dynamic mode                - shows that the controller leaves traffic alone

Everything lands in ``results/runs/<case>_n<players>_r<rep>/``; ``python3 analysis/plot.py`` turns it into graphs.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from typing import Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analysis import metrics as M                      # noqa: E402
from app.sdn_client import SdnClient, SdnError         # noqa: E402

SERVER_IP, GEN_IP, SINK_IP, ROOT_IP = "10.0.0.1", "10.0.0.101", "10.0.0.102", "10.0.0.254"
GAME_PORT = 5005
LOCAL_CONTROLLER = "http://127.0.0.1:8080"             # from the root namespace, where this script runs
HOST_CONTROLLER = "http://%s:8080" % ROOT_IP           # from a Mininet host (the game server)
MAX_PLAYER_HOSTS = 12                                  # topo/game_topo.py limit

CASES = {
    "baseline":         {"mode": "off",     "congestion": False},
    "congested_nosdn":  {"mode": "off",     "congestion": True},
    "congested_static": {"mode": "static",  "congestion": True},
    "congested_sdn":    {"mode": "dynamic", "congestion": True},
    "idle_sdn":         {"mode": "dynamic", "congestion": False},
}
DEFAULT_CASES = ["baseline", "congested_nosdn", "congested_static", "congested_sdn"]


# ---------------------------------------------------------------------- small helpers
def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def split_bots(players: int, hosts: int) -> List[int]:
    """How many bots each player host runs: ``players`` spread as evenly as possible over ``hosts`` hosts."""
    base, extra = divmod(players, hosts)
    return [base + (1 if i < extra else 0) for i in range(hosts)]


def stop_proc(p: Optional[subprocess.Popen], sig=signal.SIGTERM, wait: float = 4.0) -> None:
    if p is None or p.poll() is not None:
        return
    try:
        p.send_signal(sig)
        p.wait(timeout=wait)
    except (subprocess.TimeoutExpired, OSError):
        try:
            p.kill()
            p.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_ryu() -> Optional[str]:
    """ryu-manager lives in ~/ryu-env (scripts/install_role2.sh); under sudo '~' is root's, so look at SUDO_USER too."""
    homes = []
    if os.environ.get("SUDO_USER"):
        try:
            homes.append(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            pass
    homes += [os.path.expanduser("~"), "/root"]
    candidates = [os.environ.get("RYU_MANAGER", "")] + [os.path.join(h, "ryu-env", "bin", "ryu-manager") for h in homes]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("ryu-manager")


def slice_csv(src: str, dst: str, t0: float, t1: float, time_col: str = "time") -> None:
    """Copy the rows of ``src`` whose time is within [t0, t1] (external controller: its logs span all runs)."""
    rows = M.read_csv(src)
    if not rows:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    import csv
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(r for r in rows if t0 <= M.num(r, time_col, 0.0) <= t1)


# ---------------------------------------------------------------------- evidence that the SDN did its job
def parse_tc_classes(text: str) -> Dict[str, dict]:
    """``tc -s class show`` -> {'1:1': {'pkts': .., 'bytes': .., 'dropped': ..}, ...} (htb classes only)."""
    out = {}
    for block in re.split(r"(?=class htb )", text):
        m = re.match(r"class htb (\d+:\d+)", block)
        s = re.search(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+)", block)
        if m and s:
            out[m.group(1)] = {"bytes": int(s.group(1)), "pkts": int(s.group(2)), "dropped": int(s.group(3))}
    return out


def parse_flows(text: str) -> dict:
    """``ovs-ofctl dump-flows`` -> number of flows that send traffic to the game queue, and the packets they matched."""
    flows = pkts = 0
    for line in text.splitlines():
        if "set_queue:1" in line:
            flows += 1
            m = re.search(r"n_packets=(\d+)", line)
            pkts += int(m.group(1)) if m else 0
    return {"priority_flows": flows, "game_flow_pkts": pkts}


def evaluate_verify(mode: str, congestion: bool, v: dict) -> dict:
    """Did the switch behave the way ``mode`` promises?  Adds ``ok`` and ``reasons`` to the evidence.

    The verdict rests on the packet counters of the two queues of the bottleneck port, which are still there after the
    race.  The priority rules are not: the controller deletes a player's rules when that player leaves, so the flow
    table read after the race holds none (``priority_flows`` is 0 by design, in every mode).  The rules are therefore
    counted while the race is running (``priority_flows_race``, see ``sample_flows``) and reported as evidence."""
    reasons = []
    gq = v.get("game_queue_pkts")
    if gq is None:
        v["ok"], v["reasons"] = None, ["no queue counters (tc not available?)"]
        return v
    in_race = v.get("priority_flows_race") or 0
    if mode == "off" and gq > 200:
        reasons.append("mode off but %d packets used the game queue" % gq)
    if mode == "static" and gq < 100:
        reasons.append("static priority: game packets did not use queue 1 (queue 1 packets=%d)" % gq)
    if mode == "dynamic" and congestion and gq < 100:
        reasons.append("dynamic priority never moved game traffic to queue 1 (queue 1 packets=%d)" % gq)
    if mode == "dynamic" and not congestion and (gq > 200 or in_race > 0):
        reasons.append("dynamic mode gave priority although nothing was congested "
                       "(queue 1 packets=%d, priority flows during the race=%d)" % (gq, in_race))
    v["ok"], v["reasons"] = (not reasons), reasons
    return v


# ---------------------------------------------------------------------- backends
class LocalBackend:
    """No network at all: processes run on this machine over the loopback, the controller is a fake.

    For testing the scripts without root or Mininet.  There is no bottleneck, so congestion cases show no effect."""
    name = "local"
    server_ip = "127.0.0.1"

    def __init__(self, args) -> None:
        self.args = args
        self.fc = None
        self.admin: Optional[SdnClient] = None

    def start(self, run_dir: str, n_hosts: int, mode: str) -> None:
        sys.path.insert(0, os.path.join(REPO, "tools"))
        from fake_controller import FakeController
        self.fc = FakeController("127.0.0.1", 0, mode="dynamic").start()
        self.admin = SdnClient(self.fc.url, retries=1)
        self.admin.set_mode(mode)

    @property
    def server_sdn_url(self) -> str:
        return self.fc.url

    def popen(self, host: str, argv: List[str], logfile) -> Optional[subprocess.Popen]:
        if host in ("gen", "sink"):                 # no iperf without a bottleneck
            return None
        return subprocess.Popen(argv, cwd=REPO, stdout=logfile, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))

    def sample_flows(self, run_dir: str) -> Optional[dict]:
        return None                                 # no switch, no flow table

    def verify(self, run_dir: str, mode: str, congestion: bool, race: Optional[dict] = None) -> dict:
        return {"ok": None, "reasons": ["local backend: there is no switch to inspect"]}

    def controller_status(self) -> dict:
        return self.admin.get_status()

    def collect_controller_logs(self, run_dir: str, t0: float, t1: float) -> None:
        pass

    def stop(self) -> None:
        if self.admin:
            self.admin.close(timeout=0.5)
        if self.fc:
            self.fc.stop()


class MininetBackend:
    """Mininet (topo/game_topo.py) + Open vSwitch + Ryu (controller/game_qos.py)."""
    name = "mininet"
    server_ip = SERVER_IP
    server_sdn_url = HOST_CONTROLLER

    def __init__(self, args) -> None:
        self.args = args
        self.net = None
        self.ryu: Optional[subprocess.Popen] = None
        self.ryu_log = None
        self.admin: Optional[SdnClient] = None
        self.run_dir = ""
        self.run_qos = None

    # ---- controller
    def _start_ryu(self, run_dir: str, mode: str) -> None:
        exe = find_ryu()
        if not exe:
            raise RuntimeError("ryu-manager not found. Run scripts/install_role2.sh, or start Ryu yourself and use "
                               "--ryu external")
        for port in (6653, 8080):
            if port_in_use(port):
                raise RuntimeError("TCP port %d is already in use (an old Ryu?). Stop it: sudo pkill -f ryu-manager"
                                   % port)
        ctl_dir = os.path.join(run_dir, "controller")
        os.makedirs(ctl_dir, exist_ok=True)
        env = dict(os.environ, GAME_QOS_MODE=mode, GAME_QOS_LOG_DIR=ctl_dir, GAME_LINK_MBPS=str(self.args.bw),
                   GAME_MATCH=self.args.match, PYTHONUNBUFFERED="1")
        self.ryu_log = open(os.path.join(run_dir, "ryu.log"), "w")
        self.ryu = subprocess.Popen([exe, os.path.join("controller", "game_qos.py"), "--ofp-tcp-listen-port", "6653",
                                     "--wsapi-port", "8080"], cwd=REPO, env=env, stdout=self.ryu_log,
                                    stderr=subprocess.STDOUT, start_new_session=True)

    def _wait_controller(self, need_switches: bool, timeout: float = 40.0) -> dict:
        end, last = time.time() + timeout, "no answer"
        while time.time() < end:
            if self.ryu is not None and self.ryu.poll() is not None:
                raise RuntimeError("Ryu stopped at start-up (see ryu.log in the run folder)")
            try:
                st = SdnClient(LOCAL_CONTROLLER, timeout=1.0, retries=1).get_status()
                if not need_switches or len(st.get("switches", [])) >= 2:
                    return st
                last = "switches connected: %s" % st.get("switches")
            except SdnError as exc:
                last = str(exc)
            time.sleep(0.5)
        raise RuntimeError("controller not ready after %.0f s (%s)" % (timeout, last))

    # ---- network
    def start(self, run_dir: str, n_hosts: int, mode: str) -> None:
        from functools import partial
        from mininet.net import Mininet
        from mininet.node import OVSSwitch, RemoteController
        from topo.game_topo import GameTopo, connect_root_namespace, run_qos
        self.run_dir, self.run_qos = run_dir, run_qos
        if self.args.ryu == "auto":
            self._start_ryu(run_dir, mode)
            self._wait_controller(need_switches=False)
        else:
            self._wait_controller(need_switches=False, timeout=5.0)
        topo = GameTopo(players=n_hosts, delay_ms=self.args.delay)
        self.net = Mininet(topo=topo, switch=partial(OVSSwitch, protocols="OpenFlow13", datapath=self.args.datapath),
                           controller=None, autoSetMacs=True, autoStaticArp=True)
        self.net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
        connect_root_namespace(self.net)
        self.net.start()
        run_qos("setup", self.args.bw, self.args.queue)
        st = self._wait_controller(need_switches=True)
        self.admin = SdnClient(LOCAL_CONTROLLER, retries=2)
        for ip in st.get("players", []):                # an external controller may still know old players
            self.admin._http("POST", "/game/unregister", {"ip": ip})
        self.admin.set_mode("off")                      # known state first: priority off, then the case's mode
        self.admin.set_mode(mode)
        self._warm_up(n_hosts)

    def _warm_up(self, n_hosts: int) -> None:
        """First packets teach the learning switch where everybody is; and prove the network works before we measure."""
        net = self.net
        h1 = net.get("h1")
        bad = []
        for i in range(n_hosts):
            ip = "10.0.0.%d" % (i + 2)
            if "1 received" not in h1.cmd("ping -c 1 -W 2 %s" % ip):
                bad.append(ip)
        if "1 received" not in net.get("gen").cmd("ping -c 1 -W 2 %s" % SINK_IP):
            bad.append("gen->sink")
        if bad:
            raise RuntimeError("the network is not working, no reply from: %s (is Ryu connected? pingall in "
                               "topo/game_topo.py)" % ", ".join(bad))
        api = h1.cmd("curl -s --noproxy '*' -m 3 %s/game/status" % HOST_CONTROLLER)
        if '"mode"' not in api:
            raise RuntimeError("h1 cannot reach the controller REST API at %s (reply: %r)" % (HOST_CONTROLLER, api[:80]))

    def popen(self, host: str, argv: List[str], logfile) -> subprocess.Popen:
        return self.net.get(host).popen(argv, cwd=REPO, stdout=logfile, stderr=subprocess.STDOUT,
                                        env=dict(os.environ, PYTHONUNBUFFERED="1"))

    def sample_flows(self, run_dir: str) -> Optional[dict]:
        """The priority rules on s1 *while the race is running*: how many there are and how many packets they matched.

        They have to be read now: when a player leaves, the controller removes that player's rules, so the flow table
        that ``verify`` reads after the race has none left.  Never raises (a failed sample must not end a run)."""
        try:
            done = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"], capture_output=True,
                                  text=True, timeout=5)
            if done.returncode != 0:
                return {"error": (done.stderr or "ovs-ofctl failed").strip()[:200]}
            with open(os.path.join(run_dir, "flows_in_race.txt"), "w") as f:
                f.write("### ovs-ofctl -O OpenFlow13 dump-flows s1   (read while the race was running)\n%s\n"
                        % done.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": str(exc)[:200]}
        sample = parse_flows(done.stdout)
        sample["t_epoch"] = round(time.time(), 3)
        return sample

    def verify(self, run_dir: str, mode: str, congestion: bool, race: Optional[dict] = None) -> dict:
        """Evidence from the switch itself after the race: the packet counters of the two queues of the bottleneck
        port, and the flow table.  ``race`` is the ``sample_flows`` result taken during the race, if there is one."""
        def run(cmd):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.TimeoutExpired):
                return ""
        flows = run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"])
        tc1 = run(["tc", "-s", "class", "show", "dev", "s1-eth1"])
        qos = run(["bash", os.path.join(REPO, "scripts", "setup_qos.sh"), "show"])
        with open(os.path.join(run_dir, "verify.txt"), "w") as f:
            f.write("### ovs-ofctl -O OpenFlow13 dump-flows s1   (after the race: the players have left, so their "
                    "priority rules are gone; flows_in_race.txt has them)\n%s\n"
                    "### tc -s class show dev s1-eth1\n%s\n### setup_qos.sh show\n%s\n" % (flows, tc1, qos))
        cls = parse_tc_classes(tc1)
        v = parse_flows(flows)
        if race and "priority_flows" in race:
            v["priority_flows_race"] = race["priority_flows"]
            v["game_flow_pkts_race"] = race.get("game_flow_pkts", 0)
        if "1:1" in cls and "1:2" in cls:
            v.update({"other_queue_pkts": cls["1:1"]["pkts"], "game_queue_pkts": cls["1:2"]["pkts"],
                      "other_queue_dropped": cls["1:1"]["dropped"], "game_queue_dropped": cls["1:2"]["dropped"]})
        return evaluate_verify(mode, congestion, v)

    def controller_status(self) -> dict:
        return self.admin.get_status()

    def collect_controller_logs(self, run_dir: str, t0: float, t1: float) -> None:
        if self.args.ryu == "external":                 # one log for all runs: keep only this run's rows
            src = os.path.join(REPO, self.args.controller_logs)
            for name in ("link_stats.csv", "events.csv"):
                slice_csv(os.path.join(src, name), os.path.join(run_dir, "controller", name), t0, t1)

    def stop(self) -> None:
        if self.admin:
            self.admin.close(timeout=0.5)
        if self.net is not None:
            try:
                self.run_qos("clear", self.args.bw)
            except Exception:
                pass
            try:
                self.net.stop()
            except Exception:
                pass
        if self.ryu is not None:
            try:
                os.killpg(self.ryu.pid, signal.SIGTERM)
                self.ryu.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                stop_proc(self.ryu, signal.SIGKILL)
        if self.ryu_log:
            self.ryu_log.close()


# ---------------------------------------------------------------------- one run
def run_case(case: str, players: int, rep: int, args, backend_factory=None) -> dict:
    spec = CASES[case]
    mode, congestion = spec["mode"], spec["congestion"]
    name = "%s_n%d_r%d" % (case, players, rep)
    run_dir = os.path.join(args.out, "runs", name)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    py = sys.executable
    n_hosts = args.hosts or min(players, MAX_PLAYER_HOSTS)
    n_hosts = max(1, min(n_hosts, players, MAX_PLAYER_HOSTS))
    meta = {"case": case, "players": players, "rep": rep, "mode": mode, "congestion": congestion,
            "backend": args.backend, "queue": args.queue, "bw_mbps": args.bw, "delay_ms": args.delay,
            "laps": args.laps, "iperf_mbps": args.iperf_rate if congestion else 0, "hosts": n_hosts,
            "match": args.match, "ryu": args.ryu, "t_start": time.time(), "finished": False, "timed_out": False}
    backend = (backend_factory or (LocalBackend if args.backend == "local" else MininetBackend))(args)
    procs: List[subprocess.Popen] = []
    files = []
    try:
        log("%s: starting (%d players on %d host(s), mode %s, congestion %s)" % (name, players, n_hosts, mode,
                                                                                   "yes" if congestion else "no"))
        backend.start(run_dir, n_hosts, mode)
        server_ip = backend.server_ip

        def spawn(host, argv, logname):
            fh = open(os.path.join(run_dir, logname), "w")
            files.append(fh)
            p = backend.popen(host, argv, fh)
            if p is not None:
                procs.append(p)
            return p

        if congestion:                                   # the iperf receiver must be listening before the sender
            spawn("sink", ["iperf", "-s", "-u", "-i", "1"], "iperf_sink.txt")
        server_cmd = [py, "-m", "app.server", "--auto-start", str(players), "--auto-start-delay", "3",
                      "--exit-after-races", "1", "--laps", str(args.laps), "--time-limit", str(args.race_limit),
                      "--label", case, "--stats-csv", os.path.join(run_dir, "server_stats.csv"),
                      "--results-csv", os.path.join(run_dir, "server_results.csv"),
                      "--event-log", os.path.join(run_dir, "server_events.csv"),
                      "--sdn-url", backend.server_sdn_url, "--sdn-log", os.path.join(run_dir, "sdn_calls.csv"),
                      "--quiet"]
        server = spawn("h1", server_cmd, "server.log")
        time.sleep(1.5)
        if server.poll() is not None:
            raise RuntimeError("the game server stopped at start-up (see server.log in %s)" % run_dir)

        bot_cmds = []
        bots = []
        for i, count in enumerate(split_bots(players, n_hosts)):
            host = "h%d" % (i + 2)
            cmd = [py, "-m", "app.bot", "--server", "%s:%d" % (server_ip, GAME_PORT), "--bots", str(count),
                   "--prefix", host + "-", "--name", host, "--out", run_dir, "--label", case,
                   "--duration", str(args.max_seconds), "--linger", "0", "--quiet"]
            bot_cmds.append(" ".join(cmd))
            bots.append(spawn(host, cmd, "%s_bot.log" % host))
        t_bots = time.time()
        meta["t_bots_start"] = t_bots
        t_iperf = t_bots + args.iperf_start
        meta["t_iperf_start"] = t_iperf                  # also in cases without iperf: the moment congestion WOULD start
        iperf_cmd = ["iperf", "-c", SINK_IP, "-u", "-b", "%dM" % args.iperf_rate, "-t", str(args.iperf_time), "-i", "1"]
        meta["commands"] = {"server": " ".join(server_cmd), "bots": bot_cmds,
                            "iperf": " ".join(iperf_cmd) if congestion else None}

        started_iperf = False
        sampled = False
        deadline = t_bots + args.max_seconds
        while time.time() < deadline and server.poll() is None:
            if congestion and not started_iperf and time.time() >= t_iperf:
                meta["t_iperf_start"] = time.time()
                spawn("gen", iperf_cmd, "iperf_gen.txt")
                started_iperf = True
                log("%s: congestion started (iperf %d Mbit/s)" % (name, args.iperf_rate))
            if not sampled and time.time() >= meta["t_iperf_start"] + args.flow_sample_after:
                sampled = True                           # mid-race: the players are registered, their rules exist
                meta["flows_in_race"] = backend.sample_flows(run_dir)
            time.sleep(0.2)
        if server.poll() is None:
            meta["timed_out"] = True
            log("%s: time limit reached, stopping the server" % name)
            stop_proc(server, signal.SIGINT, wait=6.0)
        meta["server_exit_code"] = server.returncode
        meta["t_server_end"] = time.time()

        meta["verify"] = backend.verify(run_dir, mode, congestion, meta.get("flows_in_race"))   # iperf still runs
        for b in bots:                                                       # bots finish by themselves; then SIGTERM
            end = time.time() + 15
            while b is not None and b.poll() is None and time.time() < end:
                time.sleep(0.2)
            stop_proc(b, signal.SIGTERM, wait=6.0)
        try:
            meta["controller_status"] = backend.controller_status()
        except SdnError as exc:
            meta["controller_status"] = {"error": str(exc)}
        meta["finished"] = True
    except Exception as exc:                            # keep the matrix going; the error is saved with the run
        meta["error"] = "%s: %s" % (type(exc).__name__, exc)
        with open(os.path.join(run_dir, "error.txt"), "w") as f:
            f.write(traceback.format_exc())
        log("%s: FAILED - %s" % (name, exc))
    finally:
        for p in procs:
            stop_proc(p, signal.SIGTERM, wait=3.0)
        for fh in files:
            fh.close()
        meta["t_end"] = time.time()
        try:
            backend.collect_controller_logs(run_dir, meta["t_start"], meta["t_end"])
        except Exception as exc:
            meta.setdefault("warnings", []).append("controller logs: %s" % exc)
        backend.stop()
        with open(os.path.join(run_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2, default=str)
    summary = M.summarize_run(run_dir)
    v = meta.get("verify") or {}
    show = lambda key: "-" if summary.get(key, "") == "" else summary[key]
    log("%s: RTT mean %s ms, p95 %s ms, loss %s %%, %s snapshots/s, %s players lost, reaction %s s, "
        "priority flows during the race %s, SDN check: %s"
        % (name, show("rtt_mean_ms"), show("rtt_p95_ms"), show("loss_down_pct"), show("updates_per_s"),
           summary["players_lost"], show("sdn_reaction_s"), v.get("priority_flows_race", "-"),
           {True: "ok", False: "FAILED (%s)" % "; ".join(v.get("reasons", [])), None: "n/a"}[v.get("ok")]))
    return summary


# ---------------------------------------------------------------------- preflight and main
def preflight(args) -> List[str]:
    problems = []
    if args.backend == "local":
        return problems
    if sys.platform != "linux":
        problems.append("Mininet needs Linux (use the Ubuntu VM)")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        problems.append("run as root:  sudo python3 scripts/run_experiment.py ...")
    try:
        import mininet  # noqa: F401
    except ImportError:
        problems.append("Python cannot import mininet:  sudo apt install mininet")
    for tool in ("ovs-vsctl", "ovs-ofctl", "tc", "iperf", "curl"):
        if not shutil.which(tool):
            problems.append("%s not found:  bash scripts/install_role2.sh" % tool)
    if args.ryu == "auto" and not find_ryu():
        problems.append("ryu-manager not found (expected in ~/ryu-env): bash scripts/install_role2.sh, "
                        "or start Ryu yourself and add --ryu external")
    if args.ryu == "external":
        if not SdnClient(LOCAL_CONTROLLER, timeout=1.0, retries=1).reachable():
            problems.append("--ryu external: no controller answers at %s (start Ryu first, see README_ROLE2.md)"
                            % LOCAL_CONTROLLER)
    return problems


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run_experiment.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--players", default="2,4,8", help="comma-separated player counts")
    ap.add_argument("--cases", default=",".join(DEFAULT_CASES), help="comma-separated, from: %s" % ", ".join(CASES))
    ap.add_argument("--reps", type=int, default=1, help="repetitions of every case (3 gives error bars)")
    ap.add_argument("--quick", action="store_true", help="4 players, baseline + congested_nosdn + congested_sdn, 1 rep")
    ap.add_argument("--backend", choices=["mininet", "local"], default="mininet",
                    help="'local' = loopback + fake controller, only to test the scripts (no congestion!)")
    ap.add_argument("--ryu", choices=["auto", "external"], default="auto",
                    help="auto: this script starts and stops Ryu for every run; external: you started it")
    ap.add_argument("--controller-logs", default=os.path.join("results", "controller"),
                    help="where an external Ryu writes its CSVs (relative to the repo)")
    ap.add_argument("--match", choices=["registered", "port"], default="registered",
                    help="registered: only players that the game server announced get priority (SDN integration)")
    ap.add_argument("--out", default="results", help="results folder (runs go to OUT/runs)")
    ap.add_argument("--laps", type=int, default=1, help="laps per race (1 lap is about 30-40 s)")
    ap.add_argument("--hosts", type=int, default=0, help="player hosts in Mininet (0 = one per player, max 12)")
    ap.add_argument("--bw", type=int, default=10, help="bottleneck rate in Mbit/s")
    ap.add_argument("--queue", type=int, default=100, help="bottleneck queue length in packets (100 = about 120 ms)")
    ap.add_argument("--delay", type=int, default=0, help="extra delay in ms on every player link")
    ap.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    ap.add_argument("--iperf-rate", type=int, default=20, help="offered background load in Mbit/s")
    ap.add_argument("--iperf-start", type=float, default=8.0,
                    help="seconds after the players start until the congestion starts (the race starts after about 7 s)")
    ap.add_argument("--iperf-time", type=int, default=90, help="how long iperf sends, in seconds")
    ap.add_argument("--flow-sample-after", type=float, default=10.0,
                    help="seconds after the congestion start when the priority rules on s1 are counted (mid-race)")
    ap.add_argument("--race-limit", type=float, default=90.0, help="server time limit of the race in seconds")
    ap.add_argument("--max-seconds", type=float, default=150.0, help="hard limit for one run")
    ap.add_argument("--resume", action="store_true", help="skip runs that already finished")
    ap.add_argument("--stop-on-error", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the commands, run nothing")
    ap.add_argument("--check", action="store_true", help="only check that this machine is ready")
    ap.add_argument("--verbose", action="store_true", help="show Mininet's own messages")
    return ap


def plan(args) -> List[tuple]:
    players = [int(x) for x in args.players.split(",") if x.strip()]
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    for c in cases:
        if c not in CASES:
            raise SystemExit("unknown case %r (choose from %s)" % (c, ", ".join(CASES)))
    for n in players:
        if not 1 <= n <= 16:
            raise SystemExit("--players: %d is not between 1 and 16" % n)
    return [(c, n, r) for n in players for c in cases for r in range(1, args.reps + 1)]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.quick:
        args.players, args.cases, args.reps = "4", "baseline,congested_nosdn,congested_sdn", 1
    runs = plan(args)
    os.chdir(REPO)
    args.out = os.path.abspath(args.out)

    log("%d run(s): %s" % (len(runs), ", ".join("%s/n%d/r%d" % r for r in runs[:6]) + (" ..." if len(runs) > 6 else "")))
    if args.dry_run:
        for case, n, rep in runs:
            hosts = max(1, min(args.hosts or min(n, MAX_PLAYER_HOSTS), n, MAX_PLAYER_HOSTS))
            print("\n== %s, %d players, rep %d  (mode %s, congestion %s)" % (
                case, n, rep, CASES[case]["mode"], CASES[case]["congestion"]))
            print("   bots per host: %s" % split_bots(n, hosts))
            print("   h1:   python3 -m app.server --auto-start %d --exit-after-races 1 --laps %d --sdn-url ..." % (n, args.laps))
            print("   h2..: python3 -m app.bot --server %s:%d --bots K --prefix hX- --out <run folder>" % (SERVER_IP, GAME_PORT))
            if CASES[case]["congestion"]:
                print("   gen:  iperf -c %s -u -b %dM -t %d   (from second %.0f)" % (
                    SINK_IP, args.iperf_rate, args.iperf_time, args.iperf_start))
        est = len(runs) * (args.max_seconds / 3 + 25) / 60
        print("\nestimated time: about %.0f minutes" % est)
        return 0

    problems = preflight(args)
    if problems:
        print("This machine is not ready:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 2
    if args.check:
        print("ready: Mininet, Open vSwitch, Ryu, iperf and root are all available")
        return 0
    if args.backend == "mininet":
        from mininet.log import setLogLevel
        setLogLevel("info" if args.verbose else "warning")
        if args.ryu == "auto":
            subprocess.run(["mn", "-c"], capture_output=True)       # clear leftovers; Ryu is not running yet

    os.makedirs(os.path.join(args.out, "runs"), exist_ok=True)
    done = failed = 0
    t0 = time.time()
    for case, n, rep in runs:
        meta_path = os.path.join(args.out, "runs", "%s_n%d_r%d" % (case, n, rep), "meta.json")
        if args.resume and os.path.exists(meta_path):
            try:
                if M.load_meta(os.path.dirname(meta_path)).get("finished"):
                    log("%s n%d r%d: already done, skipped" % (case, n, rep))
                    continue
            except (ValueError, OSError):
                pass
        try:
            run_case(case, n, rep, args)
            done += 1
        except KeyboardInterrupt:
            log("interrupted - cleaning up; use --resume to continue later")
            if args.backend == "mininet" and args.ryu == "auto":
                subprocess.run(["mn", "-c"], capture_output=True)
            break
        time.sleep(2.0)                                 # let sockets and interfaces settle
        meta = M.load_meta(os.path.join(args.out, "runs", "%s_n%d_r%d" % (case, n, rep)))
        if meta.get("error"):
            failed += 1
            if args.stop_on_error:
                break
    M.write_summary(os.path.join(args.out, "runs"), os.path.join(args.out, "summary.csv"))
    log("finished %d run(s) (%d failed) in %.1f minutes" % (done, failed, (time.time() - t0) / 60))
    log("next:  python3 analysis/plot.py --runs %s --summary %s" % (
        os.path.join(args.out, "runs"), os.path.join(args.out, "summary.csv")))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
