#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--instance-ids", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    wanted = {
        line.strip() for line in args.instance_ids.read_text().splitlines()
        if line.strip()
    }
    selected = []
    for line in args.predictions.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["instance_id"] in wanted:
            selected.append(row)
    found = {row["instance_id"] for row in selected}
    missing = sorted(wanted - found)
    if missing:
        raise SystemExit(f"Missing predictions: {missing}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row) + "\n" for row in selected), encoding="utf-8"
    )
    print(f"selected={len(selected)}")


if __name__ == "__main__":
    main()
