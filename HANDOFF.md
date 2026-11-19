# Project 17 - Role 1 handoff (Game & UDP Application)

Last updated 2026-10-21 (checkpoint 4): all Role 1 build items are done and hardened (see the checklist, items 9b and 9c). Continue from the files in this folder; do not rebuild them.
This file is the memory of the project: update the checklist at the bottom after every finished step.
**Exact resume point and next action: `PROJECT_STATUS.md`, section RESUME FROM HERE. Read it first.**

## What Role 1 must deliver (from the team role breakdown)

Own the multiplayer racing application and the UDP communication between players and the server.

Responsibilities:
1. Design the track and the basic game mechanics.
2. Implement the UDP game server.
3. Implement the pygame client.
4. Implement JOIN / JOIN_ACK and player handling.
5. Implement INPUT and SNAPSHOT messages.
6. Implement packet headers and sequence numbers.
7. Handle basic packet loss and out-of-order updates.
8. Implement game events, player timeout and lap/checkpoint logic.

Main files: `app/server.py`, `client.py`, `game.py`, `protocol.py`, `track.py`.
Success condition: multiple players can join and play a working multiplayer race over UDP.
Role 1's build-order steps: (1) basic local UDP server/client, (2) JOIN + INPUT + SNAPSHOT flow.
Roles 2 (Mininet + Ryu controller) and 3 (bots, experiments, graphs) depend on this: Role 3's `bot.py` must be able to reuse the network client, so it must not import pygame.

(The role breakdown .docx arrived empty, so this was taken from screenshots of it.)

## Design decisions already made

- Same concept as the earlier web game: circle / square / triangle shapes (not cars), health, distance to goal, damaging obstacles. Shapes differ slightly in handling (`SHAPE_STATS` in game.py).
- Authoritative server. Clients send only controls (throttle, steer) and render snapshots. Python standard library only on the server.
- Circuit track (564.5 m), 3 laps by default, 4 ordered checkpoints, 26 obstacles (static 15 HP, sweeping 20 HP, 0.8 s invulnerability). 0 HP = wrecked, 3 s, respawn at last checkpoint (`respawn=False` eliminates). Cars pass through each other.
- Rates: server tick 60 Hz, SNAPSHOT 20 Hz, client INPUT 30/s (`INPUT_HZ`). Timing constants (keepalive 0.5 s, timeouts 5 s, JOIN retry 0.5 s, CMD retry 0.25 s x 20, event log 256) live in protocol.py.
- The track is not sent: JOIN_ACK carries track_id, seed, hash; clients rebuild it with `build_track(track_id, seed)` and compare `track.hash`.
- Loss handling: state messages are superseded; events are re-sent in every SNAPSHOT until acknowledged via `ack_event` in INPUT; receivers use `SeqTracker`. Full rules are in `docs/PROTOCOL.md` (the code wins if they disagree).
- Default UDP port 5005, magic 0x1717. Added `DISC_LEFT = 5` (graceful quit / rejoin) on 2026-10-17; used as the PLAYER_LEAVE reason for LEAVE.

### Server design (decided and implemented in `app/server.py`)

- Class `GameServer(game_cfg, host, port, ..., sock=None, clock=None)`: injectable socket and clock so unit tests need no network. Methods `handle_datagram(data, addr)`, `tick()`, `serve_forever()`, `start_thread()`, `stop()`, `close()`, property `address`.
- Loop: `select` on a non-blocking UDP socket, fixed-step `Game.step()` with catch-up (max 5 steps), snapshots by accumulator (`snapshot_rate/tick_rate`). Windows: ignore `ConnectionResetError` from `recvfrom`, disable `SIO_UDP_CONNRESET`, `timeBeginPeriod(1)`.
- Per client: pid (1..255), slot (lowest free), random non-zero 32-bit session token, address, nonce, outgoing seq (+1 per datagram of any type), `SeqTracker` for incoming, `acked_event`, RFC 3550 jitter from the `ts` field, byte/packet counters, cached JOIN_ACK payload.
- Packet rules: non-JOIN packet with a wrong/unknown session -> `DISCONNECT(unknown session)`, at most 1 per second per address. Every authenticated packet updates `last_heard`; SeqTracker DUPLICATE/TOO_OLD are dropped; REORDERED packets are processed only if idempotent (CMD, PING, ROSTER_REQ, LEAVE), never INPUT.
- JOIN: wrong version -> JOIN_REJECT(REJ_VERSION); same address + same nonce -> resend the same JOIN_ACK; same address + new nonce -> old entry replaced (PLAYER_LEAVE with DISC_LEFT); only in the lobby and while not full. First player is host; if the host leaves the longest-present player becomes host. A new client's `acked_event` starts at the newest event id (no history replay).
- Events: `Game.drain_events()` tuples get uint16 ids 1..65535 (0 = none) and go into a 256-entry log. Each SNAPSHOT carries up to 16 log events newer than that client's `acked_event` (oldest first). A client's `ack_event` is accepted only if newer than the stored one and not newer than the newest id issued.
- CMD: READY (arg 0/1), SHAPE (0..2, lobby), START (host only, lobby; ignored unless every player is ready when `--require-ready`). Roster version (uint8) bumps on join, leave, ready, shape, host change, start; SNAPSHOT carries it and clients ask with ROSTER_REQ.
- Timeout: silent for `CLIENT_TIMEOUT_S` -> remove, DISCONNECT(timed out), PLAYER_LEAVE event. LEAVE removes on the first copy. Ctrl+C sends DISCONNECT(shutdown) and prints a summary.
- Options for Role 3's automated runs: `--auto-start N` (+ `--auto-start-delay S`), `--exit-after-races K`, `--stats-csv`, `--results-csv`, `--event-log`, plus `--host --port --laps --max-players --tick-rate --snapshot-rate --seed --no-respawn --time-limit --require-ready --quiet`.
- Stats CSV (one row per client per `--stats-interval`, default 1 s): t_s, race_t_s, state, player_id, name, rx_pps, tx_pps, rx_kbps, tx_kbps, rx_expected, rx_lost, rx_loss_pct, rx_reordered, rx_duplicates, jitter_ms.

### Client design (decided and implemented: `app/netclient.py` 2026-10-17, `app/client.py` + `gfx.py` + `hud.py` + `local.py` 2026-10-18)

- `app/netclient.py`: `NetClient` (no pygame): `pump()` does receive + timers (JOIN retry, 0.5 s keepalive, 1 s PING with RTT list incl. p95, CMD retry, ROSTER_REQ, INPUT at 30 Hz), `set_controls(throttle, steer)`, `send_cmd()`, `leave()` (3 x LEAVE), `snapshot_buffer` with interpolation about 100 ms behind, event de-dup by id (wrap-safe) and `on_event` callback, `ack_event` in every INPUT, builds and hash-checks the track from JOIN_ACK, server-lost detection after 5 s.
- `app/client.py`: pygame front end: connect screen (server address, name, shape 1/2/3), lobby (roster, R = ready, Enter/Space = start if host), north-up camera on the local car, track/obstacle/player drawing, HUD with every player's health bar and distance to goal, lap, place, RTT, loss, countdown and results overlay. Controls W/S/A/D or arrows.

## Status

| Item | State | How it was checked |
|---|---|---|
| `app/protocol.py` | Done (timing constants + DISC_LEFT added) | `tests/test_protocol.py` |
| `app/track.py` | Done | `tests/test_track.py` |
| `app/game.py` | Done | `tests/test_game.py` (33 tests) + headless race |
| `app/autopilot.py` | Done | used by tests and `tools/sim_race.py`; takes about 30 hits per 3-lap race, tune later |
| `docs/PROTOCOL.md` | Done | sizes checked against the code |
| `tools/track_report.py`, `tools/sim_race.py` | Done | |
| Whole suite | **247 tests pass (about 25 s)** | `python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .` (note: with `-I` use `discover`, not `-m unittest tests.test_x`; the line "cannot start server: laps must be 1..50" in the output is expected) |
| `app/netstats.py`, `app/server.py` | Done (run: `python -m app.server --help`) | live smoke test; unit tests next |
| `tests/test_server.py` | Done (30 tests) | |
| `app/netclient.py` | Done (about 590 lines, pygame-free); `start()` begins a fresh session after a failure/disconnect/leave; `parse_server`, `run_bots(... keep_going ...)`, byte/packet counters in `summary()` | 63 tests + live smoke runs (real server, `python3 -m app.netclient --bots 3 --start`) |
| `tests/nethelpers.py`, `tests/test_netclient.py` (28), `tests/test_netclient_run.py` (28), `tests/test_netclient_wire.py` (7) | Done, passing | the wire tests run the real GameServer + NetClients on fake sockets: clean race (health/hits visible to all), lobby commands, lossy race, silent client timed out, restarted server, refusals |
| `app/gfx.py`, `app/hud.py`, `app/client.py` | Done (2026-10-18), incl. the F3 network panel (RTT now/avg/p95, jitter, loss, reorder/dup, snapshots/s, kbit/s, RTT graph; `--net` starts with it open) | `tests/test_gfx_hud.py` (13) + `tests/test_client_app.py` (28, incl. a whole race through the App against the real server on a simulated clock); live headless runs with `--selftest` (4 and 8 players, screenshots viewed, F3 panel viewed) |
| `app/local.py` + `--local` | Done: one command `py -m app.client --local` = private server + 3 autopilot opponents on 127.0.0.1 | `tests/test_local.py` (3) + live `--selftest 120 --local --laps 1` (race finished, exit 0) |
| `tests/test_loopback.py` (lossy proxy) | Done (3 tests, about 10 s) | real UDP through `LossyProxy`: 10 % loss / 5 % duplicates / 15 % reordered both ways; 4 clients race, events 100 % delivered, loss/reorder/dup show in client stats and server CSV; scripted-drop JOIN retry test; stress-run 20x without a failure |
| `app/console.py` (+ guarded `_log` in the server) | Done (2026-10-19): a player name the console code page cannot show (Windows cp1252) printed as `?` instead of crashing the server or a bot | `tests/test_console.py` (2), `tests/test_server.py::LogTests`; checked to fail on the old code |
| Window sizing in `app/client.py` | Done (2026-10-19): the window fits the desktop (about 96 % x 86 %) unless `--size` is given; the menus are laid out for 1100 x 660 and are scaled down in small windows; F3 panel only when there is room | `tests/test_client_app.py` (fit_size, desktop_size, buttons inside small windows, clicks in a shrunk menu, F3 in a short window), screenshots viewed |
| Python / pygame compatibility | Done (2026-10-19): `requirements.txt` picks `pygame` up to Python 3.13 and `pygame-ce` from 3.14 (same `import pygame`) | suite passes on Python 3.9 to 3.14 without pygame; with the window code on 3.9, 3.10 (pygame 2.6.1) and 3.13, 3.14, 3.15 beta (pygame-ce 2.5.8); live races on 3.14 + pygame-ce and 3.9 + pygame |
| Bot CSV in `app/netclient.py` | Done (2026-10-19): `--csv FILE --label TEXT`, `write_bot_csv()`, `BOT_CSV_COLUMNS`, `snapshot_hz` in `summary()` | `tests/test_netclient_bots.py` (4), `tests/test_netclient_run.py` |
| Server exit rule | Fixed (2026-10-20): with `--exit-after-races K` the server also stops when everybody left after the K-th race (it used to wait for the results screen forever when bots left at once) | `tests/test_server.py::FullRaceTests` (regression test, shown to fail on the old rule) |
| `tools/netns_lab.py` | Done (2026-10-21): a dumbbell network lab with Linux namespaces + `tc` (no Mininet): server and bots on separate hosts, 10 Mbit/s bottleneck, iperf3 congestion, `--priority` class for UDP 5005, fixed ARP, results table. Results (baseline, congested queue 100 / 1000, priority) are in `docs/HANDOVER_ROLES_2_3.md` section 6 | `tests/test_netns_lab.py` (7, no root needed); 4 real runs; the lab leaves no namespaces behind (checked after every run) |
| `README_ROLE1.md`, `requirements.txt`, `docs/HANDOVER_ROLES_2_3.md` | Done (2026-10-18), updated 2026-10-21 (bot CSV, queue size and ARP advice, lab results) | the commands in them were run; the bot snippet in the hand-over was run against a real server; `results/` is empty on purpose (Role 3 uses it) |

Not part of Role 1: the earlier web game (`server.js`, `package.json`, `public/index.html`, WebSocket/TCP, touch-button fix applied) is a reference copy only; it cannot satisfy the UDP requirement.

## Checklist (tick as you go)

- [x] 1. `docs/PROTOCOL.md`
- [x] 2. `tests/test_game.py`
- [x] 3. `app/netstats.py` (Jitter, RateMeter, percentile) and `app/server.py` (written 2026-10-17; live smoke test with raw UDP clients passed: join, re-ack, roster, host-only start, countdown, snapshots, ping, unknown session, leave + host handover)
- [x] 4. `tests/test_server.py` (30 tests, fake socket + clock: handshake, resend, events, timeout, host change, unknown session, loss/jitter stats, full auto-started race with CSV logs, one real-UDP round trip). Suite = 110 tests, all pass.
- [x] 5. `app/netclient.py` and its tests (done 2026-10-17: 62 tests = tests/test_netclient.py 28 + tests/test_netclient_run.py 27 + tests/test_netclient_wire.py 7, helpers in tests/nethelpers.py, two live smoke runs). Suite = 172 tests, all pass.
- [x] 6. `app/client.py` (pygame) with `app/gfx.py` and `app/hud.py` (done 2026-10-18: connect screen, lobby with track preview and shape cards, race with camera, standings, own panel, minimap, toasts, countdown, results; `--auto`, `--selftest N`, `--shots DIR`). Suite = 214 tests, all pass.
- [x] 7. `tests/test_loopback.py`: real server + NetClients on 127.0.0.1 through a UDP proxy that drops, duplicates, reorders (done 2026-10-18; a flaky self-check was traced to the kernel dropping a 300-datagram burst into a default 256-datagram UDP buffer and fixed by pacing + a bigger buffer). Suite = 217 tests.
- [x] 8. `README_ROLE1.md`, `requirements.txt`: run on one machine (`--local`), LAN, three windows, Mininet hosts (done 2026-10-18)
- [x] 9. `docs/HANDOVER_ROLES_2_3.md` (port, classification offsets, packet sizes and bandwidth, stats CSV columns, NetClient usage, experiment matrix) and the zip (done 2026-10-18)
- [x] 9b. `--local` one-command mode (`app/local.py`), F3 network panel, `NetClient` byte/packet counters, tests for them (done 2026-10-18; suite = 224)
- [x] 9c. Hardening after checkpoint 3 (2026-10-19 to 2026-10-21): console/encoding fix, window fitting and menu scaling, Python 3.9 to 3.15 matrix with the right pygame, bot CSV + snapshot rate for Role 3, server exit-rule fix, `tools/netns_lab.py` and its results (suite = 247)
- [ ] 10. OPTIONAL polish, only if time is left: (a) balance: the autopilot takes about 30 hits and about 4 wrecks in a 3-lap race, so the obstacles may be harsher than needed (`tools/sim_race.py --bots 4 --laps 3` measures it); (b) `NetClient.leave()` while still JOINING sends nothing, so the server keeps a ghost player until its 5 s timeout; (c) `NetClient.handle_datagram` accepts replies only from the exact address it sent to: right for normal one-network-card hosts (laptops, Mininet hosts), but a server with several network cards could answer from another address and the client would ignore it.
- [ ] 11. Needs the user, not code: run `py -m app.client --local` on the real Windows laptop and confirm the window opens and plays (the Windows GUI could not be tested in the cloud workspace; headless pygame, screenshots and tests were used). Then join up with Roles 2 and 3 (they start from `docs/HANDOVER_ROLES_2_3.md`).

## Run what exists

```
cd project17
python3 -I -W error::ResourceWarning -m unittest discover -s tests -t .      # 247 tests, about 25 s
python3 -m app.client --local                         # the game window with a private server + 3 opponents (needs pygame)
python3 -m app.client --selftest 120 --local --laps 1 --net --shots OUTDIR   # same, no window: plays a whole race, prints RESULT
python3 -m app.server --help                          # start a server: python3 -m app.server --laps 2
python3 tools/sim_race.py --bots 4 --laps 3           # headless race
python3 -m app.netclient --server 127.0.0.1:5005 --bots 3 --start   # headless autopilot players joining a running server
python3 tools/track_report.py --png track.png         # needs matplotlib
sudo python3 tools/netns_lab.py --case baseline --players 8     # Linux + root only: server and bots on separate virtual hosts
```

## When you return

Say "continue Project 17 Role 1". Read `PROJECT_STATUS.md` (RESUME FROM HERE) first, then continue at the first unticked item. If the workspace was reset, attach `project17_role1_checkpoint.zip` (the newest; the older `project17_role1_progress.zip` is outdated) and unzip it first. Python 3.9 or newer is required on your laptop for the client plus `py -m pip install -r requirements.txt` (it installs `pygame`, or `pygame-ce` on Python 3.14+); Mininet needs Linux, so the server and bots will run inside Mininet hosts while the pygame window runs on the desktop or VM.
