#!/usr/bin/env python3
"""Deterministic 60% get / 40% put traces. Generate/decode outside the timed section."""

import argparse
import json
import random
import sys
from pathlib import Path


def operations(settings, worker):
    if not 0 <= worker < settings["threads"]:
        raise ValueError("worker must be in [0, threads)")
    if settings["operations_per_thread"] % 10:
        raise ValueError("operations_per_thread must be divisible by 10")
    rng = random.Random(settings["seed"] + worker)
    for _ in range(settings["operations_per_thread"] // 10):
        block = ["get"] * 6 + ["put"] * 4
        rng.shuffle(block)
        for op in block:
            row = {"op": op, "key": f'{rng.randrange(settings["keys"]):016x}'}
            if op == "put":
                row["value"] = rng.randrange(-(2**31), 2**31)
            yield row


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--settings", type=Path, required=True)
    ap.add_argument("--worker", type=int, required=True)
    args = ap.parse_args()
    settings = json.loads(args.settings.read_text())
    for row in operations(settings, args.worker):
        print(json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2)
