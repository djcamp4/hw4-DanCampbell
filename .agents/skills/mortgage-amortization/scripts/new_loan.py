#!/usr/bin/env python3
"""Interactive loan-spec builder.

Prompts the user for the fields needed to run amortize.py, validates each,
writes a JSON spec to disk, and (unless --no-run) runs the amortizer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AMORTIZE = SCRIPT_DIR / "amortize.py"


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        print("  (required)")


def prompt_float(label: str, default: float | None = None, minimum: float | None = None) -> float:
    while True:
        raw = prompt(label, None if default is None else str(default))
        try:
            v = float(raw)
        except ValueError:
            print(f"  not a number: {raw!r}")
            continue
        if minimum is not None and v < minimum:
            print(f"  must be >= {minimum}")
            continue
        return v


def prompt_int(label: str, default: int | None = None, minimum: int | None = None) -> int:
    while True:
        raw = prompt(label, None if default is None else str(default))
        try:
            v = int(raw)
        except ValueError:
            print(f"  not an integer: {raw!r}")
            continue
        if minimum is not None and v < minimum:
            print(f"  must be >= {minimum}")
            continue
        return v


def prompt_date(label: str, default: str | None = None) -> str:
    while True:
        raw = prompt(label, default)
        try:
            date.fromisoformat(raw)
            return raw
        except ValueError:
            print(f"  expected YYYY-MM-DD, got {raw!r}")


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    pretty = "/".join(choices)
    while True:
        raw = prompt(f"{label} ({pretty})", default).lower()
        if raw in choices:
            return raw
        print(f"  must be one of: {pretty}")


def prompt_yes_no(label: str, default: bool = False) -> bool:
    d = "y" if default else "n"
    return prompt_choice(label, ["y", "n"], d) == "y"


def first_of_next_month(today: date) -> str:
    y, m = today.year, today.month + 1
    if m > 12:
        y, m = y + 1, 1
    return date(y, m, 1).isoformat()


def build_spec() -> dict:
    print("Enter loan parameters. Press Enter to accept the default in [brackets].")
    print()

    principal = prompt_float("Principal (USD)", minimum=0.01)
    apr = prompt_float("APR (annual %, e.g. 6.25)", minimum=0)
    term_years = prompt_int("Term (years)", default=30, minimum=1)
    start_date = prompt_date("Start date", default=first_of_next_month(date.today()))
    schedule = prompt_choice("Schedule", ["monthly", "biweekly"], "monthly")

    spec: dict = {
        "principal": principal,
        "apr": apr,
        "term_years": term_years,
        "start_date": start_date,
        "schedule": schedule,
    }

    extras: list[dict] = []
    if prompt_yes_no("Add a recurring extra payment?", default=False):
        amount = prompt_float("  Recurring extra amount (USD/period)", minimum=0.01)
        starts = prompt_date("  Starts on", default=start_date)
        extras.append({"type": "recurring", "amount": amount, "from": starts})

    while prompt_yes_no("Add a one-time extra payment?", default=False):
        amount = prompt_float("  One-time amount (USD)", minimum=0.01)
        on = prompt_date("  On date")
        extras.append({"type": "one_time", "amount": amount, "on": on})

    if extras:
        spec["extra_payments"] = extras

    rate_changes: list[dict] = []
    while prompt_yes_no("Add a rate change (ARM)?", default=False):
        effective = prompt_date("  Effective on")
        new_apr = prompt_float("  New APR (%)", minimum=0)
        rate_changes.append({"effective": effective, "apr": new_apr})

    if rate_changes:
        spec["rate_changes"] = rate_changes

    return spec


def main() -> int:
    ap = argparse.ArgumentParser(description="Interactively build a loan spec and run the amortizer.")
    ap.add_argument("--out", type=Path, default=Path("loan_spec.json"),
                    help="path to write the JSON spec (default: ./loan_spec.json)")
    ap.add_argument("--no-run", action="store_true", help="write the spec but don't run amortize.py")
    args = ap.parse_args()

    try:
        spec = build_spec()
    except (KeyboardInterrupt, EOFError):
        print("\naborted", file=sys.stderr)
        return 130

    args.out.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"\nwrote {args.out}", file=sys.stderr)

    if args.no_run:
        return 0

    return subprocess.call([sys.executable, str(AMORTIZE), str(args.out)])


if __name__ == "__main__":
    sys.exit(main())
