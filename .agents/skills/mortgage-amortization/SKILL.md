---
name: mortgage-amortization
description: Build a deterministic month-by-month or biweekly mortgage amortization schedule with optional extra payments and rate-change (ARM) support, and report total interest, payoff date, and savings versus a no-extras baseline. Use when the user asks to amortize a loan, compare payoff strategies, evaluate biweekly vs monthly, model an ARM, or compute total interest paid.
---

# mortgage-amortization

Generates a per-period amortization schedule (CSV) and a summary report (Markdown) from a JSON loan spec. Uses integer-cents arithmetic so totals reconcile to the penny across runs.

## When to use

- "How much interest will I pay on a $400k 30-year at 6.25%?"
- "If I add $200/month extra, how much sooner do I pay it off?"
- "Compare biweekly vs monthly for this loan."
- "Model an ARM that resets to 7.5% in year 5."
- "Build me an amortization table I can drop into a spreadsheet."

## When NOT to use

- The user wants tax/PITI/escrow modeling (insurance, property tax, PMI). This skill only models principal + interest.
- Negative-amortization, interest-only, or graduated-payment loans — not supported; refuse and say so.
- Investment-return comparisons (pay-down vs invest). Out of scope.
- Ad-hoc one-line questions like "what's 6% of 400k?" — just answer those directly.

## Inputs

A JSON loan-spec file:

```json
{
  "principal": 400000,
  "apr": 6.25,
  "term_years": 30,
  "start_date": "2026-05-01",
  "schedule": "monthly",
  "extra_payments": [
    {"type": "recurring", "amount": 200, "from": "2026-05-01"},
    {"type": "one_time", "amount": 5000, "on": "2027-01-01"}
  ],
  "rate_changes": [
    {"effective": "2031-05-01", "apr": 7.50}
  ]
}
```

Required: `principal`, `apr`, `term_years`, `start_date`. Defaults: `schedule="monthly"`, no extras, no rate changes.

If the user gives the loan in prose, build the JSON spec first and write it to disk — do not try to pass values inline.

## Output

- `<spec>.schedule.csv` — one row per period: `period,date,payment,interest,principal,extra,balance`. Always sums to zero balance on the last row.
- `<spec>.summary.md` — totals, payoff date, and (if extras are specified) a side-by-side savings comparison versus a no-extras baseline.
- The Markdown summary is also echoed to stdout for the agent to show the user.

## Step-by-step

1. Gather the loan parameters from the user. If anything required is missing, ask before running.
2. Write the spec to a JSON file (e.g. under `assets/` or `/tmp/`).
3. Run:
   ```
   python3 .agents/skills/mortgage-amortization/scripts/amortize.py <spec.json>
   ```
4. Show the user the summary. Point at the CSV path so they can open it in a spreadsheet.
5. For follow-up scenarios ("what if I do $300 instead of $200?"), edit the spec and re-run — do not try to recompute totals by hand.

## Limitations and checks

- **Principal + interest only.** No taxes, insurance, PMI, or escrow.
- **Rate changes** apply on or after their effective date. The payment is recomputed using the *remaining balance* and *remaining periods* — this matches standard ARM recasting.
- **Final-period rounding**: the script clamps the last payment so the balance lands exactly at zero. The last row's `payment` may differ slightly from prior rows.
- **Extra payments** that would overpay the loan are clamped; the script will not produce a negative balance.
- **Determinism**: same spec → byte-identical CSV and summary. Do not add timestamps or progress output to stdout.
- **Biweekly** = 26 periods/year, half the equivalent monthly payment in effect (since the formula uses the periodic rate). This is the "true biweekly" model, not "monthly with two half-payments."
