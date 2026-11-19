# PROJECT STATUS — Project 17: Multiplayer Game Communication Network (UDP)

**Role 1 – Game & UDP Application** (the user's role in the "SDN-Socket Programming – Jackfruit Mini Project"; rubric 40 marks: D1 15, D2 25).
Checkpoint 4 saved **Wed 2026-10-21** (checkpoint 3: Sun 2026-10-18 23:30 IST, checkpoint 2: Sat 2026-10-17 19:55 IST, checkpoint 1: Sat 2026-10-17 about 15:00).

* Project root: `/home/claude/project17`. If the workspace was reset, unzip `project17_role1_checkpoint.zip` first (it recreates `project17/` and `reference_web_game/`).
* On return read, in this order: **this file** → `HANDOFF.md` (rules, designs, checklist) → `README_ROLE1.md` (how to run) → `docs/HANDOVER_ROLES_2_3.md` → `docs/PROTOCOL.md`.
* State at this checkpoint: `python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .` gives **Ran 247 tests ... OK** (about 25 s). The line `cannot start server: laps must be 1..50` in that output is expected (a test passes a bad `--laps`). **Nothing is half-written on disk.**
* Standing instructions from the user: keep the same 2D racing concept (circle / square / triangle, not cars; health and distance to goal visible to everyone; damaging obstacles; everything over UDP); build Role 1 "precisely, more and more"; continue from the existing project, never rebuild it; keep `HANDOFF.md` and this file updated; implementation first, documentation second; at the end of a session leave working code plus a saved checkpoint, then a SHORT summary.

---

## RESUME FROM HERE

### Exact stopping point

**Every Role 1 build item (HANDOFF.md checklist 1–9b) is DONE and verified.** The success condition "multiple players can join and play a working multiplayer race over UDP" is met and was shown live (see Evidence). The last things done, in order:

Checkpoint 3 (2026-10-18) had everything required. Checkpoint 4 adds hardening that was done afterwards, in this order:

1. **Windows console fix** (2026-10-19): `app/console.py::tolerant_output()` is called first in `main()` of `server.py`, `netclient.py`, `client.py`; the server's `_log` also survives `UnicodeEncodeError`. A player name that the console code page (cp1252) cannot show no longer crashes the server. Tests: `tests/test_console.py`, `tests/test_server.py::LogTests`.
2. **Python / pygame matrix** (2026-10-19): `requirements.txt` uses environment markers: `pygame>=2.1` for Python < 3.14, `pygame-ce>=2.5` (imports as `pygame`) from 3.14. Suite verified on Python 3.9 to 3.14 (and a 3.15 beta) without pygame, with the window code on 3.9, 3.10, 3.13, 3.14, 3.15b4. Tests that need `protocol as P` import it unconditionally.
3. **Window fitting** (2026-10-19): `fit_size(DEFAULT_SIZE, desktop_size())` unless `--size` is given; menus are laid out for `MENU_SIZE` (1100 x 660) and drawn on a centred subsurface, or on a canvas that is `smoothscale`d down when the window is smaller (`to_menu` maps the mouse back); the F3 panel needs `room >= 160` px; `hud.draw_netpanel` skips the graph if its rect is lower than 20 px.
4. **Bot CSV for Role 3** (2026-10-19): `python3 -m app.netclient ... --csv FILE --label TEXT` (`write_bot_csv`, `BOT_CSV_COLUMNS`), `snapshot_hz` in `NetClient.summary()`. One file per bot host (two programs must not append to one file).
5. **Server exit rule fix** (2026-10-20): `--exit-after-races K` also stops when everybody left after the K-th race (`_housekeeping`); regression test in `tests/test_server.py::FullRaceTests`.
6. **Network lab** (2026-10-21): `tools/netns_lab.py` (Linux + root, namespaces + `tc htb/pfifo/u32`, iperf3). Dumbbell topology, fixed ARP, `--priority`, per-case files (`case_files`), results table. Four runs (8 players, 10 Mbit/s, iperf 14 Mbit/s toward the players): baseline RTT 4.7 ms / 0 % loss / 20 snapshots/s / 8 of 8 finished; congested queue 100: RTT 118 ms, 85 % loss, 2.5 snapshots/s, 1 of 8 finished (6 bots `lost`); congested queue 1000: RTT 1150 ms, 84 % loss, 0 of 8; priority: RTT 4.9 ms, 0 % loss, 8 of 8. Written up in `docs/HANDOVER_ROLES_2_3.md` section 6. Test: `tests/test_netns_lab.py` (7, no root needed). Lab runs write to the scratchpad or `--out`, never into the repo's `results/` (it stays empty except `.gitkeep`).
7. Full suite: **247 OK**. README, HANDOFF.md, hand-over doc and this file brought up to date; zip rebuilt and verified from a clean unzip (see Delivery).

### What is left (none of it is needed for the Role 1 success condition)

**A. Needs the user, not code**
* Run on the real Windows laptop: `py -m pip install -r requirements.txt`, then `py -m app.client --local`. The Windows window itself could not be tested in the cloud workspace (it was tested with headless pygame, screenshots and the tests). If anything looks wrong, ask the user for the exact error text or a screenshot and fix that.
* The role-breakdown `.docx` they uploaded earlier was empty (0 bytes); the Role 1 spec in `HANDOFF.md` comes from their screenshots. Tell them once, so the team can re-send it if the repo needs it.
* Hand `docs/HANDOVER_ROLES_2_3.md` to the teammates (Role 2: Mininet/OVS/Ryu/QoS; Role 3: `sdn_client.py`, `bot.py`, experiments, CSV, plots, README). Create the GitHub repo with the zip contents (the rubric wants GitHub + docs).

**B. Optional polish (HANDOFF.md item 10), only if the user asks for more**
* (a) Balance: the autopilot takes about 30 hits and is wrecked about 4 times in a 3-lap race (`python3 tools/sim_race.py --bots 4 --laps 3`). If a human finds it too hard, lower obstacle damage or count in `app/track.py` / `app/game.py`, then re-run `tests/test_track.py`, `tests/test_game.py` (they assert counts and damage) and update the numbers in README section 9 and `HANDOFF.md`.
* (b) `NetClient.leave()` while still JOINING sends nothing, so the server keeps a ghost player until its 5 s timeout.
* (c) `NetClient.handle_datagram` accepts replies only from the exact server address (right for laptops and single-network-card Mininet hosts; a multi-homed server could answer from another address).
* (d) Sparkline scale in the F3 panel is set by the largest of the last 60 RTT samples, so one startup outlier (about 70 ms) flattens the line for a while. Cosmetic.

**C. Next coding action if the user simply says "continue"**: there is no unfinished Role 1 task. Ask nothing; do B(a) only if the user complains about difficulty, otherwise tell them Role 1 is complete and point at A. Do not rebuild or rewrite anything.

### Evidence (so nobody has to re-prove it)

| Check | Result |
|---|---|
| Whole suite | 247 tests OK, about 24 s |
| `--selftest 120 --local --laps 1 --net` (window code, real UDP on loopback, 3 autopilot opponents) | race finished, exit 0, RTT mean 3.7 ms, p95 5.4 ms, jitter 0.95 ms, 0 % loss, 850 snapshots, 66 events, 0 bad datagrams |
| Earlier 8-player live run | race finished |
| `tests/test_loopback.py`: real UDP through `LossyProxy` (10 % loss, 5 % duplicates, 15 % reordered, both ways) | 4 clients join, race, receive every event; downlink loss 5–16 %, uplink about 10 %, reorder and duplicates visible in client stats and server CSV; stress-run 60x without a failure |
| Packet sizes (computed with the real encoder) | SNAPSHOT 119 / 207 / 295 / 383 B for 4 / 8 / 12 / 16 players; INPUT 21 B; largest datagram 543 B (no fragmentation) |

---

## Completed work (verified; do not redo)

| File | What it is | Checked by |
|---|---|---|
| `app/protocol.py` (539 lines) | wire format: 16-byte header, 12 message types, pack/unpack, `SeqTracker`, timing constants | `tests/test_protocol.py` (30) |
| `docs/PROTOCOL.md` (155) | full spec: handshake, reliability table, timeouts, sizes, SDN classification | sizes checked against the code |
| `app/track.py` (386) | deterministic circuit (564.5 m, 4 checkpoints, 26 obstacles), `build_track`, `Track`, `Obstacle` | `tests/test_track.py` (17), `tools/track_report.py` |
| `app/game.py` (519) | authoritative simulation: shapes, health, obstacles, laps/checkpoints, wreck and respawn | `tests/test_game.py` (33) |
| `app/autopilot.py` (99) | bot driver `Autopilot(track, skill=...)` | tests, `tools/sim_race.py`, `run_bots` |
| `app/netstats.py` (76) | `Jitter` (RFC 3550), `RateMeter`, `percentile`, `describe` | server and client runs |
| `app/server.py` (626) | UDP server: sessions, events with acks, timeouts, host handover, stats/event/results CSVs, Role 3 options | `tests/test_server.py` (30), live runs |
| `app/netclient.py` (660, **no pygame**) | join, INPUT, snapshots with interpolation, events, RTT, commands, timeouts, counters, `parse_server`, `run_bots`, CLI | `tests/test_netclient.py` (28), `test_netclient_run.py` (28), `test_netclient_wire.py` (7) |
| `app/gfx.py` (246), `app/hud.py` (327) | world drawing (camera, track, obstacles, cars, minimap), HUD (standings, own panel, toasts, countdown, results, F3 panel, `TextField`) | `tests/test_gfx_hud.py` (13), `tests/test_client_view.py` (4) |
| `app/client.py` (888) | pygame application: connect → lobby → race → results; `--local --bots --laps --auto --skill --wait --selftest --shots --net --size --server --name --shape` | `tests/test_client_app.py` (28, includes a whole race through the window code), live `--selftest` runs |
| `app/local.py` (61, no pygame) | `LocalGame`: private server on 127.0.0.1 + persistent autopilot opponents for `--local` | `tests/test_local.py` (3) |
| `app/console.py` (18) | `tolerant_output()`: stdout/stderr never raise on characters the console cannot show | `tests/test_console.py` (2) |
| `tools/netns_lab.py` (283) | Linux namespace network lab (dumbbell, `tc`, iperf3, priority class, fixed ARP), summary table | `tests/test_netns_lab.py` (7), four real runs |
| `tests/test_loopback.py` | real UDP through a lossy proxy | 3 tests, about 10 s |
| `tests/nethelpers.py` (235) | `Env`, `Wire`, `FakeClock`, `FakeSock`, `car`, `ev`, `make_ack`, `TRACK` | used by the client tests |
| `tools/sim_race.py`, `tools/track_report.py` | headless N-bot race; track picture/report | run by hand |
| `README_ROLE1.md`, `requirements.txt`, `docs/HANDOVER_ROLES_2_3.md` | user guide (one command, LAN, three windows, Mininet, troubleshooting), `pygame>=2.1`, hand-over to Roles 2 and 3 | commands run; the bot snippet was run against a real server |
| `HANDOFF.md`, `PROJECT_STATUS.md` | project memory | n/a |
| `reference_web_game/` (in the zip; originals `/home/claude/server.js`, `package.json`, `public/index.html`) | the first Node/WebSocket game; reference only (TCP, cannot meet the UDP requirement) | n/a |

The empty `results/` folder is for Role 3's CSVs and plots.

---

## Facts needed to continue

**Design decisions** (details in HANDOFF.md): authoritative server, clients send only controls; 60 Hz tick, 20 Hz snapshots, 30 Hz INPUT (5 Hz in the lobby); UDP port 5005, magic 0x1717, version 1; events are re-sent in every SNAPSHOT until acknowledged through `ack_event` (uint16 ids 1..65535, 0 = none, log of 256, at most 16 per snapshot); roster version plus ROSTER_REQ; host is the first player and passes on when they leave; timeouts 5 s, keepalive 0.5 s; JOIN retry 0.5 s up to 5 s; CMD retry 0.25 s x 20; the track is rebuilt from `track_id` and seed and verified with `track.hash` (never sent); defaults 3 laps, up to 16 players, countdown 3 s, time limit 180 s; race ends when all finished, at 180 s, or 20 s after the first finisher; results screen 8 s, then the lobby (ready flags reset).

**World:** x right, y down, heading (cos, sin), steer +1 = right; 10 units = 1 m (`app.track.UNITS_PER_METER`); `Track.length` 5644.8 units = 564.5 m; half-width 110; `PLAYER_RADIUS` 14; obstacles: static 15 HP, sweeping 20 HP, 0.8 s invulnerability after a hit; 0 HP = wreck, 3 s, respawn at last checkpoint with full health and 2 s invulnerability; cars pass through each other (ghost collisions). Shapes (`app.game.SHAPE_STATS`, max speed / accel / turn rate): circle 300/260/3.2, square 320/220/2.7, triangle 285/300/3.6.

**Codes:** `PlayerState.status` bits: WRECKED 0x01, FINISHED 0x02, INVULN 0x04, STARTED 0x08, DNF 0x10. Game states: ST_LOBBY 0, ST_COUNTDOWN 1, ST_RACING 2, ST_FINISHED 3. Shapes: 0 circle, 1 square, 2 triangle. CMD: READY 1 (arg 0/1), START 2 (host only), SHAPE 3 (arg 0..2, lobby only). Events: PLAYER_JOIN 1, LEAVE 2, RACE_START 3, RACE_END 4, HIT 5 (a = damage, b = obstacle), CHECKPOINT 6 (a = index, b = ms), LAP 7 (a = lap, b = lap ms), FINISH 8 (a = place, b = ms), WRECK 9, RESPAWN 10. Reject reasons: REJ_FULL 1, REJ_IN_PROGRESS 2, REJ_VERSION 3, REJ_BAD_REQUEST 4. Disconnect reasons: TIMEOUT 1, UNKNOWN_SESSION 2, SHUTDOWN 3, KICKED 4, LEFT 5.

**`NetClient` API** (`app/netclient.py`, no pygame; Role 3's `bot.py` reuses it):
* `NetClient(server=(host, port), name, shape, *, clock=None, sock=None, on_event=None, interp_delay=0.1, nonce=None)`.
* States are module constants `IDLE, JOINING, JOINED, REJECTED, FAILED, LOST, DISCONNECTED, LEFT` (`ACTIVE = (JOINING, JOINED)`); text in `error`; `reject_reason`, `disconnect_reason`.
* Calls: `start()` (a new session after any failure), `pump(now=None)` (every frame, never blocks), `set_controls(throttle, steer)`, `send_cmd(cmd, arg=0)`, `pending_arg(cmd)`, `leave()`, `close()`, `wait_joined(timeout)`.
* Data: `me`, `players`, `roster`, `is_host`, `game_state`, `countdown_ms`, `race_time_ms`, `race_time_now()`, `snapshot`, `track`, `ack`, `events`, `ranking()`, `remaining_m()`, `buffer.sample(now, delay=)` / `sample(delay=)`.
* Metrics: `rtt_ms` (deque), `tracker` (loss, reordered, duplicates), `jitter.value`, `updates_per_s()`, `snapshot_hz()` (average over the whole run), `packets_in/out`, `bytes_in/out`, `summary()` (dict for the experiments, includes the four traffic keys and `snapshot_hz`).
* Helpers: `parse_server("host[:port]", default_port)`, `run_bots(server, count, skill=0.85, start=False, duration=0.0, verbose=True, linger=0.0, keep_going=False, stop=None, prefix="bot")`; `write_bot_csv(path, label, clients)`; CLI `python3 -m app.netclient --server HOST:PORT --bots N [--start] [--duration S] [--skill X] [--prefix P] [--linger S] [--csv FILE] [--label TEXT]`.

**Server CLI:** `python3 -m app.server --help`. Options: `--host --port --laps --max-players --tick-rate --snapshot-rate --seed --no-respawn --time-limit --countdown --results-time --require-ready --auto-start N --auto-start-delay S --exit-after-races K --stats-csv --stats-interval --results-csv --event-log --label --quiet`. Stats CSV columns: `label, t_s, race_t_s, state, player_id, name, rx_pps, tx_pps, rx_kbps, tx_kbps, rx_expected, rx_lost, rx_loss_pct, rx_reordered, rx_duplicates, jitter_ms`.

| Network lab, 8 players (see `docs/HANDOVER_ROLES_2_3.md` section 6) | baseline 4.7 ms RTT, 0 % loss, 8/8 finished; congested (queue 100) 118 ms, 85 % loss, 1/8; congested (queue 1000) 1150 ms, 0/8; priority 4.9 ms, 0 % loss, 8/8 |

### Run / check (copy-paste)

```
cd /home/claude/project17

# 1. whole suite: expect "Ran 247 tests ... OK"
python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .
# one module only:
python3 -I -W error::ResourceWarning -m unittest discover -s tests -t . -p test_netclient_run.py

# 2. the game, no window needed (plays a whole race, about 45 s, prints RESULT: race finished, exit 0)
SDL_VIDEODRIVER=dummy python3 -m app.client --selftest 120 --local --laps 1 --net --shots /tmp/shots

# 3. other tools
python3 -m app.server --help
python3 tools/sim_race.py --bots 4 --laps 3
python3 tools/track_report.py --png track.png        # needs matplotlib

# 4. network lab (Linux + root + iproute2 + iperf3; plays one race through virtual hosts, cleans up after itself)
sudo python3 tools/netns_lab.py --case baseline  --players 8
sudo python3 tools/netns_lab.py --case congested --players 8 --iperf 14
sudo python3 tools/netns_lab.py --case priority  --players 8 --iperf 14 --priority
```

### Gotchas already met (do not rediscover)

* With `python3 -I`, run tests with `discover -s tests -t .`; `-m unittest tests.test_x` fails with `ModuleNotFoundError: No module named 'tests'`. To RUN a module (`app.client`, `app.server`, `app.netclient`) use plain `python3 -m app.xxx`: `-I` hides the project folder.
* Close the server in tests (`env.server.close()`) or open CSV files raise `ResourceWarning`.
* `Env.run(keepalive=True)` injects a PONG after every silent second: pass `keepalive=False` when counting datagrams or testing silence.
* A background `&` job in a non-interactive shell ignores SIGINT. Let servers exit with `--exit-after-races` or use `GameServer.start_thread()` / `stop()` in-process.
* Linux holds only about 256 tiny UDP datagrams in a default receive buffer: a burst of 300 loses some. The server and the loopback test set `SO_RCVBUF` (1–2 MB) and pace their sends; do the same in any new stress test.
* Windows: Python's `ioctl` cannot set `SIO_UDP_CONNRESET`, so server and client catch `OSError` around `recvfrom`; server and netclient call `timeBeginPeriod(1)` (the pygame window gets 1 ms timers from SDL).
* Jitter tests must stamp `ts` from the send time, not the arrival time.
* Headless pygame: `SDL_VIDEODRIVER=dummy`; screenshots are PNG files viewed with the Read tool.
* Windows redirected output uses a legacy code page: never `print` user-supplied text without `tolerant_output()` / `errors="replace"`.
* The lab: running `tools/netns_lab.py` as root really builds namespaces. Never run it with a short tool timeout (a SIGTERM is handled, but check `ip netns list` and `ip -br link | grep t17` afterwards). `ip netns del` is asynchronous in the kernel (`remove_lab` waits). `pkill -f netns_lab` can kill your own shell. No `netem` in this sandbox kernel, so use `htb` + `pfifo` only. Overloaded links drop ARP too: the lab uses fixed ARP entries.
* `rsync` is not installed; use Python's `zipfile`. The workspace is ephemeral: the zip sent to the user is the durable copy.

### Notes about the user

* Student on a Windows laptop (PowerShell). Likes exact, numbered, copy-paste steps and simple words. Asks for the status and exact next steps whenever a session might end.
* Roles 2 (Mininet, Ryu, QoS) and 3 (`sdn_client.py`, `bot.py`, experiments, plots, README) belong to teammates. They depend on port 5005, the packet format, `NetClient`, the CSV outputs, and on the code still running after every merge (re-run the 247 tests after any merge).

### Delivery

`project17_role1_checkpoint.zip` = project folder plus `reference_web_game/`, no `__pycache__`. Rebuilt at checkpoint 4 (2026-10-21), unzipped into a clean folder, compared file by file, and the 247 tests were run there before it was sent in the chat. Older copies on the user's side (checkpoint 1 to 3 zips, `project17_role1_progress.zip`) are outdated. Re-send a fresh zip whenever a milestone is finished.
