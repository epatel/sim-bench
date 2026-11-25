"""Deterministic random streams and distribution sampling.

Each named stream (one per entity, link, ...) is seeded from the run seed and the
stream name, so adding an entity never changes the numbers another entity draws.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Any


class RngPool:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._streams: dict[str, random.Random] = {}

    def stream(self, name: str) -> random.Random:
        rng = self._streams.get(name)
        if rng is None:
            digest = hashlib.sha256(f"{self.seed}:{name}".encode()).digest()
            rng = random.Random(int.from_bytes(digest[:8], "big"))
            self._streams[name] = rng
        return rng


def sample(spec: Any, rng: random.Random) -> float:
    """Draw from a distribution spec.

    A spec is a number (constant) or a dict with a ``dist`` key:
    ``const`` (value), ``exp`` (mean), ``uniform`` (low, high),
    ``normal`` (mean, std; clamped at 0), ``lognormal`` (mean, std of the
    resulting values), ``choice`` (values, optional weights).
    """
    if spec is None:
        return 0.0
    if isinstance(spec, (int, float)):
        return float(spec)
    dist = spec.get("dist", "const")
    if dist == "const":
        return float(spec["value"])
    if dist == "exp":
        return rng.expovariate(1.0 / float(spec["mean"]))
    if dist == "uniform":
        return rng.uniform(float(spec["low"]), float(spec["high"]))
    if dist == "normal":
        return max(0.0, rng.gauss(float(spec["mean"]), float(spec["std"])))
    if dist == "lognormal":
        mean, std = float(spec["mean"]), float(spec["std"])
        sigma2 = math.log(1 + (std / mean) ** 2)
        return rng.lognormvariate(math.log(mean) - sigma2 / 2, math.sqrt(sigma2))
    if dist == "choice":
        return float(rng.choices(spec["values"], weights=spec.get("weights"))[0])
    raise ValueError(f"unknown distribution {dist!r}")


def mean_of(spec: Any) -> float:
    """Analytical mean of a spec (used for validation and summaries)."""
    if spec is None:
        return 0.0
    if isinstance(spec, (int, float)):
        return float(spec)
    dist = spec.get("dist", "const")
    if dist == "const":
        return float(spec["value"])
    if dist in ("exp", "normal", "lognormal"):
        return float(spec["mean"])
    if dist == "uniform":
        return (float(spec["low"]) + float(spec["high"])) / 2
    if dist == "choice":
        vals, w = spec["values"], spec.get("weights") or [1] * len(spec["values"])
        return sum(v * x for v, x in zip(vals, w, strict=True)) / sum(w)
    raise ValueError(f"unknown distribution {dist!r}")
