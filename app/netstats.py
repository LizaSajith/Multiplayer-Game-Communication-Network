"""netstats.py - small measurement helpers shared by the server, the clients and the bots.

* :class:`Jitter`     RFC 3550 interarrival jitter from the header ``ts`` field.
* :class:`RateMeter`  packets / kbit per second over the interval since the last read.
* :func:`percentile`  linear-interpolated percentile (used for the 95th-percentile RTT).
* :func:`describe`    mean / min / max / p50 / p95 of a list of samples.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .protocol import ts_diff


class Jitter:
    """RFC 3550 interarrival jitter estimate, in milliseconds.

    Feed it every packet in arrival order with the sender's ``ts`` (ms) and the
    local arrival time (s).  Only differences are used, so the two clocks do not
    have to be synchronised.  ``J += (|D| - J) / 16`` with
    ``D = (arrival_j - arrival_i) - (ts_j - ts_i)``.
    """

    def __init__(self) -> None:
        self.value = 0.0
        self._prev: Optional[Tuple[int, float]] = None

    def update(self, ts_ms: int, arrival_s: float) -> float:
        arrival_ms = arrival_s * 1000.0
        if self._prev is not None:
            prev_ts, prev_arrival = self._prev
            d = (arrival_ms - prev_arrival) - ts_diff(ts_ms, prev_ts)
            self.value += (abs(d) - self.value) / 16.0
        self._prev = (ts_ms, arrival_ms)
        return self.value


class RateMeter:
    """Counts packets and bytes; :meth:`take` returns the rates since the last take."""

    def __init__(self, now: float) -> None:
        self.t0 = now
        self.count = 0
        self.bytes = 0

    def add(self, nbytes: int = 0) -> None:
        self.count += 1
        self.bytes += nbytes

    def take(self, now: float) -> Tuple[float, float]:
        """Return ``(packets_per_second, kilobits_per_second)`` and start a new interval."""
        dt = max(1e-6, now - self.t0)
        rates = (self.count / dt, self.bytes * 8.0 / 1000.0 / dt)
        self.t0, self.count, self.bytes = now, 0, 0
        return rates


def percentile(values: Iterable[float], pct: float) -> float:
    """``pct`` in 0..100; linear interpolation between the two nearest samples."""
    data: List[float] = sorted(values)
    if not data:
        return 0.0
    if len(data) == 1:
        return float(data[0])
    rank = max(0.0, min(100.0, pct)) / 100.0 * (len(data) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


def describe(values: Iterable[float]) -> Dict[str, float]:
    data = list(values)
    if not data:
        return {"n": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}
    return {"n": len(data), "mean": sum(data) / len(data), "min": min(data), "max": max(data),
            "p50": percentile(data, 50.0), "p95": percentile(data, 95.0)}
