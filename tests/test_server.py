"""Server tests: no real network, a fake socket and a fake clock drive GameServer directly."""
import csv
import io
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

from app import protocol as P
from app.autopilot import Autopilot
from app.game import GameConfig
from app.server import GameServer, main


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def getsockname(self):
        return ("127.0.0.1", 5005)

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Env:
    """A server wired to a fake socket and clock."""

    def __init__(self, cfg=None, **kwargs):
        self.clock, self.sock = FakeClock(), FakeSock()
        self.server = GameServer(cfg or GameConfig(laps=1, countdown_s=1.0), sock=self.sock,
                                 clock=self.clock, verbose=False, **kwargs)

    def run(self, seconds):
        for _ in range(int(round(seconds * self.server.cfg.tick_rate))):
            self.clock.advance(self.server.game.dt)
            self.server.tick()


class Peer:
    """A scripted client that talks to the server through handle_datagram."""

    def __init__(self, env, port, name="Peer", shape=P.SHAPE_CIRCLE):
        self.env, self.name, self.shape = env, name, shape
        self.addr = ("10.0.0.%d" % (port % 250 + 1), port)
        self.nonce = 1000 + port
        self.seq, self.session, self.pid, self.ack = 0, 0, None, None

    def raw(self, mtype, payload=b"", seq=None, session=None):
        use = self.seq if seq is None else seq
        if seq is None:
            self.seq = P.seq_next(self.seq)
        data = P.encode(mtype, use, 0, self.session if session is None else session,
                        int(self.env.clock.t * 1000) & 0xFFFFFFFF, payload)
        self.env.server.handle_datagram(data, self.addr)

    def pull(self):
        mine = [P.decode(d) for d, a in self.env.sock.sent if a == self.addr]
        self.env.sock.sent = [(d, a) for d, a in self.env.sock.sent if a != self.addr]
        return mine

    def join(self):
        self.raw(P.MSG_JOIN, P.pack_join(self.nonce, self.name, self.shape), session=0)
        for h, p in self.pull():
            if h.msg_type == P.MSG_JOIN_ACK:
                self.ack, self.session = P.unpack_join_ack(p), h.session
                self.pid = self.ack.player_id
                return self.ack
            if h.msg_type == P.MSG_JOIN_REJECT:
                return P.unpack_join_reject(p)[1]
        return None

    def inp(self, throttle=0.0, steer=0.0, ack_event=0, seq=None):
        self.raw(P.MSG_INPUT, P.pack_input(throttle, steer, 0, ack_event), seq=seq)


def snapshots(msgs):
    return [P.unpack_snapshot(p) for h, p in msgs if h.msg_type == P.MSG_SNAPSHOT]


def of_type(msgs, mtype):
    return [(h, p) for h, p in msgs if h.msg_type == mtype]


class HandshakeTests(unittest.TestCase):
    def test_join_ack_carries_session_and_config(self):
        env = Env()
        a = Peer(env, 5001, "Alice", P.SHAPE_TRIANGLE)
        ack = a.join()
        self.assertIsInstance(ack, P.JoinAck)
        self.assertNotEqual(a.session, 0)
        self.assertEqual((ack.nonce, ack.tick_rate, ack.snapshot_rate, ack.laps, ack.track_hash),
                         (a.nonce, 60, 20, 1, env.server.game.track.hash))
        self.assertEqual(env.server.game.players[a.pid].shape, P.SHAPE_TRIANGLE)
        self.assertEqual(env.server.host_id, a.pid)

    def test_repeated_join_with_same_nonce_gets_the_same_ack(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.raw(P.MSG_JOIN, P.pack_join(a.nonce, "Peer", 0), session=0)
        again = of_type(a.pull(), P.MSG_JOIN_ACK)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0][0].session, a.session)
        self.assertEqual(len(env.server.game.players), 1)

    def test_new_nonce_from_same_address_replaces_the_player(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        old_session = a.session
        a.nonce += 1
        a.join()
        self.assertEqual(len(env.server.clients), 1)
        self.assertNotEqual(a.session, old_session)
        leaves = [e for e in env.server.event_log if e.type == P.EV_PLAYER_LEAVE]
        self.assertEqual([e.a for e in leaves], [P.DISC_LEFT])

    def test_version_mismatch_is_rejected(self):
        env = Env()
        data = bytearray(P.encode(P.MSG_JOIN, 0, 0, 0, 0, P.pack_join(5, "Old", 0)))
        data[2] = 9
        env.server.handle_datagram(bytes(data), ("10.0.0.9", 4000))
        (_, payload), = of_type([P.decode(d) for d, _ in env.sock.sent], P.MSG_JOIN_REJECT)
        self.assertEqual(P.unpack_join_reject(payload), (5, P.REJ_VERSION))
        self.assertFalse(env.server.clients)

    def test_bad_join_payloads_are_rejected(self):
        env = Env()
        env.server.handle_datagram(P.encode(P.MSG_JOIN, 0, 0, 0, 0, b"\x00\x00\x00\x07"), ("10.0.0.9", 1))
        env.server.handle_datagram(P.encode(P.MSG_JOIN, 0, 0, 0, 0, P.pack_join(8, "X", 7)), ("10.0.0.9", 2))
        reasons = [P.unpack_join_reject(p) for h, p in (P.decode(d) for d, _ in env.sock.sent)
                   if h.msg_type == P.MSG_JOIN_REJECT]
        self.assertEqual(reasons, [(7, P.REJ_BAD_REQUEST), (8, P.REJ_BAD_REQUEST)])

    def test_full_room_and_race_in_progress_are_refused(self):
        env = Env(GameConfig(laps=1, max_players=2, countdown_s=1.0))
        a, b, c = Peer(env, 5001), Peer(env, 5002), Peer(env, 5003)
        a.join()
        b.join()
        self.assertEqual(c.join(), P.REJ_FULL)
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        env.run(0.1)
        self.assertEqual(env.server.game.state, P.ST_COUNTDOWN)
        env.server.game.remove_player(b.pid)           # free a seat: still refused, a race is on
        env.server.clients.pop(b.pid)
        self.assertEqual(c.join(), P.REJ_IN_PROGRESS)

    def test_garbage_is_dropped_silently(self):
        env = Env()
        for junk in (b"", b"abc", b"\x00" * 40, b"\x17\x17\x01\x63" + b"\x00" * 12):
            env.server.handle_datagram(junk, ("10.0.0.9", 1))
        self.assertEqual(env.sock.sent, [])
        self.assertEqual(env.server.counters["rx_bad"], 4)

    def test_unknown_session_gets_a_rate_limited_disconnect(self):
        env = Env()
        ghost = Peer(env, 6000)
        ghost.session = 4242
        ghost.inp()
        ghost.inp()
        replies = of_type(ghost.pull(), P.MSG_DISCONNECT)
        self.assertEqual([P.unpack_reason(p) for _, p in replies], [P.DISC_UNKNOWN_SESSION])
        env.clock.advance(1.1)
        ghost.inp()
        self.assertEqual(len(of_type(ghost.pull(), P.MSG_DISCONNECT)), 1)

    def test_wrong_session_from_a_known_address_is_refused_too(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.raw(P.MSG_INPUT, P.pack_input(1.0, 0, 0, 0), session=a.session ^ 1)
        self.assertEqual(env.server.game.players[a.pid].throttle, 0.0)
        self.assertEqual(len(of_type(a.pull(), P.MSG_DISCONNECT)), 1)

    def test_host_is_first_player_and_passes_on_when_they_leave(self):
        env = Env()
        a, b, c = Peer(env, 5001), Peer(env, 5002), Peer(env, 5003)
        for p in (a, b, c):
            p.join()
        self.assertEqual(env.server.host_id, a.pid)
        a.raw(P.MSG_LEAVE)
        self.assertEqual(env.server.host_id, b.pid)
        env.run(0.1)
        flags = [s.flags for s in snapshots(b.pull())]
        self.assertTrue(flags and all(f & P.SNAPF_HOST for f in flags))
        self.assertFalse(any(s.flags & P.SNAPF_HOST for s in snapshots(c.pull())))


class CommandTests(unittest.TestCase):
    def test_input_applies_only_the_newest_packet(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.inp(1.0, 0.5, seq=5)
        a.inp(-1.0, -1.0, seq=4)             # late: must not undo the newer controls
        a.inp(0.0, 0.0, seq=5)               # duplicate
        p = env.server.game.players[a.pid]
        self.assertAlmostEqual(p.throttle, 1.0, places=2)
        self.assertAlmostEqual(p.steer, 0.5, delta=0.01)
        t = env.server.clients[a.pid].tracker
        self.assertEqual((t.reordered, t.duplicates), (1, 1))

    def test_ready_and_shape_change_the_roster(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        v0 = env.server.roster_ver
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_READY, 1))
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_READY, 1))        # idempotent: no second bump
        v1 = env.server.roster_ver
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_SHAPE, P.SHAPE_SQUARE))
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_SHAPE, 9))        # invalid: ignored
        self.assertEqual((v1 - v0) & 0xFF, 1)
        self.assertEqual((env.server.roster_ver - v1) & 0xFF, 1)
        a.pull()
        a.raw(P.MSG_ROSTER_REQ)
        (_, payload), = of_type(a.pull(), P.MSG_ROSTER)
        version, entries = P.unpack_roster(payload)
        self.assertEqual(version, env.server.roster_ver)
        self.assertEqual([(e.name, e.shape, e.ready, e.host) for e in entries],
                         [("Peer", P.SHAPE_SQUARE, True, True)])

    def test_only_the_host_can_start(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        b.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        self.assertEqual(env.server.game.state, P.ST_LOBBY)
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        self.assertEqual(env.server.game.state, P.ST_COUNTDOWN)

    def test_require_ready_waits_for_the_other_players(self):
        env = Env(require_ready=True)
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        self.assertEqual(env.server.game.state, P.ST_LOBBY)
        b.raw(P.MSG_CMD, P.pack_cmd(P.CMD_READY, 1))
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        self.assertEqual(env.server.game.state, P.ST_COUNTDOWN)

    def test_commands_are_ignored_once_the_race_is_on(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_START))
        a.raw(P.MSG_CMD, P.pack_cmd(P.CMD_SHAPE, P.SHAPE_TRIANGLE))
        self.assertEqual(env.server.game.players[a.pid].shape, P.SHAPE_CIRCLE)

    def test_ping_is_echoed_and_leave_removes_once(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.raw(P.MSG_PING, P.pack_ping(4321))
        (_, payload), = of_type(a.pull(), P.MSG_PONG)
        self.assertEqual(P.unpack_ping(payload), 4321)
        a.raw(P.MSG_LEAVE)
        a.raw(P.MSG_LEAVE)                                  # duplicate copy of the same quit
        self.assertEqual(len(env.server.clients), 0)
        self.assertEqual(len([e for e in env.server.event_log if e.type == P.EV_PLAYER_LEAVE]), 1)

    def test_outgoing_seq_counts_every_datagram_and_ack_follows_the_client(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.inp(seq=40)
        env.run(0.2)
        msgs = a.pull()
        seqs = [h.seq for h, _ in msgs]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        self.assertEqual(msgs[-1][0].ack, 40)


class SnapshotAndEventTests(unittest.TestCase):
    def test_twenty_snapshots_per_second_with_all_players(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        a.pull()
        env.run(1.0)
        snaps = snapshots(a.pull())
        self.assertEqual(len(snaps), 20)
        self.assertEqual({p.id for p in snaps[-1].players}, {a.pid, b.pid})
        self.assertEqual(snaps[-1].state, P.ST_LOBBY)
        self.assertEqual(snaps[-1].roster_ver, env.server.roster_ver)

    def test_events_repeat_until_the_client_acknowledges_them(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        a.pull()
        env.run(0.3)
        snaps = snapshots(a.pull())
        self.assertGreaterEqual(len(snaps), 5)
        for s in snaps:                       # B's join is repeated in every snapshot
            self.assertEqual([(e.type, e.player) for e in s.events], [(P.EV_PLAYER_JOIN, b.pid)])
        eid = snaps[0].events[0].id
        a.inp(ack_event=eid)
        env.run(0.2)
        self.assertTrue(all(not s.events for s in snapshots(a.pull())))

    def test_a_new_client_does_not_replay_old_events(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        env.run(0.2)
        c = Peer(env, 5003)
        c.join()
        c.pull()
        env.run(0.2)
        for s in snapshots(c.pull()):
            self.assertEqual(s.events, [])

    def test_an_ack_from_the_future_is_ignored(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        a.pull()
        a.inp(ack_event=env.server.last_event_id + 5)
        env.run(0.2)
        self.assertTrue(all(s.events for s in snapshots(a.pull())))

    def test_at_most_sixteen_events_per_snapshot_oldest_first(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        a.pull()
        for i in range(40):
            env.server.game.emit(P.EV_HIT, a.pid, 5, i)
        env.run(0.1)
        first = snapshots(a.pull())[0]
        self.assertEqual([e.b for e in first.events], list(range(16)))
        a.inp(ack_event=first.events[-1].id)
        env.run(0.1)
        second = snapshots(a.pull())[-1]
        self.assertEqual([e.b for e in second.events], list(range(16, 32)))

    def test_event_ids_wrap_and_skip_zero(self):
        env = Env()
        a = Peer(env, 5001)
        a.join()
        env.server.last_event_id = 65534
        for i in range(3):
            env.server.game.emit(P.EV_HIT, a.pid, 5, i)
        env.server._pump_events(env.clock())
        self.assertEqual([e.id for e in list(env.server.event_log)[-3:]], [65535, 1, 2])


class TimeoutAndStatsTests(unittest.TestCase):
    def test_silent_client_times_out_and_the_others_are_told(self):
        env = Env()
        a, b = Peer(env, 5001), Peer(env, 5002)
        a.join()
        b.join()
        for _ in range(12):                       # B keeps talking, A goes silent
            b.raw(P.MSG_PING, P.pack_ping(1))
            env.run(0.5)
        self.assertNotIn(a.pid, env.server.clients)
        self.assertIn(b.pid, env.server.clients)
        reasons = [P.unpack_reason(p) for _, p in of_type(a.pull(), P.MSG_DISCONNECT)]
        self.assertEqual(reasons, [P.DISC_TIMEOUT])
        leave = [e for s in snapshots(b.pull()) for e in s.events if e.type == P.EV_PLAYER_LEAVE]
        self.assertTrue(leave and leave[0].player == a.pid and leave[0].a == P.DISC_TIMEOUT)
        self.assertEqual(env.server.host_id, b.pid)

    def test_loss_reordering_and_duplicates_are_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stats.csv")
            env = Env(stats_csv=path, label="unit")
            a = Peer(env, 5001, "Alice")
            a.join()
            for seq in (1, 2, 3, 8, 5, 8):        # 4..7 missing, 5 arrives late, 8 duplicated
                a.inp(seq=seq)
            t = env.server.clients[a.pid].tracker
            self.assertEqual((t.expected, t.received, t.lost, t.reordered, t.duplicates), (8, 5, 3, 1, 1))
            env.clock.advance(1.0)
            env.server._write_stats(env.clock())
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["label"], row["name"], row["rx_lost"], row["rx_reordered"],
                              row["rx_duplicates"], row["state"]), ("unit", "Alice", "3", "1", "1", "LOBBY"))
            self.assertAlmostEqual(float(row["rx_loss_pct"]), 37.5, places=2)
            env.server.close()                    # release the CSV before the folder is deleted

    def test_jitter_is_measured_from_the_ts_field(self):
        env = Env()
        steady, bumpy = Peer(env, 5001), Peer(env, 5002)
        steady.join()
        bumpy.join()
        send_t = env.clock.t
        for i in range(40):                       # both send every 33 ms
            send_t += 0.033
            for peer, delay in ((steady, 0.004), (bumpy, 0.020 if i % 2 else 0.0)):
                env.clock.t = send_t + delay      # arrival = send time + network delay
                data = P.encode(P.MSG_INPUT, peer.seq, 0, peer.session, int(send_t * 1000) & 0xFFFFFFFF,
                                P.pack_input(0, 0, 0, 0))
                peer.seq = P.seq_next(peer.seq)
                env.server.handle_datagram(data, peer.addr)
        clients = env.server.clients
        self.assertLess(clients[steady.pid].jitter.value, 1.5)    # constant delay: ~0 (ms rounding only)
        self.assertGreater(clients[bumpy.pid].jitter.value, 10.0)  # alternating 20 ms: converges to ~20


class FullRaceTests(unittest.TestCase):
    def test_auto_start_race_results_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {k: os.path.join(tmp, k + ".csv") for k in ("stats", "results", "events")}
            env = Env(GameConfig(laps=1, countdown_s=1.0, results_s=2.0), auto_start_players=2,
                      auto_start_delay=0.5, exit_after_races=1, stats_csv=paths["stats"],
                      results_csv=paths["results"], event_log=paths["events"], label="race")
            a, b = Peer(env, 5001, "Alice", P.SHAPE_TRIANGLE), Peer(env, 5002, "Bob", P.SHAPE_SQUARE)
            a.join()
            b.join()
            game = env.server.game
            pilots = {a.pid: Autopilot(game.track), b.pid: Autopilot(game.track, skill=0.85)}
            peers = {a.pid: a, b.pid: b}
            for tick in range(60 * 120):
                if game.state == P.ST_RACING and tick % 2 == 0:
                    for pid, peer in peers.items():
                        p = game.players[pid]
                        thr, steer = pilots[pid].control(p.x, p.y, p.heading, p.speed, game.race_time)
                        peer.inp(thr, steer)
                env.run(1.0 / 60)
                env.sock.sent.clear()
                if env.server._stop.is_set():
                    break
            self.assertTrue(env.server._stop.is_set())
            self.assertEqual(game.races_completed, 1)
            with open(paths["results"], newline="") as f:
                results = list(csv.DictReader(f))
            self.assertEqual(sorted(r["place"] for r in results), ["1", "2"])
            self.assertEqual({r["label"] for r in results}, {"race"})
            with open(paths["events"], newline="") as f:
                names = {r["event"] for r in csv.DictReader(f)}
            self.assertTrue({"PLAYER_JOIN", "RACE_START", "LAP", "FINISH", "RACE_END"} <= names)
            with open(paths["stats"], newline="") as f:
                self.assertGreater(len(list(csv.DictReader(f))), 10)
            env.server.close()                    # release the CSV files before the folder is deleted


    def test_the_server_still_stops_when_everybody_leaves_the_moment_the_race_ends(self):
        """Bots started with --linger 0 quit at once; the game then drops straight back to the lobby, and
        --exit-after-races used to wait for the results state for ever (the server never stopped)."""
        env = Env(GameConfig(laps=1, countdown_s=1.0, results_s=8.0), auto_start_players=2, auto_start_delay=0.5,
                  exit_after_races=1)
        a, b = Peer(env, 5001, "Alice"), Peer(env, 5002, "Bob")
        a.join()
        b.join()
        game = env.server.game
        pilots = {a.pid: Autopilot(game.track), b.pid: Autopilot(game.track, skill=0.85)}
        peers = {a.pid: a, b.pid: b}
        left_at = None
        for tick in range(60 * 150):
            if game.state == P.ST_RACING and tick % 2 == 0:
                for pid, peer in peers.items():
                    p = game.players[pid]
                    peer.inp(*pilots[pid].control(p.x, p.y, p.heading, p.speed, game.race_time))
            if game.state == P.ST_FINISHED and left_at is None:
                left_at = tick
                a.raw(P.MSG_LEAVE)
                b.raw(P.MSG_LEAVE)
            env.run(1.0 / 60)
            env.sock.sent.clear()
            if env.server._stop.is_set():
                break
        self.assertIsNotNone(left_at, "the race never finished")
        self.assertTrue(env.server._stop.is_set())
        self.assertLess(tick - left_at, 60 * 3)                   # soon after they left, long before results_s


class LogTests(unittest.TestCase):
    def test_a_name_the_console_cannot_show_does_not_stop_the_server(self):
        """Windows writes redirected output in a legacy code page (cp1252).  A player whose name uses another
        script must not crash the server loop through its log line (this used to raise UnicodeEncodeError)."""
        env = Env()
        env.server.verbose = True
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        a = Peer(env, 5001, "\u540d\u524d\u2603")
        with mock.patch.object(sys, "stdout", console):
            ack = a.join()
            env.run(0.5)
            b = Peer(env, 5002, "Zo\u00eb")                      # a name cp1252 can show is printed as it is
            b.join()
        console.flush()
        self.assertIsInstance(ack, P.JoinAck)
        text = console.buffer.getvalue().decode("cp1252")
        self.assertIn("??? joined from", text)
        self.assertIn("Zo\u00eb joined from", text)
        self.assertEqual(len(env.server.game.players), 2)


class ConfigAndSocketTests(unittest.TestCase):
    def test_track_seed_must_fit_32_bits(self):
        with self.assertRaises(ValueError):
            GameServer(GameConfig(track_seed=2 ** 32), sock=FakeSock(), verbose=False)

    def test_command_line_rejects_bad_settings(self):
        self.assertEqual(main(["--laps", "0", "--port", "0", "--quiet"]), 2)

    def test_real_udp_round_trip(self):
        server = GameServer(GameConfig(laps=1), "127.0.0.1", 0, verbose=False)
        server.start_thread()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            sock.sendto(P.encode(P.MSG_JOIN, 0, 0, 0, 0, P.pack_join(7, "Net", P.SHAPE_SQUARE)),
                        server.address)
            header, payload = P.decode(sock.recvfrom(2048)[0])
            self.assertEqual(header.msg_type, P.MSG_JOIN_ACK)
            self.assertEqual(P.unpack_join_ack(payload).nonce, 7)
            count, end = 0, time.time() + 1.0
            while time.time() < end:
                try:
                    h, _ = P.decode(sock.recvfrom(2048)[0])
                except socket.timeout:
                    break
                count += h.msg_type == P.MSG_SNAPSHOT
            self.assertTrue(10 <= count <= 30, count)
        finally:
            sock.close()
            server.stop()


if __name__ == "__main__":
    unittest.main()
