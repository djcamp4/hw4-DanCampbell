# mortgage-amortization skill

## What it does

Given a JSON loan spec (principal, APR, term, start date, optional extra payments and rate changes), it produces:

1. A per-period CSV schedule with payment / interest / principal / extra / running balance.
2. A Markdown summary with totals, payoff date, and — when extra payments are specified — a side-by-side comparison against a no-extras baseline showing interest saved and time saved.

Same input → byte-identical output every run. Final balance always reconciles to exactly `$0.00`.

## Why I chose this task

A mortgage amortization is the textbook case where prose math fails. To answer "how much interest do I save with $200/month extra on a $400k 30-year at 6.25%?", you need to:

- Compute the level payment from the standard amortization formula.
- Step through ~286 periods with per-period interest, principal, and balance updates.
- Apply a recurring extra and a one-time extra at the right dates.
- Run a parallel baseline schedule and diff the totals.
- Avoid floating-point drift across hundreds of compounding steps.

A model can describe the formula but cannot execute it accurately over a 360-row schedule. The script does the arithmetic in integer cents; the model decides which scenarios to model and explains the results.

I picked this over the other options (subtitle resyncer, flaky-test detector, license auditor) because the model/script split is unambiguous and the output is something a real person actually wants — a spreadsheet they can open and trust.

## How to use

Write a JSON loan spec, then run the script:

```bash
python3 .agents/skills/mortgage-amortization/scripts/amortize.py path/to/spec.json
```

The script writes `<spec>.schedule.csv` and `<spec>.summary.md` next to the spec, and echoes the summary to stdout.

Three example specs are included under [`assets/`](assets/):
- [`example_30yr_fixed.json`](assets/example_30yr_fixed.json) — vanilla 30-year fixed.
- [`example_with_extras.json`](assets/example_with_extras.json) — same loan + $200/month + a $5k lump in year 2.
- [`example_arm.json`](assets/example_arm.json) — 5/1 ARM-style rate change to 7.5% at year 5.

In an agent, the typical flow is: user describes a loan in prose → agent activates the skill from its description → agent writes the spec JSON → runs the script → shows the user the summary and points at the CSV.

## What the script does

[`scripts/amortize.py`](scripts/amortize.py) does the parts that prose alone cannot:

1. **Parses the spec** — JSON in, typed dataclass out, with validation.
2. **Computes the level payment** using the standard formula `P·r·(1+r)^n / ((1+r)^n − 1)`, in integer cents.
3. **Steps the schedule period-by-period**, splitting payment into interest and principal, applying extra payments by date, and advancing the calendar correctly (true 14-day biweekly, real-month monthly with day-of-month preservation).
4. **Recasts on rate changes** — when a rate-change effective date is reached, recomputes the level payment using the remaining balance and remaining periods (standard ARM behavior).
5. **Clamps the final period** so the balance lands exactly at `0.00`.
6. **Runs a parallel baseline** when extras are present and computes interest/time saved.
7. **Emits deterministic CSV + Markdown** to stdout/disk; all diagnostics go to stderr.

Math notes are in [`references/math.md`](references/math.md).

## What worked well

- **Integer cents end-to-end** eliminated all the rounding drift I'd expect from a 360-step float loop. Final balance reconciles to `0.00` on every example.
- **Clamping the last payment** (instead of letting it land at `−$0.04` or `$0.03`) makes the CSV trustworthy as a spreadsheet input.
- **Side-by-side baseline comparison** is the part users actually want — "this saves you $120,198 and 6.2 years" — and falls out naturally from re-running the same engine with `ignore_extras=True`.
- **The model/script split landed cleanly**: in testing, the agent took prose ("400k, 6.25, 30 years, biweekly with $100 extra"), generated the JSON, ran the script, and explained the result without trying to do the math itself.

## Limitations

- **Principal + interest only** — no taxes, insurance, PMI, or escrow. A real monthly mortgage payment is higher than what this models. The skill says so up front.
- **No daily simple interest** — uses the standard periodic-rate convention. For loans that compound daily (some HELOCs, some auto loans), totals will be off by small amounts.
- **No negative-amortization, interest-only, or graduated-payment loans.** The script does the right thing only for level-payment amortizing loans.
- **Rate-change recasting assumes payment recomputation.** Some ARMs cap the payment change rather than fully recasting; that's not modeled.
- **Calendar arithmetic preserves day-of-month** when possible but clamps to month-end (Jan 31 → Feb 28). For schedules sensitive to "first business day" rules, this is approximate.
- **Currency is implicit dollars.** No multi-currency or rounding-rule selection (banker's vs half-up).
