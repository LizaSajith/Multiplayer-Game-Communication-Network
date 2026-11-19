"""sdn_client.py - the game server's connection to the SDN controller (Role 3).

The Ryu controller (``controller/game_qos.py``, Role 2) offers a small REST API.  This module is the *agreed
interface* between the game and that controller:

    game server           sdn_client.SdnClient                 Ryu controller
    -----------           --------------------                 --------------
    player JOINs   --->   register(ip, port)      --->   POST /game/register    (flow rules for that player)
    player leaves  --->   unregister(ip)          --->   POST /game/unregister
    server stops   --->   unregister_all()        --->   (the controller is left clean for the next run)
    experiments    --->   set_mode("dynamic")     --->   POST /game/mode        (off | static | dynamic)
                          get_status()            --->   GET  /game/status

Design rules (they matter for the 60 Hz game loop and for the report):

* ``register`` / ``unregister`` never block the game.  They put a job on a queue; one background thread makes
  the HTTP calls, retries a failed call a few times and then gives up (the game keeps running; the controller
  can still prioritise with ``GAME_MATCH=port``).
* Several players can share one IP address (the bots of one Mininet host do).  The controller keys on the IP, so
  the client counts references: the controller is told on the first player of an IP and on the last one.
* Every call is written to a CSV (``--sdn-log``): epoch time, action, ip, ok, latency.  That file is the
  evidence that *application events* (a player joining) reach the SDN controller, and it is the time stamp
  the analysis uses to compare "player registered" with "priority flows installed".
* Standard library only, and it ignores ``http_proxy`` variables (a proxy must never sit between a Mininet host
  and the controller).

Command line (needs a reachable controller)::

    python3 -m app.sdn_client --url http://127.0.0.1:8080 status
    python3 -m app.sdn_client --url http://127.0.0.1:8080 mode dynamic
    python3 -m app.sdn_client register 10.0.0.2 --port 40000
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

DEFAULT_URL = "http://10.0.0.254:8080"      # how a Mininet host reaches the controller (topo/game_topo.py)
MODES = ("off", "static", "dynamic")
CALL_COLUMNS = ("label", "t_epoch", "action", "ip", "port", "ok", "latency_ms", "attempt", "detail")


class SdnError(Exception):
    """The controller could not be reached or refused the request."""


class SdnClient:
    """Thin, non-blocking client for the controller's REST API (see the module docstring)."""

    def __init__(self, base_url: str = DEFAULT_URL, *, timeout: float = 2.0, retries: int = 3,
                 retry_delay: float = 0.5, log_path: Optional[str] = None, label: str = "",
                 opener=None, clock: Callable[[], float] = time.time) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout, self.retries, self.retry_delay = timeout, max(1, retries), retry_delay
        self.label, self.clock = label, clock
        # ProxyHandler({}) = never use http_proxy: the controller is on the local virtual network
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._refs: Dict[str, int] = {}                 # ip -> players currently behind that address
        self._ports: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._jobs: "queue.Queue" = queue.Queue()
        self.counters = {"registered": 0, "unregistered": 0, "failed": 0, "calls": 0}
        self._log_lock = threading.Lock()
        self._log_file = None
        self._log = None
        if log_path:
            folder = os.path.dirname(log_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            new = not os.path.exists(log_path) or os.path.getsize(log_path) == 0
            self._log_file = open(log_path, "a", newline="", encoding="utf-8")
            self._log = csv.writer(self._log_file)
            if new:
                self._log.writerow(CALL_COLUMNS)
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="sdn-client", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ plain HTTP
    def _http(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise SdnError("HTTP %d from %s%s" % (exc.code, self.base_url, path)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SdnError("cannot reach %s%s (%s)" % (self.base_url, path, getattr(exc, "reason", exc))) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise SdnError("controller sent something that is not JSON: %r" % raw[:80]) from exc

    # ------------------------------------------------------------------ blocking helpers (scripts, CLI)
    def get_status(self) -> dict:
        """GET /game/status: mode, priority_on, util, players, switches."""
        return self._http("GET", "/game/status")

    def set_mode(self, mode: str) -> dict:
        """POST /game/mode: ``off`` (baseline), ``static`` (always priority) or ``dynamic`` (on while congested)."""
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % (MODES,))
        return self._http("POST", "/game/mode", {"mode": mode})

    def reachable(self) -> bool:
        try:
            self.get_status()
            return True
        except SdnError:
            return False

    # ------------------------------------------------------------------ non-blocking (used by the game server)
    def register(self, ip: str, port: int = 0) -> None:
        """A player behind ``ip`` joined: ask the controller to prioritise its game flows."""
        with self._lock:
            self._refs[ip] = self._refs.get(ip, 0) + 1
            first = self._refs[ip] == 1
            self._ports[ip] = int(port)
        if first:
            self._jobs.put(("register", ip, int(port)))

    def unregister(self, ip: str) -> None:
        """A player behind ``ip`` left; the controller hears about it when the last one is gone."""
        with self._lock:
            n = self._refs.get(ip, 0)
            if n <= 0:
                return
            self._refs[ip] = n - 1
            last = n - 1 == 0
            if last:
                del self._refs[ip]
        if last:
            self._jobs.put(("unregister", ip, 0))

    def unregister_all(self) -> None:
        """Forget every registered address (server shutdown): the controller is left without stale players."""
        with self._lock:
            ips = list(self._refs)
            self._refs.clear()
        for ip in ips:
            self._jobs.put(("unregister", ip, 0))

    def registered_ips(self):
        with self._lock:
            return sorted(self._refs)

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                self._do(*job)
            except Exception as exc:                    # the worker must never die: the game depends on its queue
                self.counters["failed"] += 1
                self._write("error", "", 0, False, 0.0, 0, str(exc))
            finally:
                self._jobs.task_done()

    def _do(self, action: str, ip: str, port: int) -> None:
        path = "/game/register" if action == "register" else "/game/unregister"
        body = {"ip": ip, "port": port} if action == "register" else {"ip": ip}
        for attempt in range(1, self.retries + 1):
            t0 = time.perf_counter()
            try:
                self.counters["calls"] += 1
                reply = self._http("POST", path, body)
                ms = (time.perf_counter() - t0) * 1000.0
                self.counters["registered" if action == "register" else "unregistered"] += 1
                self._write(action, ip, port, True, ms, attempt, json.dumps(reply, sort_keys=True))
                return
            except SdnError as exc:
                ms = (time.perf_counter() - t0) * 1000.0
                self._write(action, ip, port, False, ms, attempt, str(exc))
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
        self.counters["failed"] += 1

    def _write(self, action: str, ip: str, port: int, ok: bool, ms: float, attempt: int, detail: str) -> None:
        if self._log is None:
            return
        with self._log_lock:
            self._log.writerow([self.label, "%.3f" % self.clock(), action, ip, port, int(ok), "%.2f" % ms,
                                attempt, detail])
            self._log_file.flush()

    # ------------------------------------------------------------------ shutdown
    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued call was made (or failed).  True when the queue is empty."""
        end = time.monotonic() + timeout
        while self._jobs.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)
        return not self._jobs.unfinished_tasks

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush(timeout)
        self._jobs.put(None)
        self._worker.join(timeout=1.0)
        if self._log_file is not None:
            with self._log_lock:
                self._log_file.close()
                self._log_file = self._log = None


# ---------------------------------------------------------------------- command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m app.sdn_client",
                                 description="Talk to the SDN controller's REST API (Role 3 interface).")
    ap.add_argument("--url", default=DEFAULT_URL, help="controller REST address (default %s)" % DEFAULT_URL)
    ap.add_argument("--timeout", type=float, default=3.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print the controller status as JSON")
    m = sub.add_parser("mode", help="set the controller mode")
    m.add_argument("mode", choices=MODES)
    r = sub.add_parser("register", help="register a player address")
    r.add_argument("ip")
    r.add_argument("--port", type=int, default=0)
    u = sub.add_parser("unregister", help="unregister a player address")
    u.add_argument("ip")
    args = ap.parse_args(argv)

    client = SdnClient(args.url, timeout=args.timeout, retries=1)
    try:
        if args.cmd == "status":
            out = client.get_status()
        elif args.cmd == "mode":
            out = client.set_mode(args.mode)
        elif args.cmd == "register":
            out = client._http("POST", "/game/register", {"ip": args.ip, "port": args.port})
        else:
            out = client._http("POST", "/game/unregister", {"ip": args.ip})
    except SdnError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        client.close(timeout=0.5)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
