#!/usr/bin/env python3
"""Mortgage amortization builder. Integer-cents arithmetic; deterministic output.

Inputs: a JSON loan-spec file. Outputs: a per-period CSV plus a Markdown summary.

Loan spec schema (all amounts in dollars; APR as percent, e.g. 6.25):
{
  "principal": 400000,
  "apr": 6.25,
  "term_years": 30,
  "start_date": "2026-05-01",
  "schedule": "monthly" | "biweekly",
  "extra_payments": [
    {"type": "recurring", "amount": 200, "from": "2026-05-01"},
    {"type": "one_time", "amount": 5000, "on": "2027-01-01"}
  ],
  "rate_changes": [
    {"effective": "2031-05-01", "apr": 7.50}
  ]
}

Outputs:
  --csv PATH       per-period schedule (period, date, payment, interest,
                   principal, extra, balance)
  --summary PATH   markdown summary (totals, payoff date, interest saved
                   vs baseline if extras present)
By default both are written next to the spec file.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CENTS = 100


def to_cents(dollars) -> int:
    return int(round(float(dollars) * CENTS))


def from_cents(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // CENTS}.{c % CENTS:02d}"


def add_period(d: date, schedule: str, n: int = 1) -> date:
    if schedule == "biweekly":
        return date.fromordinal(d.toordinal() + 14 * n)
    if schedule == "monthly":
        for _ in range(n):
            y, m = d.year, d.month + 1
            if m > 12:
                y, m = y + 1, 1
            day = min(d.day, _days_in_month(y, m))
            d = date(y, m, day)
        return d
    raise ValueError(f"unknown schedule: {schedule}")


def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        return (date(y + 1, 1, 1) - date(y, 12, 1)).days
    return (date(y, m + 1, 1) - date(y, m, 1)).days


def periods_per_year(schedule: str) -> int:
    return 26 if schedule == "biweekly" else 12


def level_payment_cents(balance_c: int, periodic_rate: float, n_periods: int) -> int:
    """Standard amortization formula. Returns the rounded periodic payment in cents."""
    if n_periods <= 0:
        return balance_c
    if periodic_rate == 0:
        return -(-balance_c // n_periods)  # ceil division
    factor = (1 + periodic_rate) ** n_periods
    payment = balance_c * periodic_rate * factor / (factor - 1)
    return int(round(payment))


@dataclass
class LoanSpec:
    principal_c: int
    apr: float
    term_years: int
    start_date: date
    schedule: str
    extra_recurring_c: int
    extra_recurring_from: date | None
    extra_one_time: dict[date, int]
    rate_changes: list[tuple[date, float]]


def parse_spec(d: dict) -> LoanSpec:
    schedule = d.get("schedule", "monthly")
    if schedule not in ("monthly", "biweekly"):
        raise SystemExit(f"error: schedule must be 'monthly' or 'biweekly', got {schedule!r}")

    extra_recurring_c = 0
    extra_recurring_from: date | None = None
    extra_one_time: dict[date, int] = {}
    for ep in d.get("extra_payments", []):
        if ep["type"] == "recurring":
            extra_recurring_c = to_cents(ep["amount"])
            extra_recurring_from = date.fromisoformat(ep["from"])
        elif ep["type"] == "one_time":
            extra_one_time[date.fromisoformat(ep["on"])] = to_cents(ep["amount"])
        else:
            raise SystemExit(f"error: unknown extra_payment type: {ep['type']!r}")

    rate_changes = sorted(
        (date.fromisoformat(rc["effective"]), float(rc["apr"]))
        for rc in d.get("rate_changes", [])
    )

    return LoanSpec(
        principal_c=to_cents(d["principal"]),
        apr=float(d["apr"]),
        term_years=int(d["term_years"]),
        start_date=date.fromisoformat(d["start_date"]),
        schedule=schedule,
        extra_recurring_c=extra_recurring_c,
        extra_recurring_from=extra_recurring_from,
        extra_one_time=extra_one_time,
        rate_changes=rate_changes,
    )


@dataclass
class Row:
    period: int
    on: date
    payment_c: int
    interest_c: int
    principal_c: int
    extra_c: int
    balance_c: int


def amortize(spec: LoanSpec, ignore_extras: bool = False) -> list[Row]:
    """Run the amortization. Returns one row per period until the balance hits 0."""
    n_total = spec.term_years * periods_per_year(spec.schedule)
    apr = spec.apr
    rate = apr / 100 / periods_per_year(spec.schedule)
    balance = spec.principal_c
    payment = level_payment_cents(balance, rate, n_total)

    rate_changes = list(spec.rate_changes)
    rows: list[Row] = []
    on = spec.start_date

    for period in range(1, n_total + 1):
        on = add_period(spec.start_date, spec.schedule, period - 1) if period > 1 else spec.start_date
        # Apply rate change if its effective date is on or before this period.
        while rate_changes and rate_changes[0][0] <= on:
            _, new_apr = rate_changes.pop(0)
            apr = new_apr
            rate = apr / 100 / periods_per_year(spec.schedule)
            remaining = n_total - (period - 1)
            payment = level_payment_cents(balance, rate, remaining)

        interest = int(round(balance * rate))
        principal_pay = payment - interest

        extra = 0
        if not ignore_extras:
            if (
                spec.extra_recurring_c
                and spec.extra_recurring_from
                and on >= spec.extra_recurring_from
            ):
                extra += spec.extra_recurring_c
            if on in spec.extra_one_time:
                extra += spec.extra_one_time[on]

        # Don't overpay — clamp principal+extra to remaining balance.
        total_principal = principal_pay + extra
        if total_principal >= balance:
            # Final payment: pay exactly the balance + interest, split appropriately.
            principal_pay = balance
            extra = 0
            payment_this = principal_pay + interest
            balance = 0
            rows.append(Row(period, on, payment_this, interest, principal_pay, 0, 0))
            break

        balance -= total_principal
        rows.append(Row(period, on, payment, interest, principal_pay, extra, balance))

    return rows


def write_csv(rows: list[Row], path: Path) -> None:
    lines = ["period,date,payment,interest,principal,extra,balance"]
    for r in rows:
        lines.append(
            f"{r.period},{r.on.isoformat()},{from_cents(r.payment_c)},"
            f"{from_cents(r.interest_c)},{from_cents(r.principal_c)},"
            f"{from_cents(r.extra_c)},{from_cents(r.balance_c)}"
        )
    path.write_text("\n".join(lines) + "\n")


def summarize(spec: LoanSpec, rows: list[Row], baseline: list[Row] | None) -> str:
    total_paid = sum(r.payment_c + r.extra_c for r in rows)
    total_interest = sum(r.interest_c for r in rows)
    total_extra = sum(r.extra_c for r in rows)
    payoff = rows[-1].on
    n_periods = len(rows)
    years = n_periods / periods_per_year(spec.schedule)

    lines = [
        f"# Mortgage amortization summary",
        "",
        f"- Principal: ${from_cents(spec.principal_c)}",
        f"- Initial APR: {spec.apr:.4f}%",
        f"- Term (scheduled): {spec.term_years} years ({spec.term_years * periods_per_year(spec.schedule)} {spec.schedule} periods)",
        f"- Start date: {spec.start_date.isoformat()}",
        f"- Schedule: {spec.schedule}",
    ]
    if spec.rate_changes:
        lines.append("- Rate changes:")
        for d, apr in spec.rate_changes:
            lines.append(f"  - {d.isoformat()} → {apr:.4f}%")
    lines.extend(
        [
            "",
            "## Results",
            f"- Periods to payoff: {n_periods} ({years:.2f} years)",
            f"- Payoff date: {payoff.isoformat()}",
            f"- Total paid: ${from_cents(total_paid)}",
            f"- Total interest: ${from_cents(total_interest)}",
            f"- Total extra payments: ${from_cents(total_extra)}",
        ]
    )
    if baseline is not None:
        base_total = sum(r.payment_c + r.extra_c for r in baseline)
        base_interest = sum(r.interest_c for r in baseline)
        base_payoff = baseline[-1].on
        lines.extend(
            [
                "",
                "## Versus baseline (no extra payments)",
                f"- Baseline payoff: {base_payoff.isoformat()}",
                f"- Baseline total interest: ${from_cents(base_interest)}",
                f"- Interest saved: ${from_cents(base_interest - total_interest)}",
                f"- Periods saved: {len(baseline) - n_periods}",
                f"- Total cost saved: ${from_cents(base_total - total_paid)}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a mortgage amortization schedule.")
    ap.add_argument("spec", type=Path, help="path to JSON loan spec")
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--summary", type=Path, default=None)
    args = ap.parse_args()

    if not args.spec.is_file():
        print(f"error: spec not found: {args.spec}", file=sys.stderr)
        return 2

    spec_d = json.loads(args.spec.read_text())
    spec = parse_spec(spec_d)

    rows = amortize(spec)
    has_extras = spec.extra_recurring_c or spec.extra_one_time
    baseline = amortize(spec, ignore_extras=True) if has_extras else None

    csv_path = args.csv or args.spec.with_suffix(".schedule.csv")
    summary_path = args.summary or args.spec.with_suffix(".summary.md")
    write_csv(rows, csv_path)
    summary_path.write_text(summarize(spec, rows, baseline))

    print(f"wrote {csv_path}", file=sys.stderr)
    print(f"wrote {summary_path}", file=sys.stderr)
    sys.stdout.write(summarize(spec, rows, baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
