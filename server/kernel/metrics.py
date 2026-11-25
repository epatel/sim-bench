"""Metric primitives: counters, sample statistics, time-weighted gauges."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

RESERVOIR_SIZE = 4096


@dataclass
class Counter:
    value: float = 0.0

    def add(self, n: float = 1.0) -> None:
        self.value += n

    def summary(self, now: float) -> dict:
        return {"kind": "counter", "value": self.value}


@dataclass
class Samples:
    """Running mean/std/min/max plus a reservoir for percentiles."""

    rng: random.Random
    count: int = 0
    mean: float = 0.0
    _m2: float = 0.0
    min: float = math.inf
    max: float = -math.inf
    reservoir: list[float] = field(default_factory=list)

    def add(self, x: float) -> None:
        self.count += 1
        delta = x - self.mean
        self.mean += delta / self.count
        self._m2 += delta * (x - self.mean)
        self.min = min(self.min, x)
        self.max = max(self.max, x)
        if len(self.reservoir) < RESERVOIR_SIZE:
            self.reservoir.append(x)
        else:
            j = self.rng.randrange(self.count)
            if j < RESERVOIR_SIZE:
                self.reservoir[j] = x

    def percentile(self, p: float) -> float:
        if not self.reservoir:
            return math.nan
        s = sorted(self.reservoir)
        return s[min(len(s) - 1, int(p / 100 * len(s)))]

    def summary(self, now: float) -> dict:
        if not self.count:
            return {"kind": "samples", "count": 0}
        std = math.sqrt(self._m2 / (self.count - 1)) if self.count > 1 else 0.0
        return {
            "kind": "samples",
            "count": self.count,
            "mean": self.mean,
            "std": std,
            "min": self.min,
            "max": self.max,
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
        }


@dataclass
class Gauge:
    """A level that changes over time; reports its time-weighted average."""

    value: float = 0.0
    start: float = 0.0
    _last_t: float = 0.0
    _area: float = 0.0
    peak: float = 0.0

    def set(self, value: float, now: float) -> None:
        self._area += self.value * (now - self._last_t)
        self._last_t = now
        self.value = value
        self.peak = max(self.peak, value)

    def add(self, delta: float, now: float) -> None:
        self.set(self.value + delta, now)

    def time_average(self, now: float) -> float:
        span = now - self.start
        if span <= 0:
            return self.value
        return (self._area + self.value * (now - self._last_t)) / span

    def summary(self, now: float) -> dict:
        return {
            "kind": "gauge",
            "value": self.value,
            "avg": self.time_average(now),
            "peak": self.peak,
        }
