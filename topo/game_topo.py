#!/usr/bin/env python3
r"""
Project 17 - Mininet topology for the multiplayer racing game (Role 2).

Layout (s1-s2 is the bottleneck link, port 1 on both switches):

    h1 (game server) ---\                     /--- h2 .. h(N+1)  (players)
                         s1 ====bottleneck==== s2
    gen (iperf sender) -/  \                  \--- sink (iperf receiver)
                            root-ns (10.0.0.254, lets hosts reach the Ryu REST API)

Run (Ryu must already be running, see README / guide):
    sudo python3 topo/game_topo.py                 # 4 players, QoS queues on, CLI
    sudo python3 topo/game_topo.py --players 12
    sudo python3 topo/game_topo.py --bw 10 --delay 5

Notes
- The bottleneck rate is set by OVS QoS (scripts/setup_qos.sh), NOT by TCLink bw=,
  because both write tc rules on the same interface and would overwrite each other.
- Both baseline and SDN runs use the same queues; only the controller's policy
  (off / static / dynamic) changes. That keeps the comparison fair.
"""

import argparse
import os
import subprocess
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

HERE = os.path.dirname(os.path.abspath(__file__))
QOS_SCRIPT = os.path.join(HERE, "..", "scripts", "setup_qos.sh")

SERVER_IP = "10.0.0.1"
GEN_IP = "10.0.0.101"
SINK_IP = "10.0.0.102"
ROOT_IP = "10.0.0.254"          # controller REST API is reached at this address
BOTTLENECK_PORT = 1             # s1-eth1 and s2-eth1


class GameTopo(Topo):
    """Two switches joined by one bottleneck link; server side and player side."""

    def build(self, players=4, delay_ms=0):
        s1 = self.addSwitch("s1", dpid="0000000000000001")
        s2 = self.addSwitch("s2", dpid="0000000000000002")

        # Added first, with fixed port numbers, so the bottleneck is always port 1.
        self.addLink(s1, s2, port1=BOTTLENECK_PORT, port2=BOTTLENECK_PORT)

        # Server side
        h1 = self.addHost("h1", ip=SERVER_IP + "/8")
        gen = self.addHost("gen", ip=GEN_IP + "/8")
        self.addLink(h1, s1)
        self.addLink(gen, s1)

        # Player side. Optional delay on player access links only (not the bottleneck).
        link_opts = {"cls": TCLink, "delay": "%dms" % delay_ms} if delay_ms > 0 else {}
        for i in range(players):
            host_ip = "10.0.0.%d/8" % (i + 2)          # h2 = 10.0.0.2, h3 = 10.0.0.3, ...
            h = self.addHost("h%d" % (i + 2), ip=host_ip)
            self.addLink(h, s2, **link_opts)
        sink = self.addHost("sink", ip=SINK_IP + "/8")
        self.addLink(sink, s2)


def connect_root_namespace(net, switch_name="s1", ip=ROOT_IP):
    """Give the root namespace (where Ryu runs) an address inside the Mininet subnet,
    so h1's game server can call the controller's REST API at http://10.0.0.254:8080.
    Same approach as mininet/examples/sshd.py."""
    root = Node("root", inNamespace=False)
    link = net.addLink(root, net[switch_name])
    root.setIP(ip + "/8", intf=link.intf1)
    return root, link


def run_qos(action, bw_mbps, queue=100):
    cmd = ["bash", QOS_SCRIPT, action, str(bw_mbps), str(queue)]
    info("*** %s\n" % " ".join(cmd))
    subprocess.run(cmd, check=False)


def main():
    parser = argparse.ArgumentParser(description="Project 17 game topology")
    parser.add_argument("--players", type=int, default=4, help="number of player hosts (1-12)")
    parser.add_argument("--bw", type=int, default=10, help="bottleneck rate in Mbps")
    parser.add_argument("--delay", type=int, default=0, help="extra delay on each player link, ms")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--no-qos", action="store_true",
                        help="do not create OVS queues (only for debugging connectivity)")
    parser.add_argument("--datapath", default="kernel", choices=["kernel", "user"],
                        help="OVS datapath; 'user' only for machines without the OVS kernel module")
    parser.add_argument("--queue", type=int, default=100,
                        help="packets each bottleneck queue can hold (100 packets at 10 Mbps = ~120 ms)")
    parser.add_argument("--no-static-arp", action="store_true",
                        help="let hosts use normal ARP (not recommended under congestion)")
    parser.add_argument("--no-root", action="store_true",
                        help="do not connect the root namespace (REST API unreachable from hosts)")
    args = parser.parse_args()

    if not 1 <= args.players <= 12:
        parser.error("--players must be between 1 and 12")

    setLogLevel("info")
    topo = GameTopo(players=args.players, delay_ms=args.delay)
    net = Mininet(
        topo=topo,
        switch=partial(OVSSwitch, protocols="OpenFlow13", datapath=args.datapath),
        controller=None,
        autoSetMacs=True,
        autoStaticArp=not args.no_static_arp,   # ARP replies get dropped under congestion otherwise
    )
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)

    if not args.no_root:
        connect_root_namespace(net)

    net.start()

    if not args.no_qos:
        run_qos("setup", args.bw, args.queue)

    info("\n*** Game server port: UDP 5005 (Role 1). Static ARP: %s\n" % ("off" if args.no_static_arp else "on"))
    info("\n*** Hosts: h1=%s (server), players h2..h%d, gen=%s, sink=%s, controller API=%s:8080\n"
         % (SERVER_IP, args.players + 1, GEN_IP, SINK_IP, ROOT_IP))
    info("*** Try: pingall | h1 ping -c3 h2 | h1 curl -s http://%s:8080/game/status\n\n" % ROOT_IP)

    try:
        CLI(net)
    finally:
        if not args.no_qos:
            run_qos("clear", args.bw)
        net.stop()


if __name__ == "__main__":
    main()
