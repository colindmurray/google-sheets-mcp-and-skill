# gsheets (MCP) — niche surface

The ~5% tier. Almost no task needs this. Reach here only when a specific sheet actually uses one of
these objects, or when a typed tool has told you it cannot do what you need. Read
`common-workflows.md` and `structure-and-rules.md` first.

---

## Opt-in per-cell reads

`sheets_inspect` has three additive reads that default off, cost nothing when unused, and attach
data **per cell only when that cell has it**.

### Rich text (`include_rich_text`)

One cell can hold several styled segments and several links — `"See A then B"` where `A` and `B`
point at different URLs. Every other read flattens that to one string. This is the **only** way to
recover a multi-link cell.

- Per-run styling lives under a nested `format` object on the run, **not** at the run's top level.
- **A run-level link takes precedence over the cell-level `hyperlink`.** For a multi-link cell the
  flat `hyperlink` is absent entirely (Google leaves it empty) — iterate the runs to recover every
  link. Reading only `hyperlink` silently loses links.
- Run offsets are `[start:end)`, end exclusive.
- **`hyperlink` is read-only.** You create an in-cell link by writing a `=HYPERLINK(...)` formula
  through `sheets_write_values`, never by writing the field back.
- Under `compact=True`, cells differing in runs or hyperlink never merge into one run.

### Pivot tables (`include_pivot`)

A pivot table's definition lives on its **anchor (top-left) cell only**. Turn this on when you find
a block of generated-looking data and need to know whether it is a pivot — chiefly so you do not
overwrite it. The object carries its own terse summary line plus the resolved source range, rows,
columns, values, and filters.

**Read-only.** There is no typed pivot write; see the escape hatch below.

### Smart chips (`chips`)

A person chip, a linked Drive file, a place, a finance ticker — all of them read back as **plain
text** everywhere else. The chip runs are the only structured signal that a cell is more than its
text. Turn this on when a column of names or links needs to be resolved to real identities.

Person chips carry an email; rich-link chips carry the authoritative URI and mime type, so a
mis-guessed `kind` never loses information. **Read-only** — chip writes go through the escape hatch.

---

## Structural objects

All of these are `sheets_structure` actions; their `params` shapes are in the tool's own
description. What follows is what the description cannot tell you.

### Native Tables

A native Table gives a range a name, a typed column schema, and a known end — which is what lets
`sheets_append_rows` find the bottom reliably.

- **A `DROPDOWN` column requires a `ONE_OF_LIST` validation** in its definition. Omitting it fails
  pre-flight. The shape is the same structured rule `sheets_set_validation` takes — the terse
  one-liner the read side emits is *not* accepted on write.
- **`SMART_CHIP` is rejected** as a column type because it has no single target — pick the specific
  chip type (`PEOPLE_CHIP`, `FILES_CHIP`, `FINANCE_CHIP`, `PLACE_CHIP`, `RATINGS_CHIP`). Legacy
  spellings `DATETIME`, `CHECKBOX`, and `RATING` still work as aliases.
- **Column types are author-declared and never validated against contents.** A column can report
  `DOUBLE` while holding text, carry no type at all, or be named `Column 23`. The read is faithful
  to what the author declared — so do not auto-generate a schema from it and trust it. Verify
  against actual cell values.
- Table dropdowns are invisible to `sheets_inspect` (they are a table property, not cell
  validation). Audit them here.
- On update, band colors are **not** atomic — only the ones you supply are masked, so unspecified
  band colors survive.

### Banding

Alternating-color ranges. Mostly useful as a *signal*: a banded rectangle is someone deliberately
saying "this is a table", which is a better guess at the data extent than eyeballing the grid.
Supply at least one of the row or column banding groups. Partial color updates mask down to the
single band you changed.

### Basic filter and filter views

A sheet has **one** basic filter (sort plus per-column hidden values) and any number of named,
non-destructive **filter views**.

- A per-column criterion has **only** `hidden` values and/or a `condition` — Google's
  `FilterCriteria` has no `visible`/`visibleValues`, and passing one is rejected. Express the
  inverse as `hidden`.
- The `condition` reuses the conditional-format grammar, but the type must be **filter-legal**:
  `ONE_OF_LIST` / `ONE_OF_RANGE` are data-validation-only and rejected here.
- **A filter hides rows.** Before writing anything row-addressed, confirm what is concealed — see
  `structure-and-rules.md`.

### Slicers

An on-grid filter control: it points at a data range, filters one column of it by 0-based offset,
and sits at a single anchor cell — usually on a *different* tab from the data it filters.

- The **anchor is required and immutable**. Update can change title, data range, column, and
  criteria; to move a slicer, delete and re-add it.
- Same `hidden`-only criteria rule as filter views.
- **Deleting a slicer maps to `deleteEmbeddedObject`** — slicers share the embedded-object id space
  with charts. Do not assume an id is a chart just because it looks like one.
- The anchor reads back as a `{sheet, row, col}` triple. Google **omits a zero index**, so an absent
  row or col means 0 — a top-left anchor still renders correctly rather than reading as missing.

### Spreadsheet properties

Title, locale, and timeZone — the write side of what `sheets_overview` reports. Locale and timeZone
matter more than they look: they govern how dates and numbers are interpreted on `USER_ENTERED`
writes. If dates are landing wrong, check them before debugging your values.

---

## Developer metadata

Durable key/value anchors on a row/column span, a whole sheet, or the spreadsheet. They survive row
and column inserts, which is the entire point — see `structure-and-rules.md` for the addressing
workflow they enable. Anchor forms are all-or-nothing: a dimension anchor needs sheet, dimension,
start, and end together; a sheet anchor needs just the sheet; a spreadsheet anchor needs nothing.

Capture the returned id on create — update and delete address by it. Delete is destructive.

---

## Charts

Create, update, delete, and read embedded charts, with a deliberate v1 limitation:

**`action="read"` returns metadata only** — id, title, type, anchor. Not the spec. So a
read-modify-write cycle through this tool will silently drop everything else about the chart. For
any real fidelity — axes, colors, the full series union — read and write through `sheets_batch`
instead. Say so plainly if a user asks you to tweak an existing chart's appearance.

---

## `sheets_batch` — the escape hatch

A raw ordered list of Sheets `batchUpdate` requests, passed straight through. Order is preserved
exactly; nothing is sorted or rewritten for you. You own correctness.

**Use it only when no typed tool covers the need.** The typed paths build field masks, capture ids,
resolve A1 to grid ranges, and order conditional-format mutations for you — all of which you forfeit
here. It is also unguarded: the destructive-operation confirmations built into the typed tools do
not exist on this path, so confirm with the user before running one.

**Capture `newIds`.** The API only surfaces newly created ids inside the replies array; the result
buckets them by kind (sheets, charts, named ranges, protected ranges, metadata, tables, banded
ranges, filter views, slicers). That is what keeps a create-then-populate flow inside a single
batch instead of a create, a read, and a second write.

### What genuinely has no typed tool

Reach for `sheets_batch` for these, and only these, before checking whether a typed tool has since
been added:

- **Pivot table writes** — pivots are readable via `sheets_inspect`, never writable typed.
- **Smart chip writes** — readable, not writable.
- **Full chart specs** — beyond title/type/anchor, per the gap above.
- **Connected Sheets / data sources** — batch-only in the API itself.

---

## See also

- `common-workflows.md` (~80%) — the everyday journeys.
- `structure-and-rules.md` (~15%) — conditional-format grammar and index safety, validation,
  structural reads and base mutations, dimensions, data operations, insert-proof addressing,
  comments, export.
