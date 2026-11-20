#!/usr/bin/env python3
"""
Project 17 - fake game traffic for testing the SDN part on its own (Role 2).
Not the real game: it just sends small UDP packets at game rate and measures RTT and loss.

In the Mininet CLI:
    h1 python3 scripts/game_probe.py server &
    h2 python3 scripts/game_probe.py client --server 10.0.0.1 --register --duration 60

    # in another Mininet window / after starting congestion:
    sink iperf -s -u &
    gen iperf -c 10.0.0.102 -u -b 20M -t 20 &

The client prints one line per second: RTT avg / max (ms), jitter (ms), loss %.
With --log FILE it also writes per-packet CSV (seq, send_time, rtt_ms).
"""

import argparse
import json
import socket
import struct
import sys
import time
import urllib.request

GAME_PORT = 5005                    # same port as the real game (Role 1)
HDR = struct.Struct("!Id")          # seq (uint32), send timestamp (float64)
CONTROLLER = "http://10.0.0.254:8080"


def run_server(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    print("probe server on udp/%d" % port, flush=True)
    while True:
        data, addr = sock.recvfrom(2048)
        sock.sendto(data, addr)                          # echo = PONG


def register(ip, port, url):
    req = urllib.request.Request(url + "/game/register",
                                 data=json.dumps({"ip": ip, "port": port}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            print("registered with controller:", r.read().decode(), flush=True)
    except OSError as e:
        print("WARNING: could not register with controller (%s); continuing" % e, flush=True)


def run_client(server, port, rate, duration, payload, do_register, url, log_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((server, port))
    my_ip, my_port = sock.getsockname()
    if do_register:
        register(my_ip, my_port, url)
    sock.setblocking(False)

    pad = b"x" * max(0, payload - HDR.size)
    log = open(log_path, "w") if log_path else None
    if log:
        log.write("seq,send_time,rtt_ms\n")

    interval = 1.0 / rate
    seq = 0
    sent_in_window = 0
    rtts = []
    jitter = 0.0
    last_rtt = None
    t_end = time.time() + duration
    next_send = time.time()
    next_report = time.time() + 1

    while time.time() < t_end:
        now = time.time()
        if now >= next_send:
            sock.send(HDR.pack(seq, now) + pad)
            seq += 1
            sent_in_window += 1
            next_send += interval
        try:
            while True:
                data = sock.recv(2048)
                rseq, ts = HDR.unpack_from(data)
                rtt = (time.time() - ts) * 1000
                rtts.append(rtt)
                if last_rtt is not None:                 # RFC 3550 style running jitter
                    jitter += (abs(rtt - last_rtt) - jitter) / 16
                last_rtt = rtt
                if log:
                    log.write("%d,%.6f,%.3f\n" % (rseq, ts, rtt))
        except BlockingIOError:
            pass
        if now >= next_report:
            got = len(rtts)
            loss = 100.0 * (sent_in_window - got) / sent_in_window if sent_in_window else 0.0
            if got:
                print("rtt avg %7.1f ms  max %7.1f ms  jitter %6.1f ms  loss %5.1f %%"
                      % (sum(rtts) / got, max(rtts), jitter, max(loss, 0)), flush=True)
            else:
                print("no replies this second (loss 100 %%)", flush=True)
            rtts, sent_in_window = [], 0
            next_report += 1
        time.sleep(0.001)
    if log:
        log.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=["server", "client"])
    p.add_argument("--server", default="10.0.0.1")
    p.add_argument("--port", type=int, default=GAME_PORT)
    p.add_argument("--rate", type=float, default=30, help="packets per second")
    p.add_argument("--payload", type=int, default=200, help="bytes per packet")
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--register", action="store_true", help="register this client with the controller")
    p.add_argument("--controller", default=CONTROLLER)
    p.add_argument("--log")
    a = p.parse_args()
    if a.role == "server":
        run_server(a.port)
    else:
        run_client(a.server, a.port, a.rate, a.duration, a.payload, a.register, a.controller, a.log)


if __name__ == "__main__":
    sys.exit(main())
