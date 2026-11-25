"""Headless runs: ``python -m server.kernel MODEL.yaml --until 1000 --seed 1``."""

from __future__ import annotations

import argparse
import json
import sys

from .model import load_model
from .sim import ModelError, Simulation


def _fmt(v):
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def print_table(summary: dict) -> None:
    print(
        f"model={summary['model']} seed={summary['seed']} t={summary['time']:.4g} "
        f"events={summary['events']} pending={summary['pending']}"
    )
    for section in ("entities", "links"):
        print(f"\n[{section}]")
        for owner, metrics in summary[section].items():
            print(f"  {owner}")
            for name, m in metrics.items():
                if not isinstance(m, dict):
                    print(f"    {name:<14} {_fmt(m)}")
                    continue
                body = " ".join(f"{k}={_fmt(v)}" for k, v in m.items() if k != "kind")
                print(f"    {name:<14} {body}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.kernel", description=__doc__)
    ap.add_argument("model", help="model YAML file")
    ap.add_argument("--until", type=float, default=1000.0, help="simulated end time")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-events", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    try:
        sim = Simulation(load_model(args.model), seed=args.seed)
    except (ModelError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    sim.run(until=args.until, max_events=args.max_events)
    summary = sim.summary()
    if args.json:
        json.dump(summary, sys.stdout, indent=2, default=str)
        print()
    else:
        print_table(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
