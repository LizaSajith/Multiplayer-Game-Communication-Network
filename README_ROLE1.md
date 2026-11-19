# Project 17 - Multiplayer game over UDP: Role 1 (game and UDP application)

A top-down 2D race for up to 16 players. You pick a **circle, square or triangle**. Everyone sees everyone's
position, **health** and **distance to the goal** live, and the track has **obstacles that damage you**.
All of it travels over **UDP** (port 5005) with a small binary protocol; the server decides what is true,
the clients only send their controls and draw what the server tells them.

This part is the game itself: track and rules, the UDP server, the pygame client, the protocol (headers,
sequence numbers, loss and out-of-order handling), events, timeouts and lap/checkpoint logic.
The network (Mininet, SDN controller, QoS) and the experiments are Roles 2 and 3; what they need from this
part is in [`docs/HANDOVER_ROLES_2_3.md`](docs/HANDOVER_ROLES_2_3.md).

---

## 1. Try it in one command (Windows PowerShell)

You need Python 3.9 or newer from python.org (tick "Add python.exe to PATH" in the installer). The test suite
passes on Python 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14 (and a 3.15 beta). The `pip install` line below chooses the
right game library for your Python by itself: the official `pygame` up to Python 3.13, and its drop-in
replacement `pygame-ce` for 3.14 and newer (the official one has no ready-made Windows package for 3.14 yet).

```powershell
cd C:\path\to\project17
py -m pip install -r requirements.txt
py -m app.client --local
```

(`py` is the Windows launcher; if it is not found, use `python` instead. On Linux / macOS use `python3`.)

A window opens, you are in a lobby with three autopilot opponents. Press **Enter** to start the race.

| Key | What it does |
|---|---|
| W / Up arrow | accelerate |
| S / Down arrow | brake, then reverse |
| A / Left, D / Right | steer (smoothly: full lock takes 0.2 s) |
| R | ready / not ready (lobby) |
| 1, 2, 3 | change shape: circle, square, triangle (lobby) |
| Enter or Space | start the race (host only; the first player to join is the host) |
| Esc | leave (press twice during a race) |
| F3 | network details panel during a race: RTT (now / average / 95th percentile), jitter, loss, reordered and duplicate packets, snapshots per second, kbit/s down and up, and an RTT graph (`--net` starts with it open) |
| F11 | full screen |

The kbit/s in that panel count the game's own datagrams (header + payload); the on-the-wire numbers in
`docs/HANDOVER_ROLES_2_3.md` add 28 bytes of IP + UDP header to each datagram, so they are a little higher.
The mouse works on the buttons too.
The window opens at 1100 x 700, or smaller if your screen is small (it measures the screen first, so a laptop with
display scaling does not get a window with the bottom cut off). You can drag its edges; the menus shrink to fit.
`--size 900x560` forces a size. Hold the throttle during the 3-2-1 countdown for a flying start.
Cars pass through each other (ghost collisions), so a late packet can never cause an unfair crash;
only walls and obstacles hurt. At 0 HP you are wrecked, and after 3 s you respawn at your last checkpoint.

**Check the installation without a window** (plays one whole race by autopilot, about 45 s):

```powershell
py -m app.client --selftest 120 --local --laps 1
```

It ends with `RESULT: race finished` and exit code 0.

---

## 2. Play with other people on the same network

On the computer that is the server (find its address with `ipconfig`, line "IPv4 Address", for example 192.168.1.20):

```powershell
cd C:\path\to\project17
py -m app.server
```

The first time, Windows asks whether to allow Python through the firewall: allow it. If people still cannot
connect, open the port once (PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "Race17 UDP 5005" -Direction Inbound -Protocol UDP -LocalPort 5005 -Action Allow
```

On every player's computer:

```powershell
cd C:\path\to\project17
py -m pip install -r requirements.txt
py -m app.client --server 192.168.1.20:5005 --name Ana --shape triangle
```

or just run `py -m app.client` and type the address on the first screen. The first player in the lobby is the
host and starts the race. Everybody who joins after a race has started must wait for the next one.

Useful server options (all are optional; `py -m app.server --help` lists them):

| Option | Meaning |
|---|---|
| `--laps 3` | laps per race (1 to 50) |
| `--max-players 16` | room size (up to 32) |
| `--require-ready` | the host can start only when everybody is ready |
| `--auto-start N` | start the countdown by itself once N players are in (for scripted runs) |
| `--exit-after-races K` | stop the server after K finished races |
| `--no-respawn` | wrecked players are out |
| `--time-limit 180` | seconds; the race also ends 20 s after the first finisher |
| `--stats-csv f.csv`, `--results-csv f.csv`, `--event-log f.csv` | log per-player network statistics, results, events |
| `--label text` | copied into every CSV row (for example `baseline` or `sdn`) |

## 3. Several programs on one computer (what the demo and the tests do)

Three PowerShell windows:

```powershell
py -m app.server                                   # window 1: the server
py -m app.client --server 127.0.0.1:5005           # window 2: you (join first, so you are the host)
py -m app.netclient --server 127.0.0.1:5005 --bots 3   # window 3: three autopilot opponents (start after window 2 joined)
```

`app.netclient` is also the reference for Role 3's bots: `NetClient` is the whole client side of the protocol with
no pygame in it (`from app.netclient import NetClient, run_bots`).

## 4. On Mininet hosts

Mininet hosts have no screen, so use the headless programs there:

```bash
h1 python3 -m app.server --port 5005 --auto-start 4 --exit-after-races 1 --label baseline \
      --stats-csv results/stats.csv --results-csv results/results.csv --event-log results/events.csv &
h2 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h2- --linger 0 \
      --csv results/baseline_bots_h2.csv --label baseline &
h3 python3 -m app.netclient --server 10.0.0.1:5005 --bots 2 --prefix h3- --linger 0 \
      --csv results/baseline_bots_h3.csv --label baseline &
```

The pygame client can join the same server from a laptop that can reach the Mininet host's address.
`--csv FILE --label TEXT` writes one row per bot (RTT, jitter, loss, snapshot rate, result); use one file per host.
See [`docs/HANDOVER_ROLES_2_3.md`](docs/HANDOVER_ROLES_2_3.md) for packet sizes, how to classify the traffic,
the CSV columns and how to write your own bot.

**No Mininet at hand? (Linux only, as root.)** `tools/netns_lab.py` builds a small virtual network out of Linux
network namespaces (the server and two bot hosts on separate hosts, a 10 Mbit/s bottleneck between them, optional
iperf3 congestion and a priority queue for UDP 5005), plays one race through it and prints RTT, jitter, loss and
snapshot rate:

```bash
sudo python3 tools/netns_lab.py --case baseline  --players 8
sudo python3 tools/netns_lab.py --case congested --players 8 --iperf 14
sudo python3 tools/netns_lab.py --case priority  --players 8 --iperf 14 --priority
```

The numbers and what they mean are in section 6 of the hand-over document. It removes everything it created, also
when it is interrupted.

---

## 5. How it works

```
 pygame client (app/client.py, gfx.py, hud.py)          UDP port 5005            GameServer (app/server.py)
   draws, reads keys                                                               Game (app/game.py), 60 steps/s:
        |                                                                          physics, walls, obstacles, laps,
   NetClient (app/netclient.py, no pygame)  --- INPUT 30/s (controls) --------->   checkpoints, health, results
   join, retries, PING/RTT, loss/jitter     <-- SNAPSHOT 20/s (everyone) -------   authoritative: only it decides
   interpolation of the other cars          --- CMD, PING, LEAVE ------------->
                                            <-- JOIN_ACK, ROSTER, PONG --------    track = app/track.py (same code
 bots (netclient.run_bots, autopilot.py)                                            on both sides, built from a seed)
```

* **Authoritative server.** Clients send only throttle and steering. The server simulates 60 steps a second
  and sends every client a SNAPSHOT of all players 20 times a second: position, heading, speed, **health**,
  lap, checkpoint, place and **progress**, from which the client shows the **distance to the goal**.
* **Header and sequence numbers.** Every datagram starts with a 16-byte header (magic, version, type,
  sequence number, ack, session token, timestamp). Each receiver keeps a sliding window over the sequence
  numbers and classifies every packet as new, reordered, duplicate or too old, which gives the loss,
  reordering and duplicate counts and the jitter of both directions (`app/protocol.py::SeqTracker`).
* **Loss and out-of-order handling.** Old data is simply replaced by newer data: a late or duplicate SNAPSHOT
  or INPUT is ignored. What must not be lost is repeated until it is confirmed: JOIN every 0.5 s, commands
  every 0.25 s, **game events in every SNAPSHOT until the client acknowledges them** (so a hit, a lap or a
  wreck is never missed, and never shown twice). The client draws the other cars about 100 ms in the past,
  interpolating between two snapshots, so a lost snapshot is invisible; its own car is drawn without delay.
* **Timeouts.** A client that is silent for 5 s is removed and the others are told; a client that hears
  nothing from the server for 5 s says so and returns to the first screen.
* **Same track everywhere.** The server sends only a track number and a seed; both sides build the same
  circuit and compare a hash (a version mismatch is reported instead of silently desynchronising).

The exact byte layout of every message is in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## 6. Tests

```powershell
py -m unittest discover -s tests -t .
```

About 25 seconds, 247 tests (one line, "cannot start server: laps must be 1..50", is printed on purpose by a test of a bad option). Without pygame the window tests are skipped.

| File | What it proves |
|---|---|
| `test_protocol.py` | encoding, decoding, sequence tracker, bad input |
| `test_track.py`, `test_game.py` | track geometry, rules, obstacles, laps, checkpoints, results |
| `test_server.py` | handshake, resends, events, timeouts, host change, statistics, CSV logs |
| `test_netclient*.py` | the client side, alone and against the real server on in-memory sockets (fake clock) |
| `test_gfx_hud.py`, `test_client_app.py` | drawing lines up with the physics, HUD, screens, keys, a whole race through the window code |
| `test_loopback.py` | **real UDP** through a relay that drops 10 %, duplicates 5 % and reorders 15 % of the datagrams: four clients race, no event is lost, the loss / reorder / duplicate statistics show up in the client and in the server CSV |
| `test_local.py` | the `--local` mode |
| `test_console.py` | a player name the console cannot show (Windows code page) never crashes the output |
| `test_netns_lab.py` | the parts of the Linux network lab that need no root: which files belong to a case, the summary and its table |

## 7. Files

```
app/protocol.py     wire format, sequence tracker            app/client.py   pygame application (screens, input)
app/track.py        circuit, obstacles, geometry             app/gfx.py      drawing the world
app/game.py         rules and physics (no sockets)           app/hud.py      standings, panels, overlays
app/server.py       UDP server, CSV logs                     app/local.py    private server + opponents (--local)
app/netclient.py    client protocol, bots (no pygame)        app/autopilot.py  AI driver
app/netstats.py     jitter, rates, percentiles               docs/PROTOCOL.md  message layouts
app/console.py      output that never raises on odd names
tools/sim_race.py   race without network   tools/track_report.py   track numbers (and a picture)
tools/netns_lab.py  network lab on Linux (namespaces + tc), no Mininet needed
tests/              the tests above                          HANDOFF.md, PROJECT_STATUS.md   working notes
```

## 8. If something does not work

| Symptom | Fix |
|---|---|
| `py` is not recognised | use `python`, or reinstall Python with "Add to PATH" |
| `pygame is not installed` | `py -m pip install -r requirements.txt` |
| `pip` fails with "Failed building wheel for pygame" or "No matching distribution" | your Python is newer than the official pygame supports. Use `-r requirements.txt` as shown above (it installs `pygame-ce` on 3.14+); or run `py -m pip install pygame-ce`, or install Python 3.13 |
| Letters of a player's name show as empty boxes in the window | the window's font has no glyphs for that script (for example Tamil or Chinese). Nothing is wrong with the game; type names with Latin letters |
| "No answer from the server" | is the server running? same address and port? firewall rule above; on a hotspot all devices must be on the same hotspot |
| "The server said no: race in progress" | wait for the race to end; joining is possible only in the lobby |
| "address already in use" | another server is running on port 5005; close it or use `--port 5006` on both sides |
| The track "does not match" | the two computers have different versions of the code; copy the same folder to both |
| The window is slow | lower the window size: `py -m app.client --size 900x560` (the FPS is shown at the bottom right) |
| The window does not fit the screen | it fits itself to the screen at start; if it still does not, use `--size 900x560` or press F11 for full screen |

## 9. Known limits

One circuit, ghost collisions (no car-to-car contact), no sound, one race at a time per server, IPv4 only.
Player names are limited to 16 bytes of UTF-8 and may use any script: the server console prints characters
it cannot show as `?` (it never stops because of a name), and the window draws only the letters its font has.
The autopilot is a reference driver, not a strong one: it takes about 30 obstacle hits and is wrecked about
four times in a three-lap race, so the obstacle course is demanding (a careful human who brakes does better).
