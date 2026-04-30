# Detection rules

## Hidden-text detection

A run/span/element of text is treated as hidden if **any** of the following holds:

| Format | Hidden-text signal                                                          |
|--------|-----------------------------------------------------------------------------|
| PDF    | Glyph fill color luminance ≥ 0.95 (near-white on assumed white page)         |
| PDF    | Glyph font size < 1.0 pt                                                     |
| DOCX   | Run has `font.hidden = true`                                                 |
| DOCX   | Run color luminance ≥ 0.95                                                   |
| DOCX   | Run font size < 1.0 pt                                                       |
| HTML   | Inline style contains `display:none`, `visibility:hidden`, `color:#fff`/`white`, or `font-size:0` |

Luminance is computed with the standard sRGB coefficients:
```
L = 0.2126·R + 0.7152·G + 0.0722·B   (R,G,B in [0,1])
```

Hidden runs are:
1. **Removed** from the extracted `.txt` so the scoring LLM never sees them.
2. **Captured** (truncated to 200 chars, max 10 excerpts) in `meta.hidden_text_excerpts` so a human reviewer can audit what was hidden.
3. **Also scanned** for injection phrases (so a hidden `"ignore previous instructions"` triggers `possible_prompt_injection`).

## Injection-phrase patterns

Case-insensitive regexes run over visible text + hidden excerpts:

- `ignore … instructions/prompts/rules/system prompt`
- `disregard … instructions/prompts/rules/system prompt/rubric`
- `you are now [role] assistant/scorer/recruiter`
- `override … instructions/system prompt/rules`
- `give/assign/return … highest/maximum/perfect/top … score`
- `score … (a) 5`
- `as (an|the) ai language model`
- `system: you …`

The matched phrase (verbatim) lands in `meta.injection_phrases`.

## Resume-shape heuristics

A file is treated as resume-shaped only if:
- `char_count >= 300` AND `word_count >= 60`, AND
- It contains at least one of these section headers (case-insensitive substring): `experience`, `education`, `skills`, `employment`, `work history`, `summary`, `objective`, `projects`, `certifications`, `qualifications`.

If it has exactly one, the `only_one_resume_section` flag is added (informational; does not change the action by itself).

## Action decision

```
if extraction error or unsupported type:
    action = refuse
elif too_short or no resume sections:
    action = refuse
elif hidden_text_detected or possible_prompt_injection:
    action = review
else:
    action = extract
```

`review` always wins over `extract` when adversarial signals are present, even if the resume looks fine otherwise — because the *whole point* is that the visible content can look fine while a hidden payload influences the scorer.

## Why this happens at the extraction layer

The hidden-text attack works by exploiting the gap between what a human sees (white-on-white = nothing) and what a naive text extractor produces (the full string, including the attack). Once the attack reaches the scoring LLM's context, regex over the prompt is too late — the model has already read it.

By splitting visible vs hidden at parse time, the skill ensures:
- The scorer sees only what a human reviewer would see.
- The hidden content is preserved off-band for governance/audit.
- A naïve downstream pipeline that only reads `.txt` files automatically gets the safer behavior.
