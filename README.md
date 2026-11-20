# extcontract — exact finite-type compatibility, adaptation, and revision-aware ingestion

A self-contained Python 3.11 library for the 745 external files. It implements three
deliberately separated concerns:

1. **Contract compatibility** (`check_compatibility`) — a *row-free* proof that every
   legal value of each writer column is exactly representable in its paired reader
   column. It emits per-column safe/unsafe reasons, a concrete witness for each
   unsafe pair, and a public adaptation plan when the whole pair is safe.
2. **File adaptation** (`adapt_rows` / `adapt_file_document`) — validates every row
   against the *declared* writer domain, then applies the actual plan to emit
   `reader-records/v1` for the supplied `materials/745/reader_client.py`.
3. **Stateful ingestion** (`IngestionLibrary`) — owns the target row state under a
   fixed reader contract, resolves row revisions, and accepts or rejects each file
   **atomically**.

The supplied `reader_client.py` is never imported or modified. It is exercised
as a real subprocess against our output in the tests and demo.

---

## Quick start

```bash
# row-free compatibility report (per-column reasons + plan)
PYTHONPATH=. python3.11 -m extcontract compat \
    materials/745/writer-contract.json materials/745/reader-contract.json

# adapt the supplied rows and read them with the real client
PYTHONPATH=. python3.11 - <<'PY'
import json
from extcontract import load_contract, adapt_rows, write_records
w = load_contract("materials/745/writer-contract.json", role="writer")
r = load_contract("materials/745/reader-contract.json", role="reader")
rows = json.load(open("materials/745/input.json"))["rows"]
write_records(adapt_rows(w, r, rows).records, "/tmp/records.json")
PY
python3.11 materials/745/reader_client.py \
    materials/745/reader-contract.json /tmp/records.json
# -> {"numeric_totals": {"amount": "-1"}, "row_count": 2}
```

Full mandated scenario (F-BAD / F-NEW, narrowed readers, INT16, DECIMAL, nullable):

```bash
PYTHONPATH=. python3.11 examples/run_745_demo.py
```

Tests (pytest):

```bash
PYTHONPATH=. python3.11 -m pytest tests/ -q
```

---

## Type model (`extcontract/types.py`)

| Kind | JSON transport (writer input / reader output) | Domain |
|------|----------------------------------------------|--------|
| `INT(bits)` | JSON integer / minimal rational string | `[-2**(bits-1), 2**(bits-1)-1]`, `bits ∈ {8,16,32,64}` |
| `DECIMAL(p,s)` | JSON **fraction string** / minimal rational string | `c / 10**s`, `|c| ≤ 10**p-1`, `1≤p≤18`, `0≤s≤p` |
| `TEXT` | JSON string / JSON string | any string; never coerced to a number |

All types carry `nullable`. Numeric values are held internally as exact
`int`/`Fraction`; nothing is ever rounded, truncated, clamped, or defaulted from
`null`.

* Reader output uses **minimal rational strings** (`Fraction.__str__`), e.g. `"1"`,
  `"-2"`, `"1/10"`, which is what `reader_client.py` validates with
  `str(Fraction(value)) == value`.
* Writer `DECIMAL` input must be a fraction string (integers/floats/exponents are
  rejected — exact transport, no inference).
* Schema `default` values may be an integer or exact fraction string; they are
  normalised and range-checked against the reader type.

---

## 1. Compatibility service (`extcontract/compat.py`)

Pure function of the two contracts — **no rows, no target `row_id`**:

```python
report = check_compatibility(writer, reader)
report.safe               # bool over the whole declared domains
report.columns[i].safe    # per reader column
report.columns[i].reasons # human-readable proof / counterexample
report.columns[i].witness # a legal writer value the reader cannot hold (unsafe)
report.adaptation_plan()  # executable plan when report.safe
```

### Safety rules (exact, no samples)

| Writer → Reader | Safe iff |
|-----------------|----------|
| nullable → non-nullable | **never** (null is a legal writer value, witness = null) |
| `INT(bw) → INT(br)` | `br ≥ bw` |
| `INT(bw) → DECIMAL(p,s)` | `2**(bw-1) * 10**s ≤ 10**p - 1` |
| `DECIMAL(pw,sw) → INT(br)` | `sw == 0` and `10**pw - 1 ≤ 2**(br-1) - 1` |
| `DECIMAL(pw,sw) → DECIMAL(pr,sr)` | `sr ≥ sw` and `(10**pw-1)·10**(sr-sw) ≤ 10**pr-1` |
| `TEXT → TEXT` | always |
| anything else | **never** (no implicit coercion; TEXT is never parsed as numeric) |

Examples the brief calls out, all covered by tests:

* `INT8 → DECIMAL(3,0)` **SAFE**; `INT8 → DECIMAL(2,0)` **UNSAFE** even though the
  samples `1, -2` fit — the witness is a legal INT8 value (e.g. `-128`) outside
  `[-99, 99]`.
* `INT16 → DECIMAL(3,0)` **UNSAFE** for the same reason.
* `DECIMAL(3,1) → DECIMAL(4,2)` **SAFE** (`1/10` converts exactly); lowering scale
  is **UNSAFE** (witness `1/10` at scale 0).
* nullable writer → non-nullable reader **UNSAFE**.

### Missing / extra columns

* A reader column is safe when the writer supplies it and the whole-domain proof
  holds, **or** when the writer omits it **and** the reader declares an explicit,
  in-domain `default`.
* A reader column absent from the writer with **no** default is **UNSAFE** —
  nullable does not imply a supplied value.
* Extra writer columns are listed under `ignored_writer_fields` (ignored).
* Pairing is **by `field_id` only**; a changed display `name` is noted but harmless.
* Duplicate `field_id` in a contract is rejected at parse time.
* A `default` never rescues a present-but-null writer value: if the writer declares
  the column, the row-level `null` passes through (legal only if both nullable).
  This is enforced both in the proof and at row adaptation.

### Contract revisions

`replace_field_type(contract, field_id=..., new_type_json=...)` models the
`reader_revision` / `writer_revision` operations in `input.json`; re-running
`check_compatibility` yields `INCOMPATIBLE` for both supplied revisions.

---

## 2. File adaptation (`extcontract/adapter.py`)

```python
result = adapt_rows(writer, reader, rows)          # -> AdaptationResult
result.status    # "ADAPTED" | "INCOMPATIBLE" | "INVALID"
result.records   # reader-records/v1 (only when ADAPTED)
result.errors    # per-row/field messages (never silently drops a row)
```

Pipeline, in order:

1. Check contract compatibility. If unsafe → `INCOMPATIBLE`, no records.
2. Parse each row envelope (`row_id`, positive-integer `revision` defaulting to `1`,
   `values` object).
3. Validate every value against the **declared writer domain** (range, scale,
   nullability, JSON encoding, unknown fields). All row errors are collected and the
   **whole file** is rejected; no partial output.
4. Apply the actual plan column-by-column:
   * `writer` column → exact conversion (`int_identity`, `int_widen`,
     `int_to_decimal_exact`, `decimal_identity`, `decimal_exact_rescale`,
     `decimal_to_int_exact`, `text_identity`) or `null_passthrough`;
   * missing column with default → `fill_default`;
   * guards re-assert reader-domain membership before encoding.
5. Emit `reader-records/v1`:

```json
{
  "format": "reader-records/v1",
  "reader_version": "r1",
  "rows": [
    {"row_id": "one", "revision": 1,
     "values": {"amount": "1", "status": "ok"},
     "sources": {
       "amount": {"field_id": "amount", "writer_field_id": "amount",
                  "writer_type": {"kind":"INT","bits":8},
                  "reader_type": {"kind":"DECIMAL","precision":3,"scale":0},
                  "conversion": "int_to_decimal_exact", "input": 1},
       "status": {"default": "ok", "conversion": "fill_default",
                  "reader_type": {"kind":"TEXT"}}}}
  ]
}
```

`sources` preserves the original field id, the conversion basis, the original
(writer) input value, and the default basis, satisfying the provenance requirement.

`adapt_file_document` additionally pins `writer_version`/`reader_version` declared by
the file to the fixed contracts.

---

## 3. Ingestion library (`extcontract/ingest.py`)
```python
library = IngestionLibrary(reader)                 # fixed reader contract
res = library.ingest(writer, file_document)        # atomic accept/reject
library.publish()                                  # reader-records/v1 snapshot
library.numeric_totals()                           # exact per-column totals
```

### Separation of concerns

`check_compatibility` (row-free) decides whether the *contract pair* is safe. Its
`SAFE` result is a **precondition**, never acceptance. The library then independently
validates rows, resolves revisions, and commits atomically. It never hard-codes a
target `row_id`.

### State model & canonical equality

Each row stores its **canonical reader value** (`int`/`Fraction`/`str`/`None`) plus
the adapted `encoded` values and `sources` provenance. Business identity is the
canonical value, so:

* different input representations of the same number (`"1"` vs `"2/2"` for
  `DECIMAL(_,0)`) do **not** create a conflict;
* changing a display `name` while keeping `field_id` changes nothing;
* `null` and the string `"null"` are distinct; `TEXT` values are never parsed to
  numbers.

### Revision rules (per file, per `row_id`)

* Missing `revision` ⇒ `1`; revisions must be positive integers.
* The **highest revision in the file wins**.
* **Every** occurrence is first validated for writer-domain legality — an illegal
  low-revision row rejects the file (it is not silently ignored).
* Lower revision than the stored one ⇒ validated, then **ignored**.
* Same revision + same canonical value ⇒ **replay** (no change).
* Same revision + different canonical value ⇒ **whole file rejected**, even if a
  higher revision for the same row also appears in the file.

### Atomicity

Validation, adaptation, revision resolution, and conflict detection happen entirely
on staged copies. State and receipt are published in one guarded assignment. A
rejected file leaves **no new rows and no receipt**.

### File identity / idempotency

Receipts are keyed by `(reader_version, file_id)` and store a canonical,
representation-independent fingerprint over the `(row_id, revision, values)` content
and contract versions:

* identical resubmission ⇒ original receipt, `"replay": true`;
* same `file_id`, different content ⇒ rejected.

### Reader-version isolation

Each `IngestionLibrary` is fixed to one reader contract; its rows live in an
isolated map keyed by `reader_version`. Distinct reader versions never rescan each
other's rows; cross-version ingress into a library is refused.

### Publication

`publish()` returns `reader-records/v1` from the **real target row slice**, retaining
each row's `revision`, adapted `values`, and `sources` provenance. It is directly
consumable by `reader_client.py`; totals recompute from the canonical values.

### Costs (the documented envelope: ≤20 columns, ≤5000 rows, ≤50 versions)

* Ingest: `O(m·k)` for validation/adaptation plus `O(m·k log m)` for sorting/
  fingerprinting (`m` rows, `k` columns); conflict checks are dict lookups.
* Publish: `O(n·k)` copy + `O(n log n)` sort. No versioned-view recomputation is
  needed because every accepted row already holds its adapted values and provenance.
* 50 reader versions are independent maps; ingesting under each does not touch the
  others.
* Measured in `tests/test_scale.py` (20 cols × 5000 rows, 50 versions): plan ≪ 1 ms,
  ingest ≈ 0.6 s, publish ≈ 0.17 s, all 50 versions ≈ 8 ms.

No arbitrary type inference, no Arrow Union, no online database migration.

---

## 4. Ordered-run catalog + archive merge work order (`runs.py`, `workorder.py`)

An independent entry point for the archive sorter. It consumes the *real*
adapter output (never re-derives it) and produces two independently checkable
artifacts: a **run catalog** and a **minimum-rewrite merge work order**.

### 4.1 Concatenate real adapter files, keep global positions

```python
from extcontract import (adapt_rows, concatenate_records, build_run_catalog,
                         plan_merges, execute_plan, verify_catalog, verify_sorted)

docs = [adapt_rows(w, r, rows_a).records, adapt_rows(w, r, rows_b).records]
records = concatenate_records(docs)          # rows in exact input order
catalog = build_run_catalog(records, r, "amount")   # numeric key field
```

`concatenate_records` stamps each row with a 0-based global `position` and
keeps the input order; duplicate `row_id`s are rejected (the supplied client
requires unique ids). The input order is what remembers "original global
position", so it is preserved through every later merge.

### 4.2 Exact-rational key, nulls last, stable

The key is one **numeric** reader field (a `TEXT` field is rejected). Values
are compared as exact `fractions.Fraction`, never as text — `-10 < -2` and
`9 < 10` because they are numbers, not strings. `null` sorts after every
number. Equal keys are tie-broken by original `position`, so the sort is stable:
`3, 2a, 2b, 1` merges to `1, 2a, 2b, 3` with `2a` before `2b`.

### 4.3 Natural-run segmentation

From the first unprocessed row: if the next key is *strictly* smaller, take the
longest strictly decreasing prefix and read it **reversed**; otherwise take the
longest non-decreasing prefix and read it forward. A trailing single row is its
own segment. Strictly decreasing may not step across an equal key — that is
exactly what keeps equal keys in order. Each segment records `start`, `end`,
`count`, `direction` (`forward`/`reverse`), and a machine-checkable `read`
instruction. `verify_catalog(records, catalog)` re-derives and checks the runs.

Original input `10,11,12,13,0,1,-4,-3,-10,-9,-8,-7` yields run sizes
**4, 2, 2, 4**. Changing only `-10 → -2` joins the third run to its
predecessor: **4, 2, 3, 3**.

### 4.4 Adjacent-only merges, minimum rewrites

`plan_merges(sizes, max_arity)` returns the cheapest schedule where only two or
three **adjacent** piles merge (adjacency = original run order, *not* key
order), each merge rewrites every row it touches, and `max_arity ∈ {2, 3}`.
Empty input or a single run costs 0.

Original input: 2-way minimum **24** rewritten rows, 3-way minimum **16**.
Modified input (`-10 → -2`): 2-way **24**, 3-way **17**.

The planner does **not** enumerate merge trees (there are exponentially many).
The interval substructure makes it a DP: the last merge over a range always
costs that range's row total, so

```
best[i][j] = W(i,j) + min( pair[i][j], triple[i][j] )
pair[i][j]   = min over split m   of best[i][m] + best[m+1][j]
triple[i][j] = min over split b   of pair[i][b] + best[b+1][j]
```

filled by increasing interval length. `execute_plan` really performs the
merges with a stable k-way heap merge (tie-break on `position`) and returns
`records` in `reader-records/v1` form, `executed_steps` (each step's inputs,
merged row count, and cost — the sum equals the reported cost), and
`ordered_positions`. `verify_sorted` re-checks the final stream.

Both machines produce the **same** ascending, stable row stream; the supplied
`reader_client.py` reads it. Run `examples/run_order_demo.py` (and
`python -m extcontract order WRITER READER VALUES.json KEY --arity 3
[--catalog OUT] [--sorted OUT]`).

### 4.5 Costs

* **Catalog** (`runs.py`): one scan — `O(m)` time, `O(m + r)` space for the key
  list and `r` run descriptors (`m` rows, `r` runs). No sort happens here.
* **Work order** (`workorder.py`): interval DP is `O(r³)` time and `O(r²)`
  space (the `pair` table is reused so the 3-way term stays cubic, not
  `O(r⁵)`). At the documented limit of **120 runs** this is ~1.7M inner
  comparisons; `plan_merges` rejects `> 120` runs with `LimitError` rather than
  silently degrading.
* **Execution**: `O(R log k)` per merge step over its `R` output rows for
  arity `k`; the total rewritten work equals the reported cost by construction.

---

## Templates for the mandated scenario

`fixtures/bootstrap.json`, `fixtures/F-BAD.json`, `fixtures/F-NEW.json` are the
exact documents from the brief:

1. Bootstrap `one=1@r1`, `two=-2@r1` → client amount total **-1**, `status="ok"`.
2. `F-BAD` (`one=2@r1` conflicts with accepted `1`; `three=1@r1`) → **whole file
   rejected**; `three` remains invisible; total stays **-1**.
3. `F-NEW` (`one=3@r2`, `two=4@r2`) → **accepted atomically**; published slice totals
   **7** and the real client reads it; the pre-`F-NEW` slice still totals **-1**.

---

## Package layout

```
extcontract/
  errors.py     exceptions (business rejections are results, not exceptions)
  types.py      INT/DECIMAL/TEXT domain model, exact encode/decode
  contract.py   writer/reader contract parsing, defaults, duplicate checks
  compat.py     row-free whole-domain proof + adaptation plan + witnesses
  adapter.py    per-row validation + plan application -> reader-records/v1
  ingest.py     revision-aware, atomic, idempotent target state
  runs.py       ordered-run catalog over the real adapted stream
  workorder.py  min-rewrite adjacent merge work order + execution
  cli.py        compat / adapt / ingest / order commands
examples/run_745_demo.py   end-to-end scenario against the real client
examples/run_order_demo.py ordered-run catalog + merge work order demo
fixtures/                  bootstrap, F-BAD, F-NEW, order-values documents
tests/                     compat, adapter, ingest, fixtures, ordering, scale
```
