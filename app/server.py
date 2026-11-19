"""server.py - authoritative UDP game server for the Project 17 racing game.

    python -m app.server                                  # 0.0.0.0:5005, 3 laps, up to 16 players
    python -m app.server --laps 2 --max-players 8
    python -m app.server --auto-start 4 --exit-after-races 1 --stats-csv results/server.csv

One thread and one non-blocking UDP socket.  ``select`` waits for datagrams until
the next fixed simulation step (``tick_rate``, default 60 Hz).  Every step runs
``Game.step()``; every ``tick_rate / snapshot_rate`` steps (default 20 Hz) a
SNAPSHOT goes to each player.  Clients send only controls, the server owns all
state.  The wire format is in protocol.py / docs/PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import csv
import os
import secrets
import select
import socket
import struct
import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

try:                                    # normal use: python -m app.server
    from . import protocol as P
    from .console import tolerant_output
    from .game import Game, GameConfig
    from .netstats import Jitter, RateMeter
except ImportError:                     # script use: python app/server.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import protocol as P
    from app.console import tolerant_output
    from app.game import Game, GameConfig
    from app.netstats import Jitter, RateMeter

MAX_CATCHUP_TICKS = 5            # simulation steps per loop pass when behind schedule
RECV_BATCH = 512                 # datagrams read per loop pass at most
UNKNOWN_REPLY_INTERVAL_S = 1.0   # DISCONNECT(unknown session) rate limit per address
HOUSEKEEPING_HZ = 10             # timeouts, auto-start, stats

STATS_COLUMNS = ["label", "t_s", "race_t_s", "state", "player_id", "name", "rx_pps", "tx_pps",
                 "rx_kbps", "tx_kbps", "rx_expected", "rx_lost", "rx_loss_pct", "rx_reordered",
                 "rx_duplicates", "jitter_ms"]
RESULT_COLUMNS = ["label", "race", "place", "player_id", "name", "shape", "finished", "finish_s",
                  "laps_done", "progress_m", "health", "hits", "wrecks", "best_lap_s", "dnf"]
EVENT_COLUMNS = ["label", "t_s", "race_t_s", "event_id", "event", "player_id", "a", "b"]

Addr = Tuple[str, int]


class CsvLog:
    """Append-only CSV file; every row is flushed so a crash loses nothing."""

    def __init__(self, path: str, columns: List[str]) -> None:
        self.path, self.columns = path, columns
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        if fresh:
            self._w.writerow(columns)
            self._f.flush()

    def write(self, row: Dict[str, object]) -> None:
        self._w.writerow([row.get(c, "") for c in self.columns])
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


class Client:
    """Server-side record of one joined player."""

    def __init__(self, pid: int, slot: int, session: int, addr: Addr, nonce: int, now: float) -> None:
        self.pid, self.slot, self.session, self.addr, self.nonce = pid, slot, session, addr, nonce
        self.joined_at = self.last_heard = now
        self.seq_out = 0                       # next outgoing seq (+1 per datagram of any type)
        self.tracker = P.SeqTracker()          # incoming packets
        self.jitter = Jitter()                 # uplink jitter from the header ts
        self.acked_event = 0                   # newest event id the client confirmed
        self.join_ack = b""                    # cached JOIN_ACK payload (re-sent for a repeated JOIN)
        self.rx, self.tx = RateMeter(now), RateMeter(now)
        self.rx_total = self.tx_total = 0


class GameServer:
    def __init__(self, game_cfg: Optional[GameConfig] = None, host: str = "0.0.0.0",
                 port: int = P.DEFAULT_PORT, *, sock=None, clock: Optional[Callable[[], float]] = None,
                 require_ready: bool = False, auto_start_players: int = 0,
                 auto_start_delay: float = 3.0, exit_after_races: int = 0,
                 stats_csv: Optional[str] = None, results_csv: Optional[str] = None,
                 event_log: Optional[str] = None, stats_interval: float = 1.0,
                 label: str = "", verbose: bool = True, sdn=None) -> None:
        self.cfg = game_cfg or GameConfig()
        self.sdn = sdn                          # optional app.sdn_client.SdnClient (Role 3): tells the SDN controller who plays
        if not 0 <= self.cfg.track_seed <= 0xFFFFFFFF:
            raise ValueError("track_seed must fit in 32 bits")
        self.game = Game(self.cfg)
        self.clock = clock or time.monotonic
        self.t0 = self.clock()
        self.require_ready, self.auto_start_players = require_ready, auto_start_players
        self.auto_start_delay, self.exit_after_races = auto_start_delay, exit_after_races
        self.stats_interval, self.label, self.verbose = stats_interval, label, verbose

        self.clients: Dict[int, Client] = {}
        self.by_addr: Dict[Addr, Client] = {}
        self.host_id: Optional[int] = None
        self.roster_ver = 1
        self.event_log: Deque[P.Event] = deque(maxlen=P.EVENT_LOG_SIZE)
        self.last_event_id = 0                 # ids run 1..65535, 0 means "none"
        self.counters = {"rx_datagrams": 0, "rx_bad": 0, "tx_datagrams": 0, "send_errors": 0,
                         "unknown_session": 0, "overruns": 0}
        self._next_pid = 1
        self._anon_seq = 0
        self._unknown_at: Dict[Addr, float] = {}
        self._snap_acc = 0
        self._last_state = self.game.state
        self._last_stats = self.t0
        self._last_housekeeping = self.t0
        self._auto_since: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._stats_log = CsvLog(stats_csv, STATS_COLUMNS) if stats_csv else None
        self._results_log = CsvLog(results_csv, RESULT_COLUMNS) if results_csv else None
        self._event_csv = CsvLog(event_log, EVENT_COLUMNS) if event_log else None
        self.sock = sock if sock is not None else self._open_socket(host, port)

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _open_socket(host: str, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        s.bind((host, port))
        s.setblocking(False)
        return s

    @property
    def address(self) -> Addr:
        return self.sock.getsockname()

    def _log(self, msg: str) -> None:
        if self.verbose:
            line = "[%7.2f] %s" % (self.clock() - self.t0, msg)
            try:
                print(line, flush=True)
            except UnicodeEncodeError:      # a player's name the console's code page cannot show: the log must never stop the game
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                print(line.encode(enc, "replace").decode(enc), flush=True)

    def _ms(self) -> int:
        return P.ms32(self.clock() - self.t0)

    def _sendto(self, data: bytes, addr: Addr) -> bool:
        try:
            self.sock.sendto(data, addr)
        except OSError:                        # unreachable peer, full buffer ...
            self.counters["send_errors"] += 1
            return False
        self.counters["tx_datagrams"] += 1
        return True

    def _send(self, c: Client, mtype: int, payload: bytes = b"") -> None:
        seq = c.seq_out
        c.seq_out = P.seq_next(seq)
        ack = c.tracker.highest if c.tracker.started else 0
        data = P.encode(mtype, seq, ack, c.session, self._ms(), payload)
        if self._sendto(data, c.addr):
            c.tx.add(len(data))
            c.tx_total += 1

    def _send_anon(self, addr: Addr, mtype: int, payload: bytes = b"") -> None:
        """Reply to an address that has no session (JOIN_REJECT, unknown-session DISCONNECT)."""
        seq = self._anon_seq
        self._anon_seq = P.seq_next(seq)
        self._sendto(P.encode(mtype, seq, 0, 0, self._ms(), payload), addr)

    # ------------------------------------------------------------------ incoming
    def handle_datagram(self, data: bytes, addr: Addr) -> None:
        now = self.clock()
        self.counters["rx_datagrams"] += 1
        try:
            header, payload = P.decode(data)
        except P.ProtocolError:
            self.counters["rx_bad"] += 1
            return
        try:
            if header.msg_type == P.MSG_JOIN:
                self._on_join(header, payload, addr, now)
            else:
                self._on_client_packet(header, payload, addr, len(data), now)
        except P.ProtocolError:
            self.counters["rx_bad"] += 1
        self._check_state(now)

    def _on_client_packet(self, h: P.Header, payload: bytes, addr: Addr, size: int, now: float) -> None:
        c = self.by_addr.get(addr)
        if c is None or h.session != c.session:
            self._unknown_session(addr, now)
            return
        c.last_heard = now
        c.rx.add(size)
        c.rx_total += 1
        kind = c.tracker.update(h.seq)
        if kind in (P.SeqTracker.DUPLICATE, P.SeqTracker.TOO_OLD):
            return
        if kind == P.SeqTracker.NEW:
            c.jitter.update(h.ts, now)
        t = h.msg_type
        if t == P.MSG_INPUT:
            if kind == P.SeqTracker.NEW:           # a late INPUT would undo newer controls
                self._on_input(c, payload)
        elif t == P.MSG_CMD:
            self._on_cmd(c, payload)
        elif t == P.MSG_PING:
            self._send(c, P.MSG_PONG, P.pack_ping(P.unpack_ping(payload)))
        elif t == P.MSG_ROSTER_REQ:
            self._send_roster(c)
        elif t == P.MSG_LEAVE:
            self._remove(c, P.DISC_LEFT, notify=False)
        # server-to-client message types sent by a client are ignored

    def _unknown_session(self, addr: Addr, now: float) -> None:
        self.counters["unknown_session"] += 1
        last = self._unknown_at.get(addr)
        if last is not None and now - last < UNKNOWN_REPLY_INTERVAL_S:
            return
        if len(self._unknown_at) > 256:
            self._unknown_at = {a: t for a, t in self._unknown_at.items() if now - t < 10.0}
        self._unknown_at[addr] = now
        self._send_anon(addr, P.MSG_DISCONNECT, P.pack_reason(P.DISC_UNKNOWN_SESSION))

    def _on_join(self, h: P.Header, payload: bytes, addr: Addr, now: float) -> None:
        nonce = struct.unpack_from("!I", payload)[0] if len(payload) >= 4 else 0
        if h.version != P.PROTOCOL_VERSION:
            self._send_anon(addr, P.MSG_JOIN_REJECT, P.pack_join_reject(nonce, P.REJ_VERSION))
            return
        try:
            nonce, name, shape = P.unpack_join(payload)
        except P.ProtocolError:
            self._send_anon(addr, P.MSG_JOIN_REJECT, P.pack_join_reject(nonce, P.REJ_BAD_REQUEST))
            return
        c = self.by_addr.get(addr)
        if c is not None:
            c.last_heard = now
            if c.nonce == nonce:                    # our JOIN_ACK was lost: say it again
                self._send(c, P.MSG_JOIN_ACK, c.join_ack)
                return
            self._remove(c, P.DISC_LEFT, notify=False)      # client restarted on the same port
        ok, reason = self.game.can_join()
        if not ok:
            self._send_anon(addr, P.MSG_JOIN_REJECT, P.pack_join_reject(nonce, reason))
            return
        pid, slot, session = self._alloc_pid(), self._alloc_slot(), self._alloc_session()
        self.game.add_player(pid, name, shape, slot)
        c = Client(pid, slot, session, addr, nonce, now)
        self.clients[pid], self.by_addr[addr] = c, c
        if self.host_id is None:
            self.host_id = pid
        self._roster_changed()
        self._pump_events(now)
        c.acked_event = self.last_event_id          # no replay of events from before the join
        track = self.game.track
        c.join_ack = P.pack_join_ack(P.JoinAck(
            nonce, pid, slot, self.cfg.tick_rate, self.cfg.snapshot_rate, self.cfg.max_players,
            self.cfg.laps, self.cfg.track_id, self.cfg.track_seed, track.hash))
        self._send(c, P.MSG_JOIN_ACK, c.join_ack)
        self._sdn_call("register", c.addr[0], c.addr[1])
        self._log("%s joined from %s:%d as player %d (%s)%s" % (
            name, addr[0], addr[1], pid, P.SHAPE_NAMES[self.game.players[pid].shape],
            " [host]" if pid == self.host_id else ""))

    def _alloc_pid(self) -> int:
        for _ in range(255):
            pid = self._next_pid
            self._next_pid = pid % 255 + 1
            if pid not in self.clients:
                return pid
        raise RuntimeError("no free player id")

    def _alloc_slot(self) -> int:
        used = {c.slot for c in self.clients.values()}
        return next(i for i in range(256) if i not in used)

    def _alloc_session(self) -> int:
        used = {c.session for c in self.clients.values()}
        while True:
            token = secrets.randbelow(0xFFFFFFFF) + 1
            if token not in used:
                return token

    def _on_input(self, c: Client, payload: bytes) -> None:
        throttle, steer, _buttons, ack_event = P.unpack_input(payload)
        self.game.set_input(c.pid, throttle, steer)
        if P.seq_gt(ack_event, c.acked_event) and not P.seq_gt(ack_event, self.last_event_id):
            c.acked_event = ack_event

    def _on_cmd(self, c: Client, payload: bytes) -> None:
        cmd, arg = P.unpack_cmd(payload)
        g = self.game
        p = g.players.get(c.pid)
        if p is None or g.state != P.ST_LOBBY:
            return
        if cmd == P.CMD_READY:
            if p.ready != bool(arg):
                g.set_ready(c.pid, bool(arg))
                self._roster_changed()
        elif cmd == P.CMD_SHAPE:
            if arg <= P.SHAPE_TRIANGLE and p.shape != arg:
                g.set_shape(c.pid, arg)
                self._roster_changed()
        elif cmd == P.CMD_START and c.pid == self.host_id:
            if self.require_ready and not all(q.ready for q in g.players.values() if q.id != c.pid):
                return
            self._begin_race()

    def _begin_race(self) -> None:
        if self.game.begin_countdown():
            self._roster_changed()               # begin_countdown clears the ready flags
            self._log("countdown started with %d player(s)" % len(self.game.players))

    def _remove(self, c: Client, reason: int, notify: bool = True) -> None:
        if c.pid not in self.clients:
            return
        if notify:
            self._send(c, P.MSG_DISCONNECT, P.pack_reason(reason))
        del self.clients[c.pid]
        self.by_addr.pop(c.addr, None)
        self._sdn_call("unregister", c.addr[0])
        player = self.game.players.get(c.pid)
        name = player.name if player else "?"
        self.game.remove_player(c.pid, reason)
        if self.host_id == c.pid:
            self.host_id = next(iter(self.clients), None)     # longest-present player
        self._roster_changed()
        self._pump_events(self.clock())
        t = c.tracker
        self._log("%s (player %d) left: %s; rx %d pkts, lost %d (%.2f%%), reordered %d, dup %d, "
                  "jitter %.2f ms" % (name, c.pid, P.DISC_TEXT.get(reason, str(reason)), t.received,
                                      t.lost, 100.0 * t.loss_ratio, t.reordered, t.duplicates,
                                      c.jitter.value))

    def _sdn_call(self, action: str, *args) -> None:
        """Tell the SDN controller about a player (join / leave).  Never raises: the game must not depend on it."""
        if self.sdn is None:
            return
        try:
            getattr(self.sdn, action)(*args)
        except Exception as exc:
            self._log("SDN %s failed: %s" % (action, exc))

    # ------------------------------------------------------------------ roster
    def _roster_changed(self) -> None:
        self.roster_ver = (self.roster_ver + 1) & 0xFF

    def _send_roster(self, c: Client) -> None:
        entries = []
        for cl in sorted(self.clients.values(), key=lambda x: x.slot):
            p = self.game.players[cl.pid]
            entries.append(P.RosterEntry(cl.pid, cl.slot, p.shape, p.ready, cl.pid == self.host_id, p.name))
        self._send(c, P.MSG_ROSTER, P.pack_roster(self.roster_ver, entries))

    # ------------------------------------------------------------------ events & snapshots
    def _pump_events(self, now: float) -> None:
        g = self.game
        for etype, pid, a, b in g.drain_events():
            self.last_event_id = self.last_event_id % 65535 + 1
            ev = P.Event(self.last_event_id, etype, pid, a, b)
            self.event_log.append(ev)
            if self._event_csv is not None:
                self._event_csv.write({"label": self.label, "t_s": "%.3f" % (now - self.t0),
                                       "race_t_s": "%.3f" % g.race_time, "event_id": ev.id,
                                       "event": P.EVENT_NAMES.get(etype, etype), "player_id": pid,
                                       "a": a, "b": b})

    def _pending_events(self, c: Client) -> List[P.Event]:
        out: List[P.Event] = []
        for ev in reversed(self.event_log):
            if not P.seq_gt(ev.id, c.acked_event):
                break
            out.append(ev)
        out.reverse()                                   # oldest first
        return out[:P.EVENTS_PER_SNAPSHOT]

    def _send_snapshots(self) -> None:
        if not self.clients:
            return
        g = self.game
        states = g.player_states()
        blob = P.pack_players(states)                   # encoded once, shared by all recipients
        for c in list(self.clients.values()):
            flags = P.SNAPF_HOST if c.pid == self.host_id else 0
            payload = P.pack_snapshot_parts(g.tick, g.state, flags, self.roster_ver, g.countdown_ms(),
                                            g.race_ms, len(states), blob, self._pending_events(c))
            self._send(c, P.MSG_SNAPSHOT, payload)

    # ------------------------------------------------------------------ simulation
    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        g = self.game
        g.step()
        self._pump_events(now)
        self._snap_acc += self.cfg.snapshot_rate
        if self._snap_acc >= self.cfg.tick_rate:
            self._snap_acc -= self.cfg.tick_rate
            self._send_snapshots()
        if now - self._last_housekeeping >= 1.0 / HOUSEKEEPING_HZ:
            self._last_housekeeping = now
            self._housekeeping(now)
        self._check_state(now)

    def _housekeeping(self, now: float) -> None:
        for c in list(self.clients.values()):
            if now - c.last_heard > P.CLIENT_TIMEOUT_S:
                self._remove(c, P.DISC_TIMEOUT, notify=True)
        g = self.game
        if (self.auto_start_players and g.state == P.ST_LOBBY
                and len(self.clients) >= self.auto_start_players):
            if self._auto_since is None:
                self._auto_since = now
            elif now - self._auto_since >= self.auto_start_delay:
                self._auto_since = None
                self._begin_race()
        else:
            self._auto_since = None
        if now - self._last_stats >= self.stats_interval:
            self._write_stats(now)
        if (self.exit_after_races and g.races_completed >= self.exit_after_races
                and ((g.state == P.ST_FINISHED and self._finished_at is not None
                      and now - self._finished_at >= 1.0)  # let the last snapshots out first
                     or g.state == P.ST_LOBBY)):           # everybody left before the results time was over
            self._log("%d race(s) completed, stopping" % g.races_completed)
            self._stop.set()

    def _check_state(self, now: float) -> None:
        s = self.game.state
        if s == self._last_state:
            return
        old, self._last_state = self._last_state, s
        self._log("state %s -> %s" % (P.STATE_NAMES[old], P.STATE_NAMES[s]))
        if s == P.ST_FINISHED:
            self._race_finished(now)

    def _race_finished(self, now: float) -> None:
        g = self.game
        self._finished_at = now
        self._log("race %d results:" % g.races_completed)
        for r in g.last_results:
            place = str(r["place"]) if r["finished"] else "DNF"
            self._log("  %3s %-14s %-8s health %3d  hits %2d  wrecks %d  %s" % (
                place, r["name"], r["shape"], r["health"], r["hits"], r["wrecks"],
                "%.1f s" % (r["finish_ms"] / 1000.0) if r["finished"] else "%.0f m" % r["progress_m"]))
            if self._results_log is not None:
                self._results_log.write({
                    "label": self.label, "race": g.races_completed, "place": r["place"],
                    "player_id": r["player_id"], "name": r["name"], "shape": r["shape"],
                    "finished": int(r["finished"]), "finish_s": "%.3f" % (r["finish_ms"] / 1000.0),
                    "laps_done": r["laps_done"], "progress_m": r["progress_m"], "health": r["health"],
                    "hits": r["hits"], "wrecks": r["wrecks"],
                    "best_lap_s": "%.3f" % (r["best_lap_ms"] / 1000.0), "dnf": int(r["dnf"])})

    def _write_stats(self, now: float) -> None:
        g = self.game
        for c in list(self.clients.values()):
            rx_pps, rx_kbps = c.rx.take(now)
            tx_pps, tx_kbps = c.tx.take(now)
            if self._stats_log is None:
                continue
            t, p = c.tracker, g.players.get(c.pid)
            self._stats_log.write({
                "label": self.label, "t_s": "%.2f" % (now - self.t0),
                "race_t_s": "%.2f" % g.race_time if g.state == P.ST_RACING else "0",
                "state": P.STATE_NAMES[g.state], "player_id": c.pid, "name": p.name if p else "",
                "rx_pps": "%.1f" % rx_pps, "tx_pps": "%.1f" % tx_pps, "rx_kbps": "%.2f" % rx_kbps,
                "tx_kbps": "%.2f" % tx_kbps, "rx_expected": t.expected, "rx_lost": t.lost,
                "rx_loss_pct": "%.3f" % (100.0 * t.loss_ratio), "rx_reordered": t.reordered,
                "rx_duplicates": t.duplicates, "jitter_ms": "%.3f" % c.jitter.value})
        self._last_stats = now

    # ------------------------------------------------------------------ main loop
    def _drain(self) -> None:
        for _ in range(RECV_BATCH):
            try:
                data, addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                return
            except OSError:                    # Windows reports an earlier ICMP error here
                continue
            self.handle_datagram(data, addr)

    def _poll(self, next_tick: float) -> float:
        now = self.clock()
        timeout = max(0.0, min(next_tick - now, 0.05))
        try:
            readable, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            self._stop.set()
            return next_tick
        if readable:
            self._drain()
        now = self.clock()
        steps, dt = 0, self.game.dt
        while now >= next_tick and steps < MAX_CATCHUP_TICKS:
            self.tick(now)
            next_tick += dt
            steps += 1
        if now >= next_tick:                   # still behind: drop the backlog instead of spiralling
            self.counters["overruns"] += 1
            next_tick = now + dt
        return next_tick

    def serve_forever(self) -> None:
        timer_raised = False
        if sys.platform == "win32":            # 1 ms timer so a 60 Hz loop is not rounded to 15.6 ms
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
                timer_raised = True
            except Exception:
                pass
        self._log("listening on %s:%d  (%d laps, up to %d players, %d Hz tick, %d Hz snapshots)" % (
            self.address[0], self.address[1], self.cfg.laps, self.cfg.max_players,
            self.cfg.tick_rate, self.cfg.snapshot_rate))
        next_tick = self.clock()
        try:
            while not self._stop.is_set():
                next_tick = self._poll(next_tick)
        finally:
            if timer_raised:
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass
            self.close()

    def start_thread(self) -> threading.Thread:
        """Run the server loop in a background thread (used by tests and demos)."""
        self._thread = threading.Thread(target=self.serve_forever, name="game-server", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.sdn is not None:               # leave the controller without stale players, then stop its worker
            try:
                self.sdn.unregister_all()
                self.sdn.close()
            except Exception as exc:
                self._log("SDN shutdown failed: %s" % exc)
        for c in list(self.clients.values()):
            for _ in range(2):                 # twice: one lost datagram must not leave a zombie client
                self._send(c, P.MSG_DISCONNECT, P.pack_reason(P.DISC_SHUTDOWN))
        c = self.counters
        self._log("stopped: %d datagrams in (%d bad, %d from unknown sessions), %d out, %d send errors, "
                  "%d loop overruns, %d race(s) completed" % (
                      c["rx_datagrams"], c["rx_bad"], c["unknown_session"], c["tx_datagrams"],
                      c["send_errors"], c["overruns"], self.game.races_completed))
        for log in (self._stats_log, self._results_log, self._event_csv):
            if log is not None:
                log.close()
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------- command line
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.server",
                                 description="UDP racing game server (Project 17).")
    ap.add_argument("--host", default="0.0.0.0", help="address to bind (default all interfaces)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--laps", type=int, default=3)
    ap.add_argument("--max-players", type=int, default=16)
    ap.add_argument("--tick-rate", type=int, default=60, help="simulation steps per second")
    ap.add_argument("--snapshot-rate", type=int, default=20, help="SNAPSHOTs per second per player")
    ap.add_argument("--seed", type=int, default=17, help="obstacle layout seed")
    ap.add_argument("--no-respawn", action="store_true", help="wrecked players are out of the race")
    ap.add_argument("--time-limit", type=float, default=180.0, help="seconds, 0 = unlimited")
    ap.add_argument("--countdown", type=float, default=3.0)
    ap.add_argument("--results-time", type=float, default=8.0, help="seconds the results stay up")
    ap.add_argument("--require-ready", action="store_true",
                    help="the host can only start when every other player is ready")
    ap.add_argument("--auto-start", type=int, default=0, metavar="N",
                    help="start the countdown by itself once N players are in the lobby")
    ap.add_argument("--auto-start-delay", type=float, default=3.0, metavar="S")
    ap.add_argument("--exit-after-races", type=int, default=0, metavar="K",
                    help="stop the server after K finished races (for scripted experiments)")
    ap.add_argument("--stats-csv", help="per-player network statistics, one row per interval")
    ap.add_argument("--stats-interval", type=float, default=1.0)
    ap.add_argument("--results-csv", help="one row per player per finished race")
    ap.add_argument("--event-log", help="every game event as CSV")
    ap.add_argument("--label", default="", help="text copied into every CSV row (e.g. baseline / sdn)")
    ap.add_argument("--sdn-url", metavar="URL",
                    help="SDN controller REST address (Mininet: http://10.0.0.254:8080): every player that joins is "
                         "registered there so the controller can prioritise its traffic")
    ap.add_argument("--sdn-log", metavar="FILE", help="CSV of every call to the SDN controller (needs --sdn-url)")
    ap.add_argument("--quiet", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    tolerant_output()
    args = build_parser().parse_args(argv)
    cfg = GameConfig(laps=args.laps, max_players=args.max_players, tick_rate=args.tick_rate,
                     snapshot_rate=args.snapshot_rate, track_seed=args.seed,
                     respawn=not args.no_respawn, countdown_s=args.countdown,
                     time_limit_s=args.time_limit, results_s=args.results_time)
    sdn = None
    if args.sdn_url:
        try:
            from .sdn_client import SdnClient, SdnError
        except ImportError:                     # script use
            from app.sdn_client import SdnClient, SdnError
        sdn = SdnClient(args.sdn_url, log_path=args.sdn_log, label=args.label)
        try:
            st = sdn.get_status()
            print("SDN controller %s: mode=%s, priority_on=%s, switches=%s" % (
                args.sdn_url, st.get("mode"), st.get("priority_on"), st.get("switches")), file=sys.stderr)
        except SdnError as exc:
            print("WARNING: SDN controller not reachable (%s); the game runs anyway, registration is retried" % exc,
                  file=sys.stderr)
    try:
        server = GameServer(cfg, args.host, args.port, require_ready=args.require_ready,
                            auto_start_players=args.auto_start, auto_start_delay=args.auto_start_delay,
                            exit_after_races=args.exit_after_races, stats_csv=args.stats_csv,
                            results_csv=args.results_csv, event_log=args.event_log,
                            stats_interval=args.stats_interval, label=args.label,
                            verbose=not args.quiet, sdn=sdn)
    except (ValueError, OSError) as exc:
        print("cannot start server: %s" % exc, file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server._stop.set()
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
