# Amortization math

## Periodic rate

Given an annual percentage rate `APR` (as a percentage) and a payment schedule:

- Monthly: `r = APR / 100 / 12`
- Biweekly: `r = APR / 100 / 26`

This is the same convention U.S. lenders use for the in-period interest calculation. (Some loans use daily simple interest; this skill does not model that — it uses the standard periodic-rate amortization formula.)

## Level payment

For a principal `P`, periodic rate `r`, and `n` remaining periods:

```
payment = P * r * (1 + r)^n / ((1 + r)^n - 1)
```

Special case: if `r == 0`, `payment = ceil(P / n)`.

The script computes this in floating-point, then **rounds to the nearest cent**. All subsequent arithmetic is on integer cents — no float drift.

## Per-period split

For each period:
1. `interest = round(balance * r)`
2. `principal_part = payment - interest`
3. `extra = recurring_extra (if eligible) + one_time_extra (if matched on this date)`
4. `balance -= (principal_part + extra)`

If `principal_part + extra >= balance`, this is the **final period**:
- `principal_part = balance`
- `extra = 0`
- `payment_this_period = principal_part + interest`
- `balance = 0`

This guarantees the schedule terminates with an exact zero balance and the last row's payment may be smaller (or, with extras, a clamped extra) than prior rows.

## Rate changes (ARM recasting)

When a `rate_changes` entry's effective date is reached:
1. Update `r` from the new APR.
2. **Recompute the level payment** using the *current outstanding balance* and *remaining periods*.

Example: a 30-year loan that resets at year 5 will recompute its payment for the remaining 25 years (300 monthly periods) using the new rate and the year-5 balance. This matches how standard ARMs recast.

## Why integer cents

Floating-point dollars accumulate rounding drift over hundreds of periods. A 360-month schedule that uses floats can show a final balance of `0.0034` or `-0.00012` depending on the FPU. Integer cents make the arithmetic exact and the CSV reconcile to zero on the dot.
