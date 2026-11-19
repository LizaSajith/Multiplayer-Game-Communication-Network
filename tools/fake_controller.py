#!/usr/bin/env python3
"""fake_controller.py - a stand-in for the Ryu controller's REST API (Role 3 test tool).

It speaks exactly the API of ``controller/game_qos.py`` (``/game/register``, ``/game/unregister``, ``/game/mode``,
``/game/status``) but touches no switch, so the game server's SDN hook, ``app/sdn_client.py`` and
``scripts/local_check.py`` can be tested on any laptop, without Mininet, Open vSwitch, Ryu or root.

    python3 tools/fake_controller.py --port 8080          # then:  python3 -m app.server --sdn-url http://127.0.0.1:8080

Every request is kept in ``FakeController.requests`` (method, path, body, time) so a test can check what arrived.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("off", "static", "dynamic")


class FakeController:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, mode: str = "dynamic") -> None:
        self.mode = mode
        self.priority_on = mode == "static"
        self.players = {}                       # ip -> {"port": ..., "since": ...}
        self.requests = []                      # (time, method, path, body)
        self.lock = threading.Lock()
        self.fail_next = 0                      # make the next N requests answer HTTP 503 (tests of retry / robustness)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # keep test output quiet
                pass

            def _reply(self, body, status=200):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    body = None
                with outer.lock:
                    outer.requests.append((time.time(), method, self.path, body))
                    if outer.fail_next > 0:
                        outer.fail_next -= 1
                        return self._reply({"error": "unavailable"}, 503)
                    status, reply = outer._route(method, self.path, body)
                self._reply(reply, status)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._thread = None

    # -------------------------------------------------------------- the API (same answers as game_qos.py)
    def _route(self, method, path, body):
        if method == "GET" and path == "/game/status":
            return 200, self.status()
        if body is None:
            return 400, {"error": "bad json"}
        if method == "POST" and path == "/game/register":
            if "ip" not in body:
                return 400, {"error": "send JSON {\"ip\": ..., \"port\": ...}"}
            self.players[str(body["ip"])] = {"port": int(body.get("port", 0)), "since": time.time()}
            return 200, {"ok": True, "priority_on": self.priority_on}
        if method == "POST" and path == "/game/unregister":
            if "ip" not in body:
                return 400, {"error": "send JSON {\"ip\": ...}"}
            return 200, {"ok": self.players.pop(str(body["ip"]), None) is not None}
        if method == "POST" and path == "/game/mode":
            if body.get("mode") not in MODES:
                return 400, {"error": "mode must be one of %s" % (MODES,)}
            self.mode = body["mode"]
            self.priority_on = self.mode == "static" or (self.mode == "dynamic" and self.priority_on)
            return 200, self.status()
        return 404, {"error": "not found"}

    def status(self):
        return {"mode": self.mode, "match": "registered", "game_port": 5005, "priority_on": self.priority_on,
                "link_mbps": 10.0, "util": {}, "players": sorted(self.players), "switches": [1, 2]}

    # -------------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def start(self) -> "FakeController":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="fake-controller", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def calls(self, path: str):
        """Bodies of the POSTs to ``path`` so far."""
        with self.lock:
            return [b for (_, m, p, b) in self.requests if m == "POST" and p == path]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fake Ryu REST API for testing without Mininet.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mode", choices=MODES, default="dynamic")
    args = ap.parse_args(argv)
    fc = FakeController(args.host, args.port, args.mode).start()
    print("fake controller on %s (mode %s); Ctrl+C stops it" % (fc.url, fc.mode), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
