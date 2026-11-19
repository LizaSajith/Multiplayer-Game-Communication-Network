# Hand-over from Role 1 to Roles 2 and 3

Everything here is checked against the code (`app/protocol.py`, `app/server.py`, `app/netclient.py`) and the
tests; where this file and the code disagree, the code wins. Role 1's own README is `README_ROLE1.md`,
the byte-level protocol is `docs/PROTOCOL.md`.

## 1. The facts you need

| | |
|---|---|
| Transport | UDP, one message per datagram, never fragmented (largest datagram 543 B with 16 players and 16 events) |
| Server | `python3 -m app.server`, UDP port **5005** (`--port` to change), room of 16 (`--max-players`, up to 32) |
| Server to client | **SNAPSHOT** 20 per second to every client (all players' position, health, progress), plus replies |
| Client to server | **INPUT** 30 per second (controls), PING once a second, CMD on user action |
| Traffic you can classify | client -> server: UDP **destination** port 5005; server -> client: UDP **source** port 5005 |
| Message type | the byte at payload offset 3 (payload starts with magic `17 17`, then version 1) |
| Game state traffic | INPUT = 3 and SNAPSHOT = 9: small, periodic, latency-sensitive |
| Control traffic | JOIN 1, JOIN_ACK 2, CMD 4, PING 5, PONG 6, LEAVE 7, JOIN_REJECT 8, ROSTER_REQ 10, ROSTER 11, DISCONNECT 12 |
| Timeouts | the server drops a client after 5 s of silence; a client calls the server lost after 5 s; JOIN is retried every 0.5 s for 5 s |

**Packet sizes and bandwidth** (computed with the real encoder; "on the wire" adds 28 B of IP + UDP per datagram):

| Players | SNAPSHOT (no events / 16 events) | to each client on the wire | server total on the wire |
|---|---|---|---|
| 4 | 119 B / 279 B | 23.5 kbit/s | 94 kbit/s |
| 8 | 207 B / 367 B | 37.6 kbit/s | 301 kbit/s |
| 12 | 295 B / 455 B | 51.7 kbit/s | 620 kbit/s |
| 16 | 383 B / 543 B | 65.8 kbit/s | 1.05 Mbit/s |

Each client sends INPUT (21 B) 30 times a second plus a PING (18 B) a second: about 12 kbit/s on the wire.
So a 10 Mbit/s link carries a 16-player game with room to spare; the game becomes sensitive only when something
else fills the link (iperf) and the queue in front of the server's link grows.

**What a player notices** (so you can say what the numbers mean): the other cars are drawn 100 ms in the past and
interpolated, so one lost snapshot is invisible; two or three lost in a row make cars jump; delay above about
150 ms makes the own car feel late (the own car is drawn without interpolation delay); loss of INPUT is covered by
the next INPUT 33 ms later. Game events (hits, laps, finishes) are repeated in every SNAPSHOT until acknowledged,
so they are never lost, only late.

## 2. Role 2 - Mininet, Open vSwitch, Ryu, QoS

* **Topology idea:** `h1` = game server (10.0.0.1), `h2 ... hN` = players (headless bots), one more pair for
  background traffic (`iperf`). One switch is enough; to create congestion make the server's link the bottleneck
  (for example 10 Mbit/s with `TCLink(bw=10)`), then the iperf flow competes with the SNAPSHOTs.
* **OpenFlow match for the game:** `eth_type=0x0800, ip_proto=17 (UDP), udp_dst=5005` (players to server) and
  `udp_src=5005` (server to players). OpenFlow cannot look at the payload, so use the ports. The message type is useful
  when you analyse a capture: it is the 4th byte of the UDP payload (`03` = INPUT, `09` = SNAPSHOT).
* **Priority idea:** put UDP/5005 into a high-priority queue (or mark DSCP EF), everything else best effort; the
  controller can also react to measured congestion (port statistics or the server's CSV) and move the game flow
  to the priority queue only when the link is busy ("dynamic prioritization").
* **What to watch in the game while you do this:** client-side RTT, jitter and downlink loss from the bots (section 4),
  uplink loss from the server's CSV (section 3), and the snapshot rate each client actually gets.
* **Do not black out the network for more than 5 s** during a race unless you want players to time out.
* The server can be told to start and stop by itself so that scripts need no keyboard:
  `--auto-start N` (countdown starts when N players are in, after `--auto-start-delay` seconds, default 3) and
  `--exit-after-races K`.

Example (inside the Mininet CLI or from a script), 4 players, one race, everything logged:

```bash
h1 python3 -m app.server --auto-start 4 --exit-after-races 1 --laps 2 --label baseline \
      --stats-csv results/baseline_stats.csv --results-csv results/baseline_results.csv \
      --event-log results/baseline_events.csv &
h2 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h2- --linger 0 \
      --csv results/baseline_bots_h2.csv --label baseline &
h3 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h3- --linger 0 \
      --csv results/baseline_bots_h3.csv --label baseline &
```

`--exit-after-races K` stops the server after K finished races, or when everybody has left after the K-th race, so
a script that starts the bots with `--linger 0` never waits for the 8 s results screen.

**Queue size matters more than you would think.** The game is light (section 1), so it only suffers when the queue in
front of the bottleneck is full, and then the queue length is the delay: 1000 packets of 1500 B on 10 Mbit/s is 1.2 s,
100 packets is 120 ms. A Mininet link with `bw=` set gets a queue of about 1000 packets by default (the `txqueuelen`
of a veth; check with `tc -s qdisc show dev s1-eth1`). Set it on purpose, for example
`TCLink(bw=10, max_queue_size=100)`, and write the number into your report; section 6 shows how large the difference is.

**Fixed ARP.** When the bottleneck is overloaded, ARP replies are dropped like every other packet and a host may fail
to find the server before its first JOIN (the lab in section 6 met exactly that). Use `Mininet(topo, ..., autoStaticArp=True)`
(or call `net.staticArp()` after `net.start()`) so that the experiment measures the game and not ARP.

## 3. The server's CSV files (what Role 3 turns into plots)

All three files start with a header row and carry `label` (text from `--label`, for example `baseline`, `congested`,
`sdn`) in every row. Times are seconds since the server started. `--stats-interval` (default 1 s) sets the rate.

**`--stats-csv`** - one row per player per interval. **rx = what the server received from that player (uplink),
tx = what it sent to that player (downlink)**; rates count our datagrams (header + payload), without the 28 B of IP + UDP.

| Column | Meaning |
|---|---|
| `label`, `t_s`, `race_t_s` | label; server time; race time (0 outside a race) |
| `state` | LOBBY, COUNTDOWN, RACING or FINISHED (filter on RACING for race numbers) |
| `player_id`, `name` | who |
| `rx_pps`, `tx_pps` | datagrams per second from / to this player |
| `rx_kbps`, `tx_kbps` | kilobit per second from / to this player |
| `rx_expected`, `rx_lost`, `rx_loss_pct` | uplink packets expected from the sequence numbers, missing ones, and the ratio (cumulative since the player joined) |
| `rx_reordered`, `rx_duplicates` | uplink packets that arrived late (but within 64 of the newest) or twice (cumulative) |
| `jitter_ms` | uplink jitter, RFC 3550 estimate from the header timestamps |

**`--results-csv`** - one row per player per finished race: `race, place, player_id, name, shape, finished, finish_s,
laps_done, progress_m, health, hits, wrecks, best_lap_s, dnf`.

**`--event-log`** - every game event: `event_id, event, player_id, a, b` (see the event table in `docs/PROTOCOL.md`).

The **downlink** (server to player) is measured at the player: loss, reordering, duplicates, jitter and RTT are
in `NetClient.summary()` (next section). The server cannot know what the network lost on the way down.

## 4. Role 3 - the client library, bots and experiments

`app/netclient.py` is the whole client side of the protocol and **does not import pygame**, so it runs on any
Mininet host. Minimal bot (this is what `run_bots` does for many bots at once):

```python
import time
from app import protocol as P
from app.autopilot import Autopilot
from app.netclient import NetClient, JOINED, JOINING

c = NetClient(("10.0.0.1", 5005), "h2-1", P.SHAPE_SQUARE)
c.start()                                           # first JOIN goes out; it is re-sent until answered
pilot = None
while c.state in (JOINING, JOINED):
    c.pump()                                        # never blocks: receive, timers, INPUT, PING, retries
    if c.state == JOINED and c.track is not None and pilot is None:
        pilot = Autopilot(c.track, skill=0.85)
        c.send_cmd(P.CMD_READY, 1)
    me = c.me
    if c.state == JOINED and pilot and c.game_state == P.ST_RACING and me and not me.status & P.STATUS_FINISHED:
        c.set_controls(*pilot.control(me.x, me.y, me.heading, me.speed, c.race_time_ms / 1000.0))
    else:
        c.set_controls(0.0, 0.0)
    if c.game_state == P.ST_FINISHED:
        break
    time.sleep(0.004)
print(c.summary())
c.leave(); c.close()
```

Ready-made: `python3 -m app.netclient --server 10.0.0.1:5005 --bots 4 --prefix h2- [--start] [--duration S] [--linger S]`,
or `from app.netclient import run_bots` (returns the finished `NetClient` objects so you can read their statistics).

`NetClient.summary()` returns, per client: `rtt_n, rtt_mean_ms, rtt_p50_ms, rtt_p95_ms, rtt_max_ms` (PING/PONG round
trips; all samples are in `c.rtt_ms`), `jitter_ms`, `rx_expected, rx_lost, rx_loss_pct, rx_reordered, rx_duplicates`
(downlink, from the sequence numbers), `snapshots`, `events`, `bad_datagrams`. `c.updates_per_s()` is the snapshot
rate of the last second (20 when the network is healthy).

**Bot CSV** (the client side of an experiment, one row per bot): `python3 -m app.netclient ... --csv FILE --label TEXT`
appends the rows when the bots are done (the header is written when the file is new). Columns: `label, bot, state,
finished, place, progress_m, health, rtt_n, rtt_mean_ms, rtt_p50_ms, rtt_p95_ms, rtt_max_ms, jitter_ms, rx_expected,
rx_lost, rx_loss_pct, rx_reordered, rx_duplicates, snapshots, snapshot_hz, events, bad_datagrams, rx_packets,
rx_bytes, tx_packets, tx_bytes`. `rx_*` is the downlink (server to player), `snapshot_hz` the average snapshot rate
from the first to the last snapshot (20 when the network is healthy), `rtt_*` the PING/PONG round trips. In Python:
`from app.netclient import write_bot_csv` (`write_bot_csv(path, label, clients)`). **Use one file per bot host**
(`..._bots_h2.csv`, `..._bots_h3.csv`) and join them afterwards: two programs finishing in the same moment could
both write the header into one shared file. The server's `--stats-csv` gives the uplink loss; the two files together
are a complete picture of one case.

A client uses ordinary datagrams only, so a bot on another machine needs nothing from this repository except `app/`.
`tools/sim_race.py` runs the same race with no network at all and is the "ideal" reference for finish times.

**Suggested experiment matrix:** players 4 / 8 / 12 x (baseline, iperf congestion, iperf congestion + SDN priority),
one race each, `--label` naming the case. Per case report RTT mean and p95, jitter, downlink loss (bots) and uplink
loss (server CSV), and the snapshot rate; plot them against the number of players. Congestion: for example
`iperf -u -b 9M -t 60 -c <iperf server>` between two other hosts across the bottleneck link.

**What the loopback test showed** (real UDP, a relay dropping 10 %, duplicating 5 % and delaying 15 % of the
datagrams both ways, `tests/test_loopback.py`): four clients still join, race and receive every event; measured
downlink loss 5 to 16 % per client, uplink about 10 %, reordering and duplicates visible in both. Expect similar
numbers from `tc netem loss 10% duplicate 5% delay 20ms reorder 15%`, and use this as a sanity check of your setup.

## 5. Where things are

| You need | Look at |
|---|---|
| message layouts, event table | `docs/PROTOCOL.md` |
| constants (rates, timeouts, ports) | `app/protocol.py` (top of the file) |
| server options | `python3 -m app.server --help` |
| the client library | `app/netclient.py` (`NetClient`, `run_bots`, `parse_server`) |
| a driver for bots | `app/autopilot.py` (`Autopilot(track).control(x, y, heading, speed, race_time)` -> throttle, steer) |
| an example of impairing UDP in a test | `tests/test_loopback.py` (`LossyProxy`) |
| a ready network lab (Linux, no Mininet) and its results | `tools/netns_lab.py`, section 6 |
| `results/` | empty on purpose; put CSVs and plots there |

## 6. A network lab you can run today (no Mininet): `tools/netns_lab.py`

Linux and root only (Ubuntu: `sudo apt install iproute2 iperf3`). Every host is a Linux network namespace with its own
network stack, so the game really crosses a network. The picture is a dumbbell:

```
   srv   ib                                    h2   h3  ...   ia
     \   |                                     |    |        /
    [ left switch ] ====== bottleneck ====== [ right switch ]
```

`srv` is the game server, `h2`, `h3` run the bots, and `ia` -> `ib` is an iperf3 UDP flow across the bottleneck. The
bottleneck is limited with `tc htb` (10 Mbit/s each way) in front of a queue of 100 packets; with `--priority` UDP port
5005 gets its own class that is served first (what an SDN controller would ask a switch to do). ARP entries are fixed,
like Mininet's `staticArp`. One race is played per run; afterwards everything the tool created is removed again, also
when it is interrupted.

```bash
sudo python3 tools/netns_lab.py --case baseline  --players 8
sudo python3 tools/netns_lab.py --case congested --players 8 --iperf 14                 # iperf 14 Mbit/s toward the players
sudo python3 tools/netns_lab.py --case priority  --players 8 --iperf 14 --priority
sudo python3 tools/netns_lab.py --case congested_q1000 --players 8 --iperf 14 --queue 1000
```

Options: `--players` (8), `--hosts` (2), `--laps` (1), `--bw` (10 Mbit/s, 0 = no limit), `--queue` (100 packets),
`--iperf MBIT`, `--iperf-dir down|up` (down = same direction as the snapshots), `--priority`, `--max-seconds`,
`--out DIR` (default `results/lab`). It writes `CASE_bots_h2.csv` (one per bot host), `CASE_stats.csv`,
`CASE_results.csv`, `CASE_events.csv` and the logs of every program, and prints one summary line per case.

**Result** (8 players, 1 lap, 10 Mbit/s bottleneck, iperf3 UDP 14 Mbit/s toward the players, one race per case):

| Case | RTT mean | RTT p95 | Jitter | Downlink loss | Uplink loss | Snapshots/s | Finished |
|---|---|---|---|---|---|---|---|
| baseline (no iperf) | 4.7 ms | 4.9 ms | 2.0 ms | 0 % | 0 % | 20.0 | 8 / 8 |
| congested, queue 100 | 118 ms | 121 ms | 1.8 ms | 85 % | 0 % | 2.5 | 1 / 8 |
| congested, queue 1000 | 1150 ms | 1178 ms | 11 ms | 84 % | 0 % | 2.8 | 0 / 8 (stopped after 120 s) |
| congested, queue 100, **priority for UDP 5005** | 4.9 ms | 5.6 ms | 1.8 ms | 0 % | 0 % | 20.0 | 8 / 8 |

How to read it:

* **The delay is the queue.** 100 packets of 1500 B on 10 Mbit/s are 120 ms, 1000 packets are 1.2 s. The RTT of the game
  equals that and does not depend on the game at all.
* **The loss is much higher than the iperf flow's own (about one third)**, because the server sends the 8 SNAPSHOTs of a
  tick back to back and a full tail-drop queue has room for only about one of them: 2.5 of 20 snapshots a second arrive,
  one in eight. Small packets sent in bursts suffer most; average loss figures hide this.
* **Priority removes the problem completely**: 0 % loss and the RTT of the empty network, while iperf keeps the rest of the
  link. This is the result Roles 2 and 3 should reproduce with Open vSwitch queues and the Ryu controller.
* **Players drop out.** With queue 100, 6 of the 8 bots ended in the state `lost` (they heard nothing from the server for 5 s)
  and only one player finished; with queue 1000, 4 of 8 were lost and nobody finished within the 120 s of the run. The
  player the server serves first in each burst fares best (the host, player 1, saw 22 % loss, the others 87 to 96 %).
  The server and the clients themselves stay healthy: no crash, no bad datagram, and the race ends by the usual rules (20 s after the first finisher, or when everybody has left).
  The loss and RTT columns are means over the bots; each bot's own row is in `CASE_bots_h*.csv` (`state` = `lost`).
* **Limits of this lab:** Linux namespaces with `tc`, not Mininet, Open vSwitch or Ryu; two CPU cores shared by all programs
  (the 4.7 ms baseline RTT is mostly the bots' 4 ms loop and scheduling); one race per case, so this is a demonstration, not
  statistics. Take the shape of the numbers to Mininet, not the exact values. Mininet sketch for Role 2 (not run here,
  Mininet is not installed in this workspace): `net.addLink(s1, s2, cls=TCLink, bw=10, max_queue_size=100)` between the
  switch of the server side and the switch of the player side, with iperf between a host on each side.
