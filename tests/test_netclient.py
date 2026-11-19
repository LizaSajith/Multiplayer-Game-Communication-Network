"""NetClient tests, part 1: interpolation buffer, join handshake, reconnect.

The test plays the server over a fake socket and a fake clock (see nethelpers.py)."""
import math
import time
import unittest

from app import protocol as P
from app.netclient import (DISCONNECTED, FAILED, IDLE, JOINED, JOINING, LEFT, REJECTED, NetClient,
                           SnapshotBuffer)
from tests.nethelpers import NONCE, SERVER, SESSION, TRACK, Env, FakeSock, car, ev, make_ack


def snap(tick, *players):
    return P.Snapshot(tick, P.ST_RACING, 0, 0, 0, 0, list(players), [])


class SnapshotBufferTests(unittest.TestCase):
    def pair(self, first, second):
        """Two snapshots 0.2 s apart (ticks 0 and 12 at 60 Hz), delivered without jitter."""
        buf = SnapshotBuffer(60, 0.1)
        self.assertTrue(buf.add(snap(0, *first), 10.0))
        self.assertTrue(buf.add(snap(12, *second), 10.2))
        return buf

    @staticmethod
    def when(buf, server_t):
        """The local time at which the buffer shows server time ``server_t``."""
        return server_t + buf.offset + buf.delay

    def test_empty_buffer_has_nothing_to_show(self):
        buf = SnapshotBuffer()
        self.assertEqual(len(buf), 0)
        self.assertEqual(buf.sample(5.0), {})

    def test_positions_are_interpolated_between_snapshots(self):
        buf = self.pair([car(1, 0.0, 0.0, speed=100.0)], [car(1, 100.0, 40.0, speed=200.0)])
        pose = buf.sample(self.when(buf, 0.1))[1]
        self.assertAlmostEqual(pose.x, 50.0, places=6)
        self.assertAlmostEqual(pose.y, 20.0, places=6)
        self.assertAlmostEqual(pose.speed, 150.0, places=6)

    def test_before_the_first_snapshot_the_first_one_is_shown(self):
        buf = self.pair([car(1, 7.0, 0.0)], [car(1, 90.0, 0.0)])
        self.assertEqual(buf.sample(self.when(buf, -5.0))[1].x, 7.0)

    def test_a_car_is_extrapolated_a_little_past_the_newest_snapshot(self):
        buf = self.pair([car(1, 0.0, 0.0, speed=100.0)], [car(1, 100.0, 0.0, speed=100.0)])
        self.assertAlmostEqual(buf.sample(self.when(buf, 0.3))[1].x, 110.0, places=6)   # +0.1 s at 100 u/s
        self.assertAlmostEqual(buf.sample(self.when(buf, 5.0))[1].x, 125.0, places=6)   # capped at 0.25 s

    def test_wrecked_and_finished_cars_stay_where_they_are(self):
        cars = [car(1, 100.0, 0.0, speed=100.0, status=P.STATUS_WRECKED),
                car(2, 100.0, 0.0, speed=100.0, status=P.STATUS_FINISHED)]
        buf = self.pair(cars, cars)
        poses = buf.sample(self.when(buf, 1.0))
        self.assertEqual((poses[1].x, poses[2].x), (100.0, 100.0))

    def test_heading_turns_the_short_way_round(self):
        buf = self.pair([car(1, 0.0, 0.0, heading=6.2)], [car(1, 1.0, 0.0, heading=0.1)])
        h = buf.sample(self.when(buf, 0.1))[1].heading
        self.assertAlmostEqual((h + math.pi) % math.tau - math.pi, 0.0084, delta=0.001)

    def test_a_respawn_jump_is_not_slid_across_the_track(self):
        buf = self.pair([car(1, 0.0, 0.0)], [car(1, 500.0, 0.0)])
        self.assertEqual(buf.sample(self.when(buf, 0.05))[1].x, 0.0)
        self.assertEqual(buf.sample(self.when(buf, 0.15))[1].x, 500.0)

    def test_old_and_repeated_ticks_are_rejected(self):
        buf = SnapshotBuffer(60, 0.1)
        self.assertTrue(buf.add(snap(10, car(1)), 1.0))
        self.assertFalse(buf.add(snap(10, car(1)), 1.1))
        self.assertFalse(buf.add(snap(9, car(1)), 1.2))
        self.assertTrue(buf.add(snap(11, car(1)), 1.3))
        self.assertEqual(len(buf), 2)

    def test_the_clock_offset_follows_the_fastest_packets(self):
        buf = SnapshotBuffer(60, 0.1)
        buf.add(snap(0, car(1)), 10.0)
        self.assertAlmostEqual(buf.offset, 10.0)
        buf.add(snap(6, car(1)), 10.5)            # 0.4 s late: jitter, not a new clock
        self.assertAlmostEqual(buf.offset, 10.0)
        buf.add(snap(12, car(1)), 10.15)          # a quicker packet moves the offset
        self.assertAlmostEqual(buf.offset, 9.95)

    def test_players_follow_the_newer_snapshot(self):
        buf = self.pair([car(1, 0.0, 0.0), car(2, 0.0, 0.0)], [car(1, 10.0, 0.0), car(3, 5.0, 0.0)])
        poses = buf.sample(self.when(buf, 0.1))
        self.assertEqual(sorted(poses), [1, 3])           # 2 left, 3 joined
        self.assertAlmostEqual(poses[1].x, 5.0)
        self.assertEqual(poses[3].x, 5.0)

    def test_clear_forgets_everything(self):
        buf = self.pair([car(1)], [car(1)])
        buf.clear()
        self.assertEqual((len(buf), buf.sample(11.0)), (0, {}))


class JoinTests(unittest.TestCase):
    def test_start_sends_a_join(self):
        env = Env()
        self.assertEqual(env.client.state, IDLE)
        env.client.start()
        ((_, header, payload),) = env.sent(P.MSG_JOIN)
        self.assertEqual(P.unpack_join(payload), (NONCE, "Tester", P.SHAPE_SQUARE))
        self.assertEqual(header.session, 0)
        self.assertEqual(env.sock.sent[0][2], SERVER)
        self.assertEqual(env.client.state, JOINING)

    def test_join_is_repeated_until_answered(self):
        env = Env()
        env.client.start()
        env.run(1.2, keepalive=False)
        joins = env.payloads(P.MSG_JOIN)
        self.assertEqual(len(joins), 3)                    # at 0 s, 0.5 s and 1.0 s
        self.assertEqual(len(set(joins)), 1)               # always the same nonce

    def test_no_answer_fails_after_the_join_timeout(self):
        env = Env()
        env.client.start()
        env.run(5.2, keepalive=False)
        self.assertEqual(env.client.state, FAILED)
        self.assertIn("no answer", env.client.error)
        count = len(env.sent(P.MSG_JOIN))
        env.run(2.0, keepalive=False)
        self.assertEqual(len(env.sent(P.MSG_JOIN)), count)    # it gave up

    def test_accepted_join(self):
        env = Env()
        c = env.accept()
        self.assertEqual(c.state, JOINED)
        self.assertTrue(c.active)
        self.assertEqual((c.player_id, c.slot, c.session), (3, 2, SESSION))
        self.assertEqual(c.track.hash, TRACK.hash)
        self.assertEqual((c.ack.laps, c.buffer.tick_rate), (2, 60))
        inputs = env.sent(P.MSG_INPUT)
        self.assertTrue(inputs)
        self.assertEqual(inputs[0][1].session, SESSION)    # from now on every packet carries the session

    def test_ack_for_another_nonce_is_ignored(self):
        env = Env()
        env.client.start()
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack(nonce=NONCE + 1)))
        env.run(0.05)
        self.assertEqual(env.client.state, JOINING)

    def test_a_track_that_does_not_match_fails_the_join(self):
        env = Env()
        env.client.start()
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack(track_hash=TRACK.hash ^ 1)))
        env.run(0.05)
        self.assertEqual(env.client.state, FAILED)
        self.assertIn("track", env.client.error)

    def test_an_unknown_track_id_fails_the_join(self):
        env = Env()
        env.client.start()
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack(track_id=200)))
        env.run(0.05)
        self.assertEqual(env.client.state, FAILED)

    def test_join_reject_is_reported(self):
        env = Env()
        env.client.start()
        env.deliver(P.MSG_JOIN_REJECT, P.pack_join_reject(NONCE + 1, P.REJ_VERSION), session=0)
        env.run(0.02)
        self.assertEqual(env.client.state, JOINING)        # somebody else's rejection
        env.deliver(P.MSG_JOIN_REJECT, P.pack_join_reject(NONCE, P.REJ_FULL), session=0)
        env.run(0.02)
        self.assertEqual(env.client.state, REJECTED)
        self.assertEqual(env.client.reject_reason, P.REJ_FULL)
        self.assertEqual(env.client.error, P.REASON_TEXT[P.REJ_FULL])
        self.assertFalse(env.client.active)

    def test_packets_from_other_addresses_are_ignored(self):
        env = Env()
        env.client.start()
        ack = P.pack_join_ack(make_ack())
        env.deliver(P.MSG_JOIN_ACK, ack, addr=("10.9.9.9", SERVER[1]))
        env.deliver(P.MSG_JOIN_ACK, ack, addr=(SERVER[0], SERVER[1] + 1))
        env.run(0.05)
        self.assertEqual(env.client.state, JOINING)

    def test_garbage_is_counted_and_ignored(self):
        env = Env()
        env.client.start()
        env.sock.inbox += [(b"\x00" * 5, SERVER), (b"X" * 40, SERVER)]
        env.run(0.02)
        self.assertEqual(env.client.bad_datagrams, 2)
        self.assertEqual(env.client.state, JOINING)

    def test_a_second_start_repeats_the_same_join(self):
        env = Env()
        env.client.start()
        env.client.start()
        joins = env.payloads(P.MSG_JOIN)
        self.assertEqual(len(joins), 2)
        self.assertEqual(joins[0], joins[1])

    def test_wait_joined_returns_as_soon_as_the_ack_is_there(self):
        sock = FakeSock(time.monotonic)
        client = NetClient(SERVER, "Tester", sock=sock, nonce=NONCE)          # real clock
        client.start()
        sock.inbox.append((P.encode(P.MSG_JOIN_ACK, 0, 0, SESSION, 0, P.pack_join_ack(make_ack())), SERVER))
        self.assertTrue(client.wait_joined(timeout=2.0))

    def test_wait_joined_gives_up_on_a_silent_server(self):
        client = NetClient(SERVER, "Tester", sock=FakeSock(time.monotonic), nonce=NONCE)
        client.start()
        self.assertFalse(client.wait_joined(timeout=0.05))
        self.assertEqual(client.state, JOINING)

    def test_only_a_socket_the_client_made_is_closed(self):
        env = Env()
        env.client.close()
        self.assertFalse(env.sock.closed)                  # the caller's socket stays open
        own = NetClient(("127.0.0.1", 9))
        own.close()
        self.assertEqual(own.sock.fileno(), -1)


class ReconnectTests(unittest.TestCase):
    def test_start_after_a_disconnect_begins_a_fresh_session(self):
        env = Env()
        c = env.accept()
        env.snapshot(1, events=[ev(1)])
        env.deliver(P.MSG_DISCONNECT, P.pack_reason(P.DISC_TIMEOUT))
        env.run(0.05)
        self.assertEqual(c.state, DISCONNECTED)
        old_nonce = c.nonce
        c.start()
        self.assertEqual(c.state, JOINING)
        self.assertNotEqual(c.nonce, old_nonce)
        self.assertEqual((c.session, c.snapshot_count, c.event_count, c.roster_ver, c.error),
                         (0, 0, 0, None, ""))
        self.assertEqual(c.tracker.expected, 0)
        self.assertEqual(len(env.sent(P.MSG_JOIN)), 2)     # the first JOIN and the new one
        # the new server counts its packets from zero again: none of them may look old
        env.seq = 0
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack(nonce=c.nonce, player_id=5)),
                    session=SESSION + 7)
        env.snapshot(1, session=SESSION + 7)
        env.run(0.05)
        self.assertEqual((c.state, c.session, c.player_id, c.snapshot_count), (JOINED, SESSION + 7, 5, 1))

    def test_start_after_leave_joins_again(self):
        env = Env()
        c = env.accept()
        c.leave()
        self.assertEqual(c.state, LEFT)
        env.clear()
        c.start()
        self.assertEqual(c.state, JOINING)
        self.assertEqual(len(env.sent(P.MSG_JOIN)), 1)

    def test_start_while_joined_does_nothing(self):
        env = Env()
        c = env.accept()
        env.clear()
        c.start()
        self.assertEqual(c.state, JOINED)
        self.assertEqual(env.sent(P.MSG_JOIN), [])


if __name__ == "__main__":
    unittest.main()
