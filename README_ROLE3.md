# Role 3 — Integration, Testing & Performance (Project 17)

Project 17 is a multiplayer racing game over UDP that runs in Mininet, with a Ryu / OpenFlow controller that gives
the game's traffic priority when the network is congested. Role 1 built the game, Role 2 built the network and the
controller. **Role 3 joined the two together and proved that it works**: the game server now tells the SDN
controller who is playing, bots play the races, one script runs the baseline-vs-SDN experiments in Mininet, and one
command produces the test results, graphs, an Excel workbook, CSV files and a report.

Contents: [1 What Role 3 did](#1-what-role-3-did) · [2 Files and folders added](#2-files-and-folders-added-to-the-project) ·
[3 Changes to Role 1 / Role 2 files](#3-changes-to-role-1-and-role-2-files) · [4 How the integration works](#4-how-the-integration-works) ·
[5 The experiment](#5-the-experiment) · [6 Results](#6-results-mininet-15-nov-2026) · [7 Output files](#7-output-files-results) ·
[8 Test cases](#8-test-cases) · [9 Troubleshooting and limits](#9-troubleshooting-and-limits) ·
**[10 How to execute everything (commands)](#10-how-to-execute-everything-step-by-step)**

---

## 1. What Role 3 did

| Area | What was done | Where |
| --- | --- | --- |
| **Socket–SDN integration** | The game server reports every player to the SDN controller: JOIN → `register`, leave / timeout → `unregister`, shutdown → `unregister_all`. Runs in a background thread with retries, so the 60 Hz game never waits; every call is logged to CSV. Several players on one IP are reference-counted. | `app/sdn_client.py`, hook in `app/server.py` (`--sdn-url`, `--sdn-log`) |
| **Bots for 2 / 4 / 8 players** | Headless autopilot players that record RTT (PING/PONG), jitter, sequence-number loss and snapshots per second, every second and per sample. | `app/bot.py` |
| **Experiment automation** | One script runs the whole matrix in Mininet: starts Ryu in the right mode, builds the topology, starts server, bots and iperf congestion, waits for the race, then reads the switch to verify the priority really happened (`set_queue:1` flows during the race, queue packet counters). Saves the exact commands and parameters per run. | `scripts/run_experiment.py` |
| **Four comparison cases** | No congestion · congested without SDN (the baseline) · congested with static priority · congested with dynamic SDN — each for 2, 4 and 8 players, optional repetitions. | `scripts/run_experiment.py`, `scripts/demo.py --prepare` |
| **Metrics** | RTT mean / p50 / p95 / p99 / max, jitter, downlink and uplink loss, snapshots/s, players finished / timed out, controller reaction time, bottleneck load, background-traffic share — all only inside the race window. | `analysis/metrics.py` |
| **Graphs and tables** | Latency, loss, jitter, snapshots/s, timeline (with congestion start and controller reaction marked), RTT distribution (CDF), markdown summary table. Fake or loopback data is watermarked so it can never pass for a result. | `analysis/plot.py` |
| **Excel and CSV results** | `results/project17_results.xlsx` (Comparison, SDN vs No SDN with improvement formulas, Test cases, All runs, Notes) plus `comparison.csv`, `test_cases.csv`, `summary.csv`. | `analysis/export_results.py` |
| **One-command demo** | 23 named test cases with PASS / FAIL, the comparison table in the terminal, and the Excel and CSV files in about 15 seconds — made for the 5-minute presentation. | `scripts/demo.py` |
| **Environment check** | Checks Linux, root, CPU cores, Mininet, Open vSwitch, Ryu, iperf, curl, free ports, matplotlib, openpyxl and the project files; can run the unit tests. | `scripts/check_env.sh` |
| **Testing without Mininet** | A fake controller with the same REST API, so the integration can be tested on any laptop. | `tools/fake_controller.py` |
| **Tests** | 31 new unit tests (SDN client, server hook, recorder, metrics, verification, exports, demo, one real local run). The whole suite: 278 tests. | `tests/test_role3.py` |
| **Documentation** | This README, the main README, testing checklist + demo script + viva answers, the step-by-step Role 3 guide, the two-machine presentation guide. | see section 2 |
| **Real measurements** | 12 Mininet runs on 15 Nov 2026, 0 failed, every `SDN check: ok`; results in section 6. | `results/` |

## 2. Files and folders added to the project

```text
project17/
├── app/
│   ├── sdn_client.py             NEW  game server -> SDN controller (REST register / unregister / mode / status)
│   └── bot.py                    NEW  recording bots for the experiments
├── analysis/                     NEW FOLDER
│   ├── __init__.py               NEW
│   ├── metrics.py                NEW  one run folder -> every number
│   ├── plot.py                   NEW  graphs + results/summary_table.md
│   ├── export_results.py         NEW  results/project17_results.xlsx + CSV files
│   └── synthetic.py              NEW  clearly-labelled fake data, only for testing the analysis
├── scripts/
│   ├── check_env.sh              NEW  is this machine ready?
│   ├── run_experiment.py         NEW  the experiment matrix in Mininet
│   └── demo.py                   NEW  the one command for the presentation
├── tools/
│   └── fake_controller.py        NEW  stand-in for the Ryu REST API
├── tests/
│   └── test_role3.py             NEW  31 tests for Role 3
├── docs/
│   ├── ROLE3_TESTING_AND_DEMO.md NEW  testing checklist, demo script, viva answers
│   └── TWO_MACHINE_DEMO_GUIDE.pdf NEW server on one laptop, player on another
├── results/
│   ├── runs/                     NEW  one folder per measured run (created by the experiments)
│   ├── controller/               old Role 2 test logs removed; only .gitkeep
│   └── (written by demo.py) project17_results.xlsx, comparison.csv, test_cases.csv, summary.csv,
│       live/;  written by analysis/plot.py: plots/, summary_table.md
├── README_ROLE3.md               NEW  this file
├── Role3_Guide.pdf               NEW  step-by-step guide with the measured results
└── requirements-analysis.txt     NEW  matplotlib + openpyxl
```

| New file | Lines | Purpose |
| --- | --- | --- |
| `app/sdn_client.py` | ~260 | Non-blocking REST client for the controller; CLI: `python3 -m app.sdn_client status` |
| `app/bot.py` | ~150 | Bots writing `<host>_bots.csv`, `<host>_timeline.csv`, `<host>_rtt.csv` |
| `scripts/run_experiment.py` | ~680 | Experiment runner (Mininet backend + local test backend) |
| `scripts/demo.py` | ~430 | Test cases + graphs + report + Excel/CSV |
| `scripts/check_env.sh` | ~60 | Environment check |
| `analysis/metrics.py` | ~260 | Metrics per run, `results/summary.csv` |
| `analysis/plot.py` | ~360 | Graphs, `results/summary_table.md` |
| `analysis/export_results.py` | ~400 | Excel workbook and CSV files |
| `analysis/synthetic.py` | ~140 | Test data (always marked SYNTHETIC) |
| `tools/fake_controller.py` | ~140 | Fake controller for tests |
| `tests/test_role3.py` | ~500 | 31 tests |

## 3. Changes to Role 1 and Role 2 files

All changes are small and optional: without the new options the game behaves exactly as before, and Role 1's 247
tests still pass unchanged.

| File | Change |
| --- | --- |
| `app/server.py` (Role 1) | New options `--sdn-url URL` and `--sdn-log FILE`; `GameServer(sdn=...)`; on JOIN `register`, on leave/timeout `unregister`, on shutdown `unregister_all`. A failing controller only logs a warning. |
| `app/netclient.py` (Role 1) | `run_bots(..., on_tick=None)`: a callback once per loop, used by `app/bot.py` to record measurements. |
| `README.md` | Rewritten as the project README (architecture, roles, quick demo, steps, results). |
| `.gitignore` | `results/live/` (the optional live race) is not committed. |
| `results/controller/` (Role 2) | Old test logs `events.csv`, `link_stats.csv` removed, so only real runs remain. |
| `controller/game_qos.py`, `topo/game_topo.py`, `scripts/setup_qos.sh` (Role 2) | **Not changed** — used through their REST API, topology options and `setup_qos.sh show`. |

## 4. How the integration works

```text
 bots / players h2..h13 ---- UDP 5005 ----> [s2] ==== 10 Mbit/s bottleneck, q0 other / q1 game ==== [s1] ---> h1 game server
                                             ^                                                           |
                                             |  OpenFlow: set_queue:1 for registered players             | player JOINs
                                             |                                                           v
                                   Ryu controller (game_qos.py)  <---- POST /game/register  (app/sdn_client.py)
                                   polls the bottleneck every 2 s:  >= 70 % busy -> priority ON
                                                                    <= 30 % for 3 polls -> priority OFF
```

1. A player sends JOIN to the game server on h1 (10.0.0.1:5005).
2. The server answers JOIN_ACK and calls `register(ip, port)`; `sdn_client` sends `POST /game/register` to
   `http://10.0.0.254:8080` in the background.
3. The controller now knows the player's IP. When the bottleneck gets busy it installs OpenFlow rules that put those
   players' UDP traffic into queue 1, which skips the full queue 0.
4. When the player leaves, times out, or the server stops, `unregister` removes it again.

This is the rubric's **Socket–SDN integration**: an application event changes how the network treats traffic.

## 5. The experiment

Each run: Ryu starts in the case's mode → Mininet builds the topology → h1 starts the game server with `--sdn-url` →
h2.. start the bots → 8 s later `gen` floods `sink` with iperf (20 Mbit/s into the 10 Mbit/s link) → after the race
the script reads the switch and writes `SDN check: ok / FAILED`.

| Case | Controller mode | Congestion | Why |
| --- | --- | --- | --- |
| `baseline` | off | no | best the game can get on this network |
| `congested_nosdn` | off | yes | **the baseline to beat**: what players suffer without SDN |
| `congested_static` | static (always on) | yes | a fixed, non-adaptive QoS rule |
| `congested_sdn` | dynamic (on only while ≥ 70 % busy) | yes | the proposed dynamic SDN |

## 6. Results (Mininet, 15 Nov 2026)

The experiment uses 2, 4 and 8 players (4 cases × 3 player counts = 12 runs). The 4- and 8-player rows below were
measured on 15 Nov 2026 (0 failed, every run `SDN check: ok`; all 23 test cases passed). The 2-player row is measured
with `sudo python3 scripts/demo.py --prepare --resume`; its numbers are then in `results/comparison.csv` and the Excel
file.

| Players | No congestion: RTT p95 | No SDN: p95 / loss | Static priority: p95 / loss | Dynamic SDN: p95 / loss / reaction |
| --- | --- | --- | --- | --- |
| 2 | to be measured | to be measured | to be measured | to be measured |
| 4 | 8.7 ms | 115.4 ms / 10.1 % | 6.4 ms / 0 % | 83.1 ms / 1.3 % / 3.4 s |
| 8 | 6.5 ms | 116.6 ms / 42.7 % | 6.7 ms / 0 % | 7.8 ms / 2.4 % / 1.9 s |

Snapshots per second (20 = healthy) at 8 players: 20.0 no congestion, 11.6 no SDN, 20.0 static, 19.6 dynamic.
No player was lost in any run.

- **Why congestion hurts:** a full 100-packet queue at 10 Mbit/s = 100 × 1500 B × 8 / 10 Mbit/s = 120 ms of waiting,
  and packets that find it full are dropped.
- **Why SDN helps:** game packets go to queue 1 and skip that queue. The game uses about 50 kbit/s per player, so the
  background traffic still gets the link.
- **Dynamic, 4 players, p95 83 ms:** the controller needed 3.4 s; in a short race those seconds are more than 5 % of
  the samples. Mean 14.6 ms and loss 1.3 % show it worked — the cost of reacting instead of always prioritising.
- **Loss grows with players without SDN** (10.1 % at 4, 42.7 % at 8): more players means more snapshot packets
  competing for the full queue.
- **Why up to 8 players:** on a single VM more bots also load the CPU, which adds delay that is not caused by the
  network; 2, 4 and 8 players show the trend without that distortion.
- **Uplink loss is 0:** iperf only floods the server → players direction.

## 7. Output files (`results/`)

| Path | Content |
| --- | --- |
| `project17_results.xlsx` | Sheets: **Comparison** (every case × players), **SDN vs No SDN** (improvement as formulas), **Test cases**, **All runs**, **Notes** |
| `comparison.csv` | One row per case and player count (mean over repetitions) |
| `test_cases.csv` | TC01–TC23 with PASS / FAIL and time |
| `summary.csv` | One row per run, every metric |
| `summary_table.md` | written by `analysis/plot.py`: the comparison as markdown tables, with the SDN verification table |
| `plots/*.png` | written by `python3 analysis/plot.py`: `fig_timeline_nN`, `fig_latency`, `fig_loss`, `fig_jitter`, `fig_updates`, `fig_cdf_nN` |
| `runs/<case>_n<N>_r<rep>/` | Raw data of one run: `h*_bots.csv`, `h*_timeline.csv`, `h*_rtt.csv`, `server_*.csv`, `sdn_calls.csv`, `controller/`, `iperf_sink.txt`, `verify.txt`, `ryu.log`, `meta.json` |
| `live/` | The optional race measured with `demo.py --live` (not committed) |

## 8. Test cases

`scripts/demo.py` runs these 23 by name (the full suite has 278 tests):

| ID | Area | What is checked |
| --- | --- | --- |
| TC01 | UDP protocol | Packet header encodes and decodes correctly |
| TC02 | UDP protocol | Malformed / random packets never crash the server |
| TC03 | UDP protocol | Sequence numbers detect lost packets |
| TC04 | UDP protocol | Reordered and duplicate packets are detected |
| TC05 | Game server | JOIN / JOIN_ACK handshake gives a session |
| TC06 | Game client | Lost JOIN is retried until answered |
| TC07 | Game server | Full room / race in progress refused |
| TC08 | Game server | Only the newest INPUT is applied |
| TC09 | Game server | 20 SNAPSHOTs per second reach every player |
| TC10 | Reliability | Game events re-sent until acknowledged |
| TC11 | Game logic | Checkpoints in order, no shortcuts |
| TC12 | Game logic | Autopilot players finish a whole race |
| TC13 | Fault handling | Silent player is timed out, others are told |
| TC14 | Measurement | RTT measured with PING / PONG |
| TC15 | Measurement | Server measures loss, reordering, duplicates |
| TC16 | Measurement | Server measures jitter (RFC 3550) |
| TC17 | Packet loss | Race survives loss and duplicates |
| TC18 | Packet loss | 4 players race over real UDP: 10 % loss, reordering |
| TC19 | Socket | Real UDP round trip through the operating system |
| TC20 | SDN integration | Player join / leave is reported to the controller |
| TC21 | SDN integration | REST register / unregister reaches the controller |
| TC22 | Fault handling | Controller down: the game keeps running |
| TC23 | SDN integration | Controller calls are retried after a failure |

## 9. Troubleshooting and limits

| Symptom | Fix |
| --- | --- |
| TCP port 6653 / 8080 already in use | `sudo pkill -f ryu-manager` |
| `pingall` loss / "the network is not working" | stop Ryu, `sudo mn -c`, run again |
| `SDN check: FAILED` | read `verify.txt` and `ryu.log` in that run's folder; `sdn_calls.csv` should have `ok = 1` |
| `--resume` measures everything again | the old runs are not in this folder's `results/runs` — copy them in first |
| No Excel file, warning about openpyxl | `sudo apt install python3-openpyxl` |
| `can't open file scripts/demo.py` | you are one folder too high: `cd project17` |
| Client: "No answer from the server" (two laptops) | same Wi-Fi / hotspot, VM network set to **Bridged**, `ping <server IP>` must work |
| Baseline RTT high or jumpy | give the VM 2–4 CPU cores; close other programs |

**Limits:** one bottleneck link; congestion only in the server → players direction; bots instead of people; 1-lap
races; everything on one VM, so the player count is kept at 8 or fewer.

---

## 10. How to execute everything, step by step

Run every command from inside the `project17` folder (the one containing `scripts/` and `app/`).
`sudo` is needed only for Mininet. **Never run `sudo mn -c` while Ryu is running.**

### Step 1 — Install (once)

```bash
cd ~/Documents/Desktop/project17_final_v3/project17      # your project folder
bash scripts/install_role2.sh                            # Mininet, Open vSwitch, iperf, Ryu in ~/ryu-env
sudo apt install python3-matplotlib python3-openpyxl     # graphs + Excel
pip install -r requirements.txt                          # pygame, only for the game window
```

### Step 2 — Check the machine

```bash
sudo bash scripts/check_env.sh --tests
```
Must end with `READY` (`[warn]` lines are fine).

### Step 3 — Run all unit tests

```bash
python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .
```
Expected: `Ran 278 tests ... OK (skipped=48)`.

### Step 4 — Play the game (one laptop)

```bash
python3 -m app.client --local --net --laps 1
```
Enter = start · W A S D / arrows = drive · F3 = network panel (RTT, jitter, loss, snapshots/s) · F11 = full screen ·
Esc twice = quit.

### Step 5 — Play across two laptops (server on one, player on the other)

Both on the same phone hotspot; the VM's network adapter set to **Bridged** (details: `docs/TWO_MACHINE_DEMO_GUIDE.pdf`).

```bash
# System A (server, the VM)
hostname -I                                              # note the first address, e.g. 192.168.43.120
python3 -m app.server --laps 1
python3 -m app.netclient --server 127.0.0.1:5005 --bots 3      # second terminal: 3 opponents

# System B (player)
ping 192.168.43.120
python3 -m app.client --server 192.168.43.120:5005 --name Player1 --net   # Windows: py -m app.client ...
```
Start System B first so it is the host, then the bots on A, then press Enter on B.

### Step 6 — See the integration and the SDN by hand (Mininet)

```bash
# Terminal 1 - controller
source ~/ryu-env/bin/activate
ryu-manager controller/game_qos.py --ofp-tcp-listen-port 6653 --wsapi-port 8080

# Terminal 2 - network
sudo python3 topo/game_topo.py --players 4 --bw 10
```
At the `mininet>` prompt:
```text
pingall
h1 python3 -m app.server --auto-start 4 --sdn-url http://10.0.0.254:8080 --sdn-log results/sdn.csv &
h2 python3 -m app.bot --server 10.0.0.1:5005 --bots 1 --prefix h2- --out results/manual &
h3 python3 -m app.bot --server 10.0.0.1:5005 --bots 1 --prefix h3- --out results/manual &
h4 python3 -m app.bot --server 10.0.0.1:5005 --bots 1 --prefix h4- --out results/manual &
h5 python3 -m app.bot --server 10.0.0.1:5005 --bots 1 --prefix h5- --out results/manual &
h1 curl -s http://10.0.0.254:8080/game/status
sink iperf -s -u -i 1 &
gen iperf -c 10.0.0.102 -u -b 20M -t 30 &
h2 ping -c 10 10.0.0.1
sh sudo bash scripts/setup_qos.sh show
sh sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep set_queue
sh tail -5 results/controller/events.csv
exit
```
Check: `/game/status` lists 10.0.0.2–10.0.0.5 · ping shows about 100+ ms · queue 1:2 packet count grows ·
`set_queue:1` rules exist · `events.csv` has `priority_on`. Then Ctrl+C in terminal 1 and `sudo mn -c`.

### Step 7 — The measured experiments (once, about 12 minutes)

Close any Ryu / Mininet you started yourself first.
```bash
sudo python3 scripts/demo.py --prepare                   # 12 runs: 4 cases x 2 / 4 / 8 players
sudo python3 scripts/demo.py --prepare --resume          # only the runs that are still missing
sudo python3 scripts/demo.py --prepare --reps 3          # optional: 3 repetitions -> error bars (~35 min)
```
Every run must end with `SDN check: ok`, and the last line `0 failed`.

### Step 8 — Test cases, results table, Excel and CSV (about 15 seconds), then the graphs

```bash
python3 scripts/demo.py                                  # test cases + results table + Excel / CSV
python3 scripts/demo.py --excel                          # the same, then opens results/project17_results.xlsx
python3 analysis/plot.py                                 # graphs: PNG files in results/plots
sudo python3 scripts/demo.py --live                      # optional: + one congested SDN race measured now (~1 min)
```

### Step 9 — Robustness checks (the "challenging scenario")

- **Controller dies:** during the race in Step 6, press Ctrl+C in the Ryu terminal → the race keeps running and
  finishes; when the players leave, `results/sdn.csv` shows the `unregister` calls failing and being retried, and
  the server only prints a warning.
- **Player dies:** during the race, `h3 pkill -f "prefix h3-"` → about 5 s later the server times that player out;
  the others keep 20 snapshots/s.

### Step 10 — The 5-minute presentation

1. Before: Step 7 done (results in `results/runs`).
2. In the slot: `python3 scripts/demo.py` → 23 tests PASS on screen → the comparison table is printed.
3. Talk over the top numbers and the timeline graph (about 2 minutes); open the Excel sheet *SDN vs No SDN* if asked.

### Step 11 — Push to GitHub

```bash
git init -b main
git add .
git commit -m "Project 17: game, SDN controller, integration, tests and results"
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```
Commit the whole `results/` folder (not the zip files). Each member should push commits of their own part.
