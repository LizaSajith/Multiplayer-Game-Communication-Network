"""local.py - a private game on this computer: a server plus autopilot opponents, all inside one program.

Used by ``python -m app.client --local`` so the game can be tried alone, with no second window and no network.
It needs no pygame.  The server listens on 127.0.0.1 only; for other computers run ``python -m app.server``.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

try:
    from .game import GameConfig
    from .netclient import run_bots
    from .server import GameServer
except ImportError:                     # script use
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.game import GameConfig
    from app.netclient import run_bots
    from app.server import GameServer


class LocalGame:
    """The server runs in a thread; the opponents (``bots`` autopilots, race after race) in another.

    The bots join ``join_delay`` seconds after ``start()`` so that the human, who connects at once, is the
    first player and therefore the host (the host starts the race)."""

    def __init__(self, laps: int = 3, bots: int = 3, skill: float = 0.85, port: int = 0, countdown: float = 3.0,
                 join_delay: float = 1.0) -> None:
        cfg = GameConfig(laps=laps, countdown_s=countdown)
        self.server = GameServer(cfg, "127.0.0.1", port, verbose=False)      # ValueError / OSError reach the caller
        self.bots, self.skill, self.join_delay = bots, skill, join_delay
        self.stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        return self.server.address

    def start(self) -> None:
        self.server.start_thread()
        if self.bots > 0:
            self._thread = threading.Thread(target=self._play, name="local-bots", daemon=True)
            self._thread.start()

    def _play(self) -> None:
        if self.stop.wait(self.join_delay):
            return
        try:
            run_bots(self.address, self.bots, self.skill, verbose=False, keep_going=True, stop=self.stop)
        except OSError:
            pass

    def close(self) -> None:
        self.stop.set()
        if self._thread is not None:
            self._thread.join(3.0)
        self.server.stop()
        self.server.close()
