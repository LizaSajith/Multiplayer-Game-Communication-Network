"""Real UDP on 127.0.0.1: the real GameServer and real NetClients with a misbehaving "network" in between.

``LossyProxy`` is a UDP relay.  Clients talk to it as if it were the server; it gives every client its own
socket towards the server and drops, duplicates and delays datagrams (a delayed datagram is overtaken by
later ones, which is reordering) in both directions.  This is the same job ``tc netem`` does in Mininet,
so the numbers here are what the loss / reordering / duplicate statistics of the project should show.

These tests take real time (about 8 seconds together) because the server runs in real time."""
import csv
import heapq
import os
import random
import select
import socket
import tempfile
import threading
import time
import unittest

from app import protocol as P
from app.game import GameConfig
from app.netclient import JOINED, NetClient, run_bots
from app.server import GameServer


class LossyProxy:
    def __init__(self, server_addr, *, loss=0.0, dup=0.0, reorder=0.0, delay=(0.01, 0.06), seed=1,
                 drop_first_up=0, drop_first_down=0):
        self.server_addr = server_addr
        self.loss, self.dup, self.reorder, self.delay = loss, dup, reorder, delay
        self.rng = random.Random(seed)
        self.drop_first = {"up": drop_first_up, "down": drop_first_down}      # scripted, timing independent
        self.stats = {k: 0 for k in ("up_in", "down_in", "up_dropped", "down_dropped", "up_duplicated",
                                     "down_duplicated", "up_delayed", "down_delayed")}
        self.front = self._new_socket()
        self.address = self.front.getsockname()
        self.backs = {}                    # client address -> socket towards the server
        self.owner = {}                    # that socket -> client address
        self.due = []                      # heap of (time, n, socket, data, destination)
        self.n = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lossy-proxy", daemon=True)
        self._thread.start()

    # ---- the relay loop (one thread)
    def _run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.front] + list(self.backs.values()), [], [], 0.002)
            except (OSError, ValueError):
                break
            for sock in ready:
                self._drain(sock)
            now = time.monotonic()
            while self.due and self.due[0][0] <= now:
                _, _, sock, data, dest = heapq.heappop(self.due)
                self._send(sock, data, dest)

    def _drain(self, sock):
        for _ in range(500):
            try:
                data, addr = sock.recvfrom(4096)
            except BlockingIOError:
                return
            except OSError:
                continue
            if sock is self.front:
                self._relay("up", data, addr)
            else:
                self._relay("down", data, self.owner[sock])

    def _relay(self, direction, data, client):
        self.stats[direction + "_in"] += 1
        if self.drop_first[direction] > 0:
            self.drop_first[direction] -= 1
            self.stats[direction + "_dropped"] += 1
            return
        if self.rng.random() < self.loss:
            self.stats[direction + "_dropped"] += 1
            return
        copies = 1
        if self.rng.random() < self.dup:
            copies = 2
            self.stats[direction + "_duplicated"] += 1
        for _ in range(copies):
            delay = 0.0
            if self.rng.random() < self.reorder:
                delay = self.rng.uniform(*self.delay)
                self.stats[direction + "_delayed"] += 1
            if direction == "up":
                sock, dest = self._back_for(client), self.server_addr
            else:
                sock, dest = self.front, client
            if delay <= 0.0:
                self._send(sock, data, dest)
            else:
                self.n += 1
                heapq.heappush(self.due, (time.monotonic() + delay, self.n, sock, data, dest))

    @staticmethod
    def _new_socket():
        """A non-blocking loopback socket with a big receive buffer: the proxy must never be the one that
        loses datagrams (the kernel drops a burst when its buffer is full), only on purpose."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
        except OSError:
            pass
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        return sock

    def _back_for(self, client):
        sock = self.backs.get(client)
        if sock is None:
            sock = self._new_socket()
            self.backs[client], self.owner[sock] = sock, client
        return sock

    @staticmethod
    def _send(sock, data, dest):
        try:
            sock.sendto(data, dest)
        except OSError:
            pass

    def close(self):
        self._stop.set()
        self._thread.join(2.0)
        for sock in [self.front] + list(self.backs.values()):
            sock.close()


class LoopbackTests(unittest.TestCase):
    def test_the_proxy_really_relays_and_impairs(self):
        """Self-check of the test tool: an echo server behind it, 300 datagrams through 20 % loss."""
        echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        echo.bind(("127.0.0.1", 0))
        echo.settimeout(0.05)
        proxy = LossyProxy(echo.getsockname(), loss=0.2, dup=0.1, reorder=0.3, seed=3)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.05)
        try:
            for i in range(300):
                client.sendto(i.to_bytes(2, "big"), proxy.address)
                if i % 10 == 9:
                    time.sleep(0.003)                              # a gentle stream, not one burst
            seen, end = [], time.monotonic() + 0.6
            while time.monotonic() < end:
                try:
                    data, addr = echo.recvfrom(100)
                except socket.timeout:
                    continue
                seen.append(int.from_bytes(data, "big"))
                echo.sendto(data, addr)                            # reply to the proxy's socket for this client
            back = []
            while True:
                try:
                    back.append(int.from_bytes(client.recvfrom(100)[0], "big"))
                except socket.timeout:
                    break
        finally:
            proxy.close()
            client.close()
            echo.close()
        self.assertEqual(proxy.stats["up_in"], 300)                # loopback itself loses nothing
        self.assertGreater(proxy.stats["up_dropped"], 30)
        self.assertGreater(proxy.stats["up_duplicated"], 5)
        self.assertGreater(proxy.stats["up_delayed"], 40)
        self.assertLess(len(set(seen)), 300)                       # some are gone for good
        self.assertGreater(len(seen), len(set(seen)))              # some arrived twice
        self.assertNotEqual(seen, sorted(seen))                    # and the order was shuffled
        self.assertTrue(set(back) <= set(seen))                    # replies find their way back to the client
        self.assertGreater(len(back), 50)

    def test_join_is_retried_until_it_gets_through(self):
        """The first two JOINs and the first two datagrams coming back (the JOIN_ACK and a snapshot) are thrown
        away; scripted, so no luck is involved.  The client keeps asking, the server answers the repeated
        JOIN with the same ACK, and exactly one player exists."""
        server = GameServer(GameConfig(laps=1), "127.0.0.1", 0, verbose=False)
        server.start_thread()
        proxy = LossyProxy(server.address, drop_first_up=2, drop_first_down=2)
        client = NetClient(proxy.address, "solo", P.SHAPE_TRIANGLE)
        started = time.monotonic()
        try:
            client.start()
            self.assertTrue(client.wait_joined(timeout=P.JOIN_TIMEOUT_S))
            elapsed = time.monotonic() - started
            self.assertTrue(1.4 < elapsed < 4.0, elapsed)          # JOINs at 0 and 0.5 s lost; the ACK to 1.0 s lost
            self.assertEqual(len(server.clients), 1)               # the repeated JOINs did not create copies
            self.assertEqual((proxy.stats["up_dropped"], proxy.stats["down_dropped"]), (2, 2))
            self.assertEqual(client.player_id, 1)
            self.assertEqual(client.track.hash, server.game.track.hash)
            end = time.monotonic() + 1.0
            while client.snapshot_count < 5 and time.monotonic() < end:
                client.pump()
                time.sleep(0.005)
            self.assertGreaterEqual(client.snapshot_count, 5)      # and the game data flows
            client.leave()
            time.sleep(0.3)
            self.assertEqual(len(server.clients), 0)
        finally:
            client.close()
            proxy.close()
            server.stop()
            server.close()

    def test_four_players_race_over_a_lossy_reordering_link(self):
        """10 % loss, 5 % duplicates and 15 % delayed (so reordered) datagrams in both directions.  Four
        autopilot clients still join, the host's START gets through, the race runs, every client sees every
        player, and no event is missed: ids are consecutive, RACE_START appears exactly once."""
        cfg = GameConfig(laps=1, countdown_s=0.5, time_limit_s=0.0)
        with tempfile.TemporaryDirectory() as tmp:
            stats_path = os.path.join(tmp, "stats.csv")
            server = GameServer(cfg, "127.0.0.1", 0, verbose=False, stats_csv=stats_path, stats_interval=0.5)
            server.start_thread()
            proxy = LossyProxy(server.address, loss=0.10, dup=0.05, reorder=0.15, seed=17)
            try:
                clients = run_bots(proxy.address, 4, skill=0.9, start=True, duration=7.0, verbose=False)
            finally:
                proxy.close()
                server.stop()
                server.close()
            stats = proxy.stats
            for d in ("up", "down"):                               # the impairments really happened
                self.assertGreater(stats[d + "_dropped"], 20, stats)
                self.assertGreater(stats[d + "_duplicated"], 5, stats)
                self.assertGreater(stats[d + "_delayed"], 20, stats)
            self.assertEqual(server.counters["rx_bad"], 0)
            self.assertGreater(server.game.race_time, 2.0)         # the race really ran on the server

            start_ids = set()
            for c in clients:
                self.assertGreater(c.snapshot_count, 60, c.name)   # ~20 per second, minus the losses
                self.assertEqual(sorted(c.players), [1, 2, 3, 4], c.name)
                self.assertEqual(c.game_state, P.ST_RACING, c.name)
                self.assertGreater(max(p.progress for p in c.players.values()), 300.0, c.name)
                ids = [e.id for e in c.events]
                self.assertTrue(ids, c.name)
                self.assertEqual(ids, list(range(ids[0], ids[0] + len(ids))), "events missing for " + c.name)
                starts = [e for e in c.events if e.type == P.EV_RACE_START]
                self.assertEqual(len(starts), 1, c.name)
                start_ids.add(starts[0].id)
                self.assertEqual(c.bad_datagrams, 0, c.name)
            self.assertEqual(len(start_ids), 1)                    # all saw the same event
            # the clients' own measurements see the damage on the downlink
            self.assertGreater(sum(c.tracker.lost for c in clients), 10)
            self.assertGreater(sum(c.tracker.reordered for c in clients), 3)
            self.assertGreater(sum(c.tracker.duplicates for c in clients), 5)
            loss = [c.tracker.loss_ratio for c in clients]
            self.assertTrue(0.03 < sum(loss) / len(loss) < 0.25, loss)    # 10 % configured, a little less is seen
            # and so does the server's CSV, which is what the experiments use, for the uplink
            with open(stats_path, newline="") as f:
                rows = list(csv.DictReader(f))
            last = {}
            for r in rows:
                last[r["player_id"]] = r
            self.assertEqual(sorted(last), ["1", "2", "3", "4"])
            self.assertTrue(all(int(r["rx_expected"]) > 100 for r in last.values()))
            self.assertGreater(sum(int(r["rx_lost"]) for r in last.values()), 10)
            self.assertGreater(sum(int(r["rx_reordered"]) for r in last.values()), 3)
            self.assertGreater(sum(int(r["rx_duplicates"]) for r in last.values()), 5)


if __name__ == "__main__":
    unittest.main()
