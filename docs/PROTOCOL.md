# Project 17 - UDP game protocol (version 1)

This is the contract shared by the game server, the pygame client, the bots (Role 3) and the SDN
controller (Role 2). The code that implements it is `app/protocol.py`; if the two ever disagree,
the code wins and this file must be fixed.

## 1. Transport

- UDP only. One datagram carries exactly one message. Maximum datagram size 1200 bytes (never fragmented).
- The server listens on **UDP port 5005** by default (`protocol.DEFAULT_PORT`). Clients use any ephemeral port.
- All integers are big-endian (network byte order). Floats are IEEE 754 single precision.
- Anything that fails to decode (short, wrong magic, unknown type, bad counts) is dropped silently.

## 2. Header (16 bytes, on every datagram)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | magic | `0x1717`. Anything else is stray traffic. |
| 2 | 1 | version | `1`. A JOIN with another version gets `JOIN_REJECT(REJ_VERSION)`. |
| 3 | 1 | type | Message type, section 3. |
| 4 | 2 | seq | Sender's packet counter. One counter per direction and peer, +1 for **every** datagram sent (any type), wraps at 65536. |
| 6 | 2 | ack | Highest `seq` the sender has received from the peer so far. |
| 8 | 4 | session | Token chosen by the server. 0 in `JOIN`, delivered in the header of `JOIN_ACK`, then on every packet. |
| 12 | 4 | ts | Sender's clock in milliseconds (uint32, wraps). Only differences are meaningful. |

Example INPUT datagram (21 bytes): `17 17 01 03 00 07 00 03 a1 b2 c3 d4 00 00 30 39 7f c0 00 00 09`
= magic, version 1, INPUT, seq 7, ack 3, session 0xA1B2C3D4, ts 12345, throttle +127 (1.0),
steer -64 (-0.504; 8-bit quantisation), buttons 0, ack_event 9.

## 3. Messages

| Code | Name | From -> to | Payload | When |
|---|---|---|---|---|
| 1 | JOIN | client -> server | 21 B | Every 0.5 s until answered (max 5 s) |
| 2 | JOIN_ACK | server -> client | 19 B | Reply to an accepted JOIN (re-sent for a repeated JOIN) |
| 3 | INPUT | client -> server | 5 B | 30 per second |
| 4 | CMD | client -> server | 2 B | On user action, re-sent until visible |
| 5 | PING | client -> server | 2 B | Once per second |
| 6 | PONG | server -> client | 2 B | Reply to every PING |
| 7 | LEAVE | client -> server | 0 B | When quitting (sent 3 times) |
| 8 | JOIN_REJECT | server -> client | 5 B | Reply to a refused JOIN |
| 9 | SNAPSHOT | server -> client | 15 + 22 n + 10 m B | 20 per second to every client |
| 10 | ROSTER_REQ | client -> server | 0 B | When the roster version in a SNAPSHOT is unknown (max 2 per second) |
| 11 | ROSTER | server -> client | 2 + 20 n B | Reply to ROSTER_REQ |
| 12 | DISCONNECT | server -> client | 1 B | Client removed |

### Payload layouts

**JOIN** `!I16sB` - nonce (random uint32 chosen by the client), name (UTF-8, zero padded to 16 bytes), shape.

**JOIN_ACK** `!IBBBBBBBII` - nonce (echo), player_id, slot, tick_rate, snapshot_rate, max_players, laps,
track_id, track_seed, track_hash. The track itself is never sent: the client rebuilds it from
`track_id` + `track_seed` and checks that its own hash equals `track_hash`.

**JOIN_REJECT** `!IB` - nonce (echo), reason: 1 room full, 2 race in progress, 3 version mismatch, 4 bad request.

**INPUT** `!bbBH` - throttle (int8, +/-127 = +/-1.0), steer (int8, +127 = full right), buttons (reserved, 0), ack_event (uint16).

**CMD** `!BB` - command, argument. 1 = READY (arg 0/1), 2 = START (host only), 3 = SHAPE (arg 0..2, lobby only).
Shapes: 0 circle, 1 square, 2 triangle. Every command is idempotent.

**PING / PONG** `!H` - id chosen by the client; the server echoes it. The client measures RTT with its own clock.

**DISCONNECT** `!B` - reason: 1 timed out, 2 unknown session, 3 server shut down, 4 removed. The same codes are
used in the `a` field of a PLAYER_LEAVE event, plus 5 = the player quit (LEAVE) or rejoined from the same address.

**ROSTER** header `!BB` - roster version, entry count; then per entry `!BBBB16s` - player_id, slot, shape,
flags (bit 0 ready, bit 1 host), name.

**SNAPSHOT** header `!IBBBHIBB` - tick (uint32), state, flags, roster version, countdown_ms, race_time_ms,
player count n, event count m. Then n player blocks `!BffHhBBBBBf` (22 B each): id, x, y, heading, speed,
health, lap, checkpoint, status, place, progress. Then m events `!HBBhi` (10 B each): event id, type, player, a, b.

- state: 0 LOBBY, 1 COUNTDOWN, 2 RACING, 3 FINISHED. `countdown_ms` counts down to GO (COUNTDOWN) or to the return to the lobby (FINISHED).
- flags: bit 0 = you are the host (you may send CMD_START).
- x, y are world units (10 units = 1 m). heading is a uint16 angle (65536 = 2 pi). speed is units/s x 10 (int16).
- lap = laps completed, checkpoint = checkpoints passed in the current lap, place = finishing place (0 while racing).
- progress = unwrapped distance along the track in units; distance to the goal = laps x track length - progress.
- status bits: 1 wrecked, 2 finished, 4 invulnerable (after a hit), 8 has crossed the start line, 16 did not finish.

### Events

| Type | Name | player | a | b |
|---|---|---|---|---|
| 1 | PLAYER_JOIN | who | - | - |
| 2 | PLAYER_LEAVE | who | reason | - |
| 3 | RACE_START | 0 | - | - |
| 4 | RACE_END | 0 | - | - |
| 5 | HIT | who | damage | obstacle id |
| 6 | CHECKPOINT | who | checkpoint number | race time ms |
| 7 | LAP | who | lap completed | lap time ms |
| 8 | FINISH | who | place | total race time ms |
| 9 | WRECK | who | - | - |
| 10 | RESPAWN | who | - | - |

## 4. Join handshake

1. The client picks a random nonce and sends `JOIN` (session 0) every 0.5 s.
2. The server answers `JOIN_ACK` (token in the header's session field) or `JOIN_REJECT`. A repeated JOIN with the same
   nonce from the same address gets the same JOIN_ACK again, so a lost reply never creates a second player.
   A JOIN with a different nonce from an address that is already joined replaces the old entry (client restarted).
3. After JOIN_ACK the client sends ROSTER_REQ (it does not know the roster version yet) and starts sending PING/INPUT.
4. Joining is only possible in the lobby, while the room is not full. The first player to join is the host; if the host
   leaves, the longest-present player becomes host.
5. The server ignores every non-JOIN datagram whose session token does not match the address it was issued to, and answers
   it with `DISCONNECT(unknown session)` (at most one per second per address).

## 5. Reliability, loss and ordering

UDP gives none of these, so the protocol adds the minimum needed:

| Message | If it is lost | If it is duplicated or late |
|---|---|---|
| INPUT | The next one (33 ms later) replaces it | Duplicates and late copies are ignored; only the newest control state is applied |
| SNAPSHOT | The next one (50 ms later) replaces it; the client interpolates | Duplicates and late copies are ignored |
| Events | Repeated in every SNAPSHOT until acknowledged (below) | The client skips ids it already processed |
| JOIN / JOIN_ACK | JOIN is re-sent every 0.5 s | Same nonce gives the same JOIN_ACK |
| CMD | Re-sent every 0.25 s (max 20) until the roster or state shows the effect | Idempotent |
| PING / PONG | The next probe a second later | Matched by id; unknown ids are ignored |

- Every receiver runs a `SeqTracker` per peer. It classifies each packet as NEW, REORDERED (late, never seen),
  DUPLICATE or TOO_OLD (more than 64 packets behind) and keeps counts, so loss = expected - received.
- Event delivery: each event gets a uint16 id from the server. Every SNAPSHOT to a client carries up to 16 events
  that client has not acknowledged yet, oldest first. The client processes ids newer than the last one it processed,
  and reports the newest in `ack_event` of every INPUT. The server stops sending events up to that id. A client that joins
  midway starts from the newest event id, so it never replays history.
- Jitter (both ends) is the RFC 3550 interarrival estimate computed from the `ts` field and the arrival time.

## 6. Timeouts

- A joined client sends something (INPUT or PING) at least every 0.5 s. The server removes a client silent for 5 s,
  sends it `DISCONNECT(timed out)` and tells the others with a PLAYER_LEAVE event.
- A client that gets no SNAPSHOT or PONG for 5 s reports the server as lost.
- A client that quits sends LEAVE three times; the server removes it on the first one.

## 7. Sizes and bandwidth

Header 16 B, INPUT datagram 21 B. SNAPSHOT datagram sizes (no events / with 16 events):

| Players | No events | 16 events |
|---|---|---|
| 4 | 119 B | 279 B |
| 8 | 207 B | 367 B |
| 12 | 295 B | 455 B |
| 16 | 383 B | 543 B |

At 12 players one client receives about 47 kbit/s (20 snapshots/s, payload only) and sends about 5 kbit/s of INPUT;
the server sends about 0.57 Mbit/s in total. Add 28 bytes of IP + UDP header per datagram for link-level numbers.

## 8. Traffic classification (for the SDN controller)

- Client -> server game traffic: UDP, destination port 5005. Server -> client: UDP, source port 5005.
- The first two payload bytes of every game datagram are `17 17`, and the byte at payload offset 3 is the message type,
  so a controller can tell state traffic (INPUT 3, SNAPSHOT 9) from control traffic (JOIN 1, CMD 4, PING 5, ...).
- State traffic is small, periodic and latency-sensitive: 30 packets/s up and 20 packets/s down per player.
