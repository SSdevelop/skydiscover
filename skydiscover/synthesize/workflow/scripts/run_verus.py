#!/usr/bin/env python3
"""Run one bounded proof attempt, or replay accepted evidence with --replay."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import suite  # installs the package fallback when workflow is distributed without spec/
from spec.proof import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(["replay" if args.replay else "check", args.run]))
