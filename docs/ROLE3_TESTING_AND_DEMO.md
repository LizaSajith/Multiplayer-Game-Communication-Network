# Role 3 - testing checklist, final demo script, viva answers

## 1. Testing checklist (tick before the demo; keep the evidence in `results/`)

| # | Check | How | Pass when |
|---|---|---|---|
| 1 | Unit tests | `python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .` | `OK` (278 tests) |
| 2 | Environment | `sudo bash scripts/check_env.sh --tests` | `READY` |
| 3 | Game alone | `python3 -m app.client --local --net`, F3 | RTT < 10 ms, 0 % loss, 20 snapshots/s |
| 4 | Topology | Mininet CLI `pingall` | 0 % dropped |
| 5 | Controller sees both switches | `h1 curl -s http://10.0.0.254:8080/game/status` | `"switches": [1, 2]` |
| 6 | **Socket -> SDN integration** | start server with `--sdn-url`, join players, then `.../game/status` | `"players"` lists the player IPs; they vanish after the race |
| 7 | Players 2 / 4 / 8 join and finish | `run_experiment.py --players 2,4,8 --cases baseline` | `players_finished = players` in `summary.csv` |
| 8 | Congestion hurts without SDN | `--cases congested_nosdn` | RTT mean >> baseline, snapshots/s << 20 |
| 9 | Static priority fixes it | `--cases congested_static` | RTT close to baseline, `SDN check: ok` |
| 10 | **Dynamic SDN reacts** | `--cases congested_sdn` | `priority_on` in `controller/events.csv` 2-4 s after iperf starts; `sdn_reaction_s` filled; RTT recovers in `fig_timeline` |
| 11 | Dynamic stays off when idle | `--cases idle_sdn` | `priority_on_events = 0`, `SDN check: ok` |
| 12 | Background traffic not starved | `iperf_got_mbps` in table | well above 0 (about 9 Mbit/s of 10) |
| 13 | Robustness: controller dead | stop Ryu mid-race (`--ryu external`) | game keeps running (sdn_calls.csv shows failures, race still finishes) |
| 14 | Robustness: player drops out | kill one bot process during a race | server logs timeout after 5 s, others unaffected |
| 15 | Reproducible | `--resume`, a fresh clone, same commands | same shape of results |

Check 13 is the "challenging scenario" for the rubric (service failure); congestion itself (8-10) is the other one.

## 2. Final demo script

**5-minute slot:** prepare the night before with `sudo python3 scripts/demo.py --prepare`. In the slot, run `python3 scripts/demo.py` (about 15 s): 23 test cases PASS/FAIL, then the comparison table is printed and saved to `results/project17_results.xlsx`. Open a graph from `results/plots` (made with `python3 analysis/plot.py`) and talk over the numbers; skip the manual steps below.

### Longer version (about 8 minutes)

1. (30 s) Architecture drawing from the README; name the three roles and the interface (REST `register` on JOIN).
2. (1 min) `sudo bash scripts/check_env.sh` -> `READY`. Show `git log` with all three members' commits.
3. (2 min) Live manual run (README step 5): start Ryu + topology, run server and 4 bots, show `/game/status` listing the players.
4. (2 min) Start iperf; `h2 ping` shows the queue delay; `setup_qos.sh show` shows packets in queue 1; `dump-flows | grep set_queue`; `events.csv` shows `priority_on`.
5. (2 min) Show the **prepared** graphs (`fig_timeline_n8.png`, `fig_latency.png`) and the numbers in `results/project17_results.xlsx` (sheet *SDN vs No SDN*) from the full run - never wait for the 30-minute matrix live.
6. (30 s) Mention limits honestly (downlink-only congestion, one bottleneck, 1 lap, N repetitions).

Prepare: run the full matrix the day before (`--reps 2` or 3), keep `results/` committed, keep a screen recording as backup.

## 3. Likely viva questions - short answers

* **Why UDP?** Game state is superseded by the next snapshot; retransmitting old positions only adds delay. Events are the exception: repeated in every snapshot until acknowledged.
* **How does the game talk to the SDN?** `app/sdn_client.py` calls the controller's REST API on every JOIN/leave (non-blocking worker thread, retries, reference counting per IP, CSV log). The controller installs `set_queue:1` flows for those players.
* **What does the controller measure and decide?** Every 2 s port counters of the bottleneck port -> utilisation. `>= 70 %` priority ON; `<= 30 %` for 3 polls priority OFF (hysteresis avoids flapping).
* **Why is dynamic worse than static in p95?** Reaction time: congestion starts, the next poll (0-2 s) notices, flows are installed; during that window the queue is full. Cost of dynamic: that window. Benefit: priority only when needed, fewer rules, idle traffic untouched.
* **What is the baseline?** Same topology, same queues, same traffic with the controller in `off` mode - only the policy changes, so the comparison is fair.
* **Why do 100 packets mean 120 ms?** 100 x 1500 B x 8 / 10 Mbit/s = 120 ms. A full queue *is* the delay.
* **How is p95 computed?** Pooled PING/PONG samples of all players during the race, linear interpolation (`analysis/metrics.percentile`).
* **Why two loss numbers?** Sequence-number loss misses blackouts; snapshots/s catches them.
* **What did you not test / limits?** One bottleneck, downlink congestion, bots not humans, 1 lap, results from N repetitions. (Say it before they ask.)
* **How do I reproduce?** `check_env.sh`, `run_experiment.py`, `plot.py` - three commands, every parameter is a flag and saved in `meta.json`.
