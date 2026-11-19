# Role 2 — Mininet & SDN (Project 17)

Topology, OVS QoS queues and a Ryu controller that gives the racing game's UDP traffic
priority when the bottleneck link is congested.

## Files

| File | What it does |
| --- | --- |
| `scripts/install_role2.sh` | Installs Mininet, Open vSwitch, iperf, and Ryu (in `~/ryu-env`) |
| `topo/game_topo.py` | 2 switches, 1 bottleneck link, server + 1–12 players + iperf hosts |
| `scripts/setup_qos.sh` | Creates queue 0 (other) and queue 1 (game) on the bottleneck (`setup` / `show` / `clear`) |
| `controller/game_qos.py` | Ryu app: learning switch, game classification, congestion monitor, REST API, CSV logs |
| `scripts/game_probe.py` | Fake game traffic (30 pkt/s UDP) to test the network without the real game |
| `scripts/register_players.sh` | Registers player hosts h2..h(N+1) with the controller in one command |

## Run (two terminals)

```bash
# Terminal 1 - controller
source ~/ryu-env/bin/activate
ryu-manager controller/game_qos.py --ofp-tcp-listen-port 6653 --wsapi-port 8080

# Terminal 2 - network (starts the queues automatically, opens the Mininet CLI)
sudo python3 topo/game_topo.py --players 4 --bw 10
```

Never run `sudo mn -c` while Ryu is running: it kills `ryu-manager` too.

## Real game (Role 1) through the network (Mininet CLI)

Role 1's game server listens on UDP 5005; its bots need no pygame.

```
h1 bash scripts/register_players.sh 4
h1 python3 -m app.server --auto-start 4 --exit-after-races 1 --laps 1 --label sdn --stats-csv results/sdn_stats.csv &
h2 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h2- --linger 0 --csv results/sdn_bots_h2.csv --label sdn &
h3 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h3- --linger 0 --csv results/sdn_bots_h3.csv --label sdn &
sink iperf -s -u &
gen iperf -c 10.0.0.102 -u -b 20M -t 30 &
```

Topology defaults: static ARP on, bottleneck queues of 100 packets (`--queue`).
Ryu option `GAME_MATCH=port` prioritises all UDP 5005 traffic without registration.

## Congestion test with the probe (Mininet CLI)

```
h1 python3 scripts/game_probe.py server &
sink iperf -s -u &
h2 python3 scripts/game_probe.py client --register --duration 60 &
gen iperf -c 10.0.0.102 -u -b 20M -t 20 &
```

Baseline: start Ryu with `GAME_QOS_MODE=off`, or switch at runtime:
`h1 curl -s -X POST -d '{"mode":"off"}' http://10.0.0.254:8080/game/mode`

## Addresses

| Host | IP |
| --- | --- |
| h1 (game server, UDP 5005) | 10.0.0.1 |
| h2 … h13 (players) | 10.0.0.2 … 10.0.0.13 |
| gen (iperf sender) | 10.0.0.101 |
| sink (iperf receiver) | 10.0.0.102 |
| Controller REST API (from hosts) | http://10.0.0.254:8080 |

## REST API

| Method | Path | Body |
| --- | --- | --- |
| POST | `/game/register` | `{"ip": "10.0.0.2", "port": 40000}` |
| POST | `/game/unregister` | `{"ip": "10.0.0.2"}` |
| POST | `/game/mode` | `{"mode": "off" \| "static" \| "dynamic"}` |
| GET | `/game/status` | — |

## Logs (for Role 3)

`results/controller/link_stats.csv` and `results/controller/events.csv`
(override with `GAME_QOS_LOG_DIR`).
