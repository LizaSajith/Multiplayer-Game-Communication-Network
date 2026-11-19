"""Unit tests for app/protocol.py (run: python -m unittest discover -s tests -v)."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import protocol as P


class HeaderTests(unittest.TestCase):
    def test_header_size(self):
        self.assertEqual(P.HEADER_SIZE, 16)

    def test_roundtrip(self):
        data = P.encode(P.MSG_INPUT, 65535, 12, 0xDEADBEEF, 123456, b"xyz")
        h, payload = P.decode(data)
        self.assertEqual((h.msg_type, h.seq, h.ack, h.session, h.ts, h.version),
                         (P.MSG_INPUT, 65535, 12, 0xDEADBEEF, 123456, P.PROTOCOL_VERSION))
        self.assertEqual(payload, b"xyz")

    def test_seq_and_ts_wrap_on_encode(self):
        h, _ = P.decode(P.encode(P.MSG_PING, 65536 + 5, 70000, 1, 2 ** 32 + 9))
        self.assertEqual((h.seq, h.ack, h.ts), (5, 70000 & 0xFFFF, 9))

    def test_rejects_garbage(self):
        good = P.encode(P.MSG_PING, 1, 0, 0, 0, P.pack_ping(1))
        with self.assertRaises(P.ProtocolError):
            P.decode(b"")
        with self.assertRaises(P.ProtocolError):
            P.decode(good[:15])                       # truncated header
        with self.assertRaises(P.ProtocolError):
            P.decode(b"\x00\x00" + good[2:])          # bad magic
        bad_type = bytearray(good)
        bad_type[3] = 200
        with self.assertRaises(P.ProtocolError):
            P.decode(bytes(bad_type))                 # unknown type
        with self.assertRaises(P.ProtocolError):
            P.decode(good + b"\0" * 2000)             # oversized

    def test_oversize_encode(self):
        with self.assertRaises(P.ProtocolError):
            P.encode(P.MSG_SNAPSHOT, 0, 0, 0, 0, b"\0" * 2000)

    def test_version_not_enforced_by_decode(self):
        d = bytearray(P.encode(P.MSG_JOIN, 0, 0, 0, 0, P.pack_join(1, "a", 0)))
        d[2] = 99
        h, _ = P.decode(bytes(d))
        self.assertEqual(h.version, 99)


class SeqHelperTests(unittest.TestCase):
    def test_seq_gt_wrap(self):
        self.assertTrue(P.seq_gt(1, 0))
        self.assertTrue(P.seq_gt(0, 65535))           # wrapped, 0 is newer
        self.assertFalse(P.seq_gt(65535, 0))
        self.assertFalse(P.seq_gt(5, 5))
        self.assertTrue(P.seq_gt(100, 65500))

    def test_seq_diff(self):
        self.assertEqual(P.seq_diff(3, 65534), 5)
        self.assertEqual(P.seq_diff(65534, 3), -5)
        self.assertEqual(P.seq_diff(7, 7), 0)

    def test_ts_diff(self):
        self.assertEqual(P.ts_diff(5, 0xFFFFFFFF), 6)
        self.assertEqual(P.ts_diff(0xFFFFFFFF, 5), -6)


class SeqTrackerTests(unittest.TestCase):
    def test_in_order(self):
        t = P.SeqTracker()
        for s in range(100):
            self.assertEqual(t.update(s), P.SeqTracker.NEW)
        self.assertEqual((t.received, t.lost, t.reordered, t.duplicates), (100, 0, 0, 0))

    def test_loss_counted(self):
        t = P.SeqTracker()
        for s in (0, 1, 2, 5, 6, 9):
            t.update(s)
        self.assertEqual(t.expected, 10)
        self.assertEqual(t.lost, 4)
        self.assertAlmostEqual(t.loss_ratio, 0.4)

    def test_reorder_and_duplicate(self):
        t = P.SeqTracker()
        for s in (0, 1, 4):
            t.update(s)
        self.assertEqual(t.lost, 2)
        self.assertEqual(t.update(3), P.SeqTracker.REORDERED)   # late arrival
        self.assertEqual(t.lost, 1)                              # no longer lost
        self.assertEqual(t.update(3), P.SeqTracker.DUPLICATE)
        self.assertEqual(t.update(4), P.SeqTracker.DUPLICATE)
        self.assertEqual(t.update(0), P.SeqTracker.DUPLICATE)
        self.assertEqual((t.reordered, t.duplicates), (1, 3))

    def test_too_old(self):
        t = P.SeqTracker()
        t.update(0)
        t.update(200)
        self.assertEqual(t.update(100), P.SeqTracker.TOO_OLD)
        self.assertEqual(t.too_old, 1)

    def test_wraparound(self):
        t = P.SeqTracker()
        for s in (65530, 65531, 65532, 65533, 65534, 65535, 0, 1, 2):
            self.assertEqual(t.update(s), P.SeqTracker.NEW)
        self.assertEqual((t.expected, t.lost), (9, 0))
        self.assertEqual(t.update(65535), P.SeqTracker.DUPLICATE)

    def test_big_jump_resets_window(self):
        t = P.SeqTracker()
        t.update(10)
        self.assertEqual(t.update(10 + 5000), P.SeqTracker.NEW)
        self.assertEqual(t.update(10 + 4999), P.SeqTracker.REORDERED)

    def test_random_shuffle_matches_ground_truth(self):
        rnd = random.Random(1)
        sent = [(65400 + i) & 0xFFFF for i in range(336)]        # crosses the 16-bit wrap
        kept = [0] + [i for i in range(1, 336) if rnd.random() > 0.2]   # 20 % loss, #0 arrives
        arrived, dups = [], 0
        for i in kept:
            arrived.append(sent[i])
            if i and rnd.random() < 0.05:                        # network duplicate, back to back
                arrived.append(sent[i])
                dups += 1
        for i in range(1, len(arrived) - 5, 5):                  # local reordering (< window)
            chunk = arrived[i:i + 5]
            rnd.shuffle(chunk)
            arrived[i:i + 5] = chunk
        t = P.SeqTracker()
        for s in arrived:
            t.update(s)
        unique = len(kept)
        self.assertEqual(t.received, unique)
        self.assertEqual(t.duplicates, dups)
        self.assertEqual(t.too_old, 0)
        self.assertEqual(t.expected, kept[-1] + 1)               # span of the stream seen
        self.assertEqual(t.lost, kept[-1] + 1 - unique)
        self.assertGreater(t.reordered, 0)


class CodecTests(unittest.TestCase):
    def test_join(self):
        nonce, name, shape = P.unpack_join(P.pack_join(0xCAFEBABE, "Manju", P.SHAPE_TRIANGLE))
        self.assertEqual((nonce, name, shape), (0xCAFEBABE, "Manju", 2))

    def test_join_bad_shape_and_short(self):
        with self.assertRaises(P.ProtocolError):
            P.unpack_join(P.pack_join(1, "x", 9))
        with self.assertRaises(P.ProtocolError):
            P.unpack_join(b"\0" * 5)

    def test_name_handling(self):
        self.assertEqual(P.unpack_name(P.pack_name("")), "Player")
        self.assertEqual(len(P.pack_name("x" * 100)), P.NAME_BYTES)
        self.assertEqual(P.unpack_name(P.pack_name("a\x00b")), "a")
        self.assertEqual(P.unpack_name(b"bad\x01\x02name".ljust(16, b"\0")), "badname")
        self.assertEqual(P.unpack_name(b"\xff\xfe\xfd".ljust(16, b"\0")).count("�"), 3)
        long_utf8 = "é" * 20                                   # 2 bytes each, cut must not split
        self.assertEqual(P.unpack_name(P.pack_name(long_utf8)), "é" * 8)

    def test_join_ack(self):
        a = P.JoinAck(77, 3, 2, 60, 20, 16, 3, 0, 17, 0xA1B2C3D4)
        self.assertEqual(P.unpack_join_ack(P.pack_join_ack(a)), a)

    def test_join_reject(self):
        self.assertEqual(P.unpack_join_reject(P.pack_join_reject(5, P.REJ_FULL)), (5, P.REJ_FULL))

    def test_input_quantisation(self):
        t, s, b, ack = P.unpack_input(P.pack_input(1.0, -1.0, 3, 65535))
        self.assertEqual((t, s, b, ack), (1.0, -1.0, 3, 65535))
        t, s, _, _ = P.unpack_input(P.pack_input(0.5, -0.25, 0, 0))
        self.assertAlmostEqual(t, 0.5, delta=0.01)
        self.assertAlmostEqual(s, -0.25, delta=0.01)
        t, s, _, _ = P.unpack_input(P.pack_input(7.0, -9.0, 0, 0))   # out of range clamps
        self.assertEqual((t, s), (1.0, -1.0))

    def test_cmd_ping_reason(self):
        self.assertEqual(P.unpack_cmd(P.pack_cmd(P.CMD_SHAPE, 2)), (P.CMD_SHAPE, 2))
        self.assertEqual(P.unpack_ping(P.pack_ping(65535)), 65535)
        self.assertEqual(P.unpack_reason(P.pack_reason(P.DISC_TIMEOUT)), P.DISC_TIMEOUT)

    def test_roster(self):
        es = [P.RosterEntry(1, 0, 0, True, True, "Alice"),
              P.RosterEntry(2, 1, 2, False, False, "Bob")]
        v, out = P.unpack_roster(P.pack_roster(9, es))
        self.assertEqual(v, 9)
        self.assertEqual(out, es)

    def test_roster_truncated(self):
        blob = P.pack_roster(1, [P.RosterEntry(1, 0, 0, False, False, "A")])
        with self.assertRaises(P.ProtocolError):
            P.unpack_roster(blob[:-3])

    def _snap(self, n=3, m=2):
        players = [P.PlayerState(i + 1, 100.5 + i, -50.25 * i, 1.0 + i, 123.4 - i, 100 - i,
                                 i % 3, i % 4, P.STATUS_STARTED, 0, 1234.5 * i - 300)
                   for i in range(n)]
        events = [P.Event(65535 - j, P.EV_HIT, 2, 15, 40 + j) for j in range(m)]
        return P.Snapshot(123456, P.ST_RACING, P.SNAPF_HOST, 7, 2500, 99999, players, events)

    def test_snapshot_roundtrip(self):
        s = self._snap()
        out = P.unpack_snapshot(P.pack_snapshot(s))
        self.assertEqual((out.tick, out.state, out.flags, out.roster_ver, out.countdown_ms,
                          out.race_time_ms), (123456, P.ST_RACING, 1, 7, 2500, 99999))
        self.assertEqual(out.events, s.events)
        for a, b in zip(s.players, out.players):
            self.assertEqual((a.id, a.health, a.lap, a.cp, a.status, a.place),
                             (b.id, b.health, b.lap, b.cp, b.status, b.place))
            self.assertAlmostEqual(a.x, b.x, places=3)
            self.assertAlmostEqual(a.progress, b.progress, places=2)
            self.assertAlmostEqual(a.speed, b.speed, delta=0.06)
            d = abs((a.heading - b.heading + 3.14159) % P.TAU - 3.14159)
            self.assertLess(d, 1e-3)

    def test_snapshot_sizes(self):
        self.assertEqual(len(P.pack_snapshot(self._snap(12, 4))) + P.HEADER_SIZE,
                         P.snapshot_wire_size(12, 4))
        self.assertLess(P.snapshot_wire_size(P.MAX_PLAYERS_HARD, P.EVENTS_PER_SNAPSHOT),
                        P.MAX_DATAGRAM)

    def test_snapshot_rejects_bad_input(self):
        blob = P.pack_snapshot(self._snap())
        with self.assertRaises(P.ProtocolError):
            P.unpack_snapshot(blob[:-1])                       # truncated
        with self.assertRaises(P.ProtocolError):
            P.unpack_snapshot(blob[:5])
        bad = bytearray(blob)
        bad[4] = 9                                             # state out of range
        with self.assertRaises(P.ProtocolError):
            P.unpack_snapshot(bytes(bad))
        bad = bytearray(blob)
        bad[13] = 200                                          # n_players too large
        with self.assertRaises(P.ProtocolError):
            P.unpack_snapshot(bytes(bad))

    def test_snapshot_nan_rejected(self):
        s = self._snap(1, 0)
        s.players[0].x = float("nan")
        with self.assertRaises(P.ProtocolError):
            P.unpack_snapshot(P.pack_snapshot(s))

    def test_fuzz_decoders_never_crash(self):
        rnd = random.Random(7)
        decoders = [P.unpack_join, P.unpack_join_ack, P.unpack_join_reject, P.unpack_input,
                    P.unpack_cmd, P.unpack_ping, P.unpack_reason, P.unpack_roster,
                    P.unpack_snapshot]
        for _ in range(3000):
            blob = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 120)))
            for dec in decoders:
                try:
                    dec(blob)
                except P.ProtocolError:
                    pass
            try:
                P.decode(blob)
            except P.ProtocolError:
                pass


if __name__ == "__main__":
    unittest.main()
