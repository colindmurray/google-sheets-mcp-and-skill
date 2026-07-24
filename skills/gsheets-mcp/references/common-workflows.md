# gsheets (MCP) — common workflows

The ~80% tier: complete tool sequences for the journeys that come up constantly. Each tool's own
description remains the authority on its arguments and return shape — what follows is the **order**,
the **decision points**, and the **stopping conditions**.

A note that applies to every journey below: stop as soon as you can answer the question. The most
common waste is climbing the whole read ladder when `sheets_overview` plus one `sheets_describe`
already settled it.

---

## J1 — Understand an unfamiliar spreadsheet

The user pastes a URL and asks what it is, or asks you to work in a sheet you have never seen.

```
1. sheets_overview
     → tab names, sizes, frozen panes, CF/protected COUNTS, named ranges, locale, theme map
     Decide: which tab matters? does any tab have a high conditionalFormatCount (→ J3)?

2. sheets_describe   on the interesting tab's plausible data range
     → per-column type / density / formulaRatio / distinct sample, NO cells
     Decide: which columns are formula-heavy (→ J2)? which are the real data (→ J4)?

3. sheets_inspect    on the narrow range that actually matters
     → values + formulas + both formats + validation + merges

4. (only if the sheet is structurally interesting)
   sheets_structure  action="read"
     → merges, named/protected ranges, groups, tables, filters, filter views, banding, slicers
```

**Decision points.**

- A high `conditionalFormatCount` in step 1 means the colours carry meaning — the sheet is
  communicating through formatting, and a value-only read will miss the point. Go to J3.
- `formulaRatio` near 1.0 on a column in step 2 means it is derived, not entered. If several
  columns look like that across a wide grid, use `sheets_formula_patterns` instead of `inspect` —
  one template beats fifty identical formulas.
- Merges showing up in step 2 usually mean a header block or a visual layout, not a data table.
  Expect the "real" data to start below them.
- Skip step 3 entirely when the user only asked what the workbook contains.

**Across several files at once**, replace steps 1–2 with `sheets_read_many` using `mode="summary"`
— a per-file overview in one call, and a bad ID comes back as a per-file error entry instead of
failing the batch. Check each result's own `ok`; a top-level success does not mean every file
worked.

---

## J2 — Explain what a region computes

The user wants the logic, not the numbers: "what does this tab actually do?"

```
1. sheets_describe            → find the formula-heavy columns
2. Fork on width:
     narrow (a few columns)   → sheets_read_values  render="all"
                                  formula and computed value side by side
     wide (a whole tracker)   → sheets_formula_patterns
                                  per-column templates with row spans + a sample value
3. sheets_inspect             only on the handful of cells whose exact formula matters
```

**Why the fork matters.** A tracker is usually one formula repeated down every row.
`sheets_formula_patterns` collapses that to a template like `=SUM(J{r}:R{r})  rows 3:52  (50)`,
which is the answer to "what does this compute" in one line. A raw formula dump of the same region
is fifty near-identical strings and often blows the context budget for no added information.

**Trust boundary on patterns.** Normalization is best-effort and honest about it: absolute (`$`)
rows stay verbatim, and a column whose formulas do not reduce cleanly comes back verbatim with
`reduced:false`. When you need the lossless ground truth, `sheets_read_values` with
`render="formula"` is it.

**Do not use `render="unformatted"` here.** It returns the typed result and hides whether a cell was
formula-driven at all — exactly the fact you are trying to establish.

---

## J3 — Trace why a cell looks the way it does

"Why is this row red?" is a two-source question, and both sources are needed.

```
1. sheets_inspect                     on the cell/region
     → effectiveFormat  = what RENDERS (conditional formatting already applied)
       userEnteredFormat = what an author SET (static intent)

2. sheets_read_conditional_formats    scoped with `range` to the same rectangle
     → the rules intersecting it, each with its priority index (0 = highest)

3. If a colour reads as `theme:NAME`:
   sheets_overview  → resolve it through the `theme` map
```

**The diagnosis.** `effectiveFormat` differing from `userEnteredFormat` means a conditional-format
rule is doing it. They agreeing means the colour was set statically and no rule is involved. That
single comparison is the whole answer, and it is why reading only values can never explain a
coloured sheet.

**Scope the rule read.** Passing `range` returns only rules that *intersect* that rectangle while
preserving each rule's original sheet-level index — so an index you learn here still addresses the
right rule if you go on to edit it. `sheet` and `range` are alternatives; a range already carries
its own sheet.

**Reading a rule.** Rules come back as terse round-trippable lines:

```
[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
```

The full grammar — and how to author one — is in `structure-and-rules.md`.

---

## J4 — Extract a dataset for analysis

The user wants the numbers out, to compute over them.

```
Small enough to reason about in place (a few dozen rows):
   sheets_read_values   output_format="markdown"   → read it directly

Large, and you will process rather than read it:
   sheets_read_values   output_format="csv"  out_path="./data.csv"
   → returns a small handle + preview; process the file, never the payload

Whole-tab dump:
   sheets_export        format="csv"  sheet="<tab>"
   → straight to a local file, no token ceiling at all
```

**Choose by what happens next, not by size alone.** If a human or you will read the rows, use
`markdown`. If code will read them, use `csv` to a file. `csv`/`tsv` need a rectangular value read —
they are a clean `format_unsupported` error on a structured read, not a silent fallback.

**The formulas caveat.** CSV cannot carry formulas. The standard pattern for a sheet that has both
is two reads: `sheets_export` (or a wide `sheets_read_values`) for the values, plus a **narrow**
`sheets_read_values` with `render="formula"` over just the formula columns.

**Guard the read.** Set `max_cells` sized to the range *rectangle*, so an over-large read fails fast
with `result_too_large` instead of returning something that only breaks at your token limit.

**Sparse vs dense shapes read differently.** Sparse formula/note data reads best address-keyed
(`C5: =SUM(...)`, one line per non-empty cell — what `render="formula"` produces). Dense numeric
grids read best as a rectangle plus a range anchor (csv/json).

---

## J5 — Edit values safely

```
1. sheets_read_values  (or sheets_inspect)  on the target
     → confirm what is there before overwriting it
2. sheets_write_values
     → USER_ENTERED by default: "=SUM(B:B)" lands as a live formula,
       "5" / "$10" / "50%" coerce to number / currency / percent
3. sheets_read_values  render="all"
     → verify the formula went in live, not as literal text
```

**`sheets_write_values` overwrites.** That is the whole risk. Read first whenever the range might
hold data, and confirm with the user before overwriting anything populated. If the goal is to *add*
data rather than replace it, you want J6.

**When to pass `input="raw"`.** Only when you genuinely want inert text — a literal `=SUM(A:A)`
stored as a string, or a value that must not be coerced. It is the exception; `USER_ENTERED` is
almost always right.

**When A1 may have moved.** If rows could have been inserted since you last looked — or if this is a
block you will come back to — use `sheets_write_values_by_data_filter` with a developer-metadata
selector instead of a literal range. See `structure-and-rules.md`.

---

## J6 — Add rows to a growing table

```
1. sheets_structure  action="read"      → does the range have a native Table? where does it end?
                                          is a filter hiding rows?
2. sheets_append_rows                   → inserts AFTER the last populated row; never overwrites
3. sheets_read_values                   → confirm via the returned updatedRange
```

`sheets_append_rows` is the safe way to add data — it detects the table's extent and inserts below
it. Prefer it over computing "the next empty row" yourself and writing there, which races with any
other editor and breaks silently under an active filter.

**Do not retry a failed append blindly.** It is not idempotent; a retried append can duplicate rows.
Pass `retry={"preset": "off"}` and, if it fails, read the range to see what actually landed before
trying again.

---

## J7 — Restyle a region

```
1. sheets_inspect        → the current userEnteredFormat (so you write the minimum)
2. sheets_format         → only the keys you pass are written
3. sheets_inspect        → confirm
```

**Partial updates are safe by construction.** The field mask is auto-built from your payload, so
setting a background leaves bold, borders, and number format untouched. There is no need to re-send
the whole format to change one part of it — and doing so risks clobbering something you did not
read first.

**Cell format and borders in one call are atomic** — they go out as a single batch, so the operation
is all-or-nothing.

**Static vs conditional.** `sheets_format` sets a colour that is always there. If the colour should
depend on the data, that is `sheets_set_conditional_format` instead (see `structure-and-rules.md`) —
and note the two are visible separately in a later read as `userEnteredFormat` vs `effectiveFormat`.

**Laying a sheet out from scratch** also means column widths and row heights: `sheets_dimensions`
with a `set_props` `runs` list sets many spans — mixing rows and columns — in one call.

---

## J8 — Survey several spreadsheets

```
sheets_read_many   mode="summary"     → a per-file overview, one call, no grid data
sheets_read_many   mode="values"      → per-request ranges across files, one call
```

This exists because of the quota: one call across N files costs far less of the shared read-RPM
bucket than N calls. Reach for it whenever the task says "these spreadsheets" rather than "this
spreadsheet".

**Failures are per-file.** A bad ID, a permission problem, or a bad range becomes an error entry in
that file's slot while the rest still read. A top-level success therefore does **not** mean every
file succeeded — check each result's own `ok` before you summarize.

For a large fan-out, pair it with `out_path` so the combined payload lands in a file rather than
your context.

---

## See also

- `structure-and-rules.md` (~15%) — the conditional-format grammar, index-safe rule edits,
  validation, structural reads and mutations, dimensions, data operations, insert-proof addressing,
  comments, export.
- `niche.md` (~5%) — rich-text/pivot/chip reads, tables, banding, filter views, slicers, metadata,
  charts, and the `sheets_batch` escape hatch.
