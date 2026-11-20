"""End-to-end demonstration of the ordered-run catalog and merge work order.

Run with:

    PYTHONPATH=. python3.11 examples/run_order_demo.py

It uses the fixed 745 contracts and drives the *real* ``reader_client.py`` on
the merged output to prove the archive contract:

  1. Input ``10,11,12,13,0,1,-4,-3,-10,-9,-8,-7`` segments as four runs of
     sizes ``4,2,2,4``; a 2-way machine rewrites 24 rows, a 3-way machine 16.
  2. Changing only ``-10`` to ``-2`` joins that run to its predecessor; the new
     segmentation and minimum costs are shown.
  3. Both machines really produce the same ascending row stream, which the
     supplied reader client accepts.

The "original input" arms (1) exercise concatenating two real adapted adapter
files; the ordered stream preserves their original global positions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "745"
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT))

from extcontract import (  # noqa: E402
    adapt_rows,
    build_run_catalog,
    concatenate_records,
    execute_plan,
    load_contract,
    plan_merges,
    verify_catalog,
    verify_sorted,
    write_records,
)

OUT = ROOT / "examples" / "out"


def client(contract_path, records_path):
    proc = subprocess.run(
        [sys.executable, str(MATERIALS / "reader_client.py"), str(contract_path), str(records_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"reader_client failed: {proc.stderr}")
    return json.loads(proc.stdout)


def adapt(rows, writer, reader):
    result = adapt_rows(writer, reader, rows)
    assert result.status == "ADAPTED", result.errors
    return result.records


def split_into_files(rows):
    """Two genuine adapter files, concatenated in input order."""
    return rows[:6], rows[6:]


def banner(n, title):
    print(f"\n=== {n}. {title} ===")


def run_machine(records, catalog, arity, name):
    counts = [s.count for s in catalog.segments]
    plan = plan_merges(counts, arity)
    result = execute_plan(records, catalog, plan)
    problems = verify_sorted(result.records, catalog)
    assert not problems, problems
    path = OUT / f"{name}-sorted.json"
    write_records(result.records, path)
    totals = client(MATERIALS / "reader-contract.json", path)
    print(f"  {arity}-way: min rewrite = {plan.total_cost} rows, "
          f"{len(plan.steps)} merge steps")
    for step in result.executed_steps:
        print(f"    step {step['step']}: merge "
              f"{[i['runs'] for i in step['inputs']]} -> {step['merged_rows']} rows")
    print(f"  reader_client({name}): {totals}")
    return plan, result


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    writer = load_contract(MATERIALS / "writer-contract.json", role="writer")
    reader = load_contract(MATERIALS / "reader-contract.json", role="reader")

    original = json.loads((FIXTURES / "order-values.json").read_text())["rows"]
    modified = json.loads((FIXTURES / "order-values-modified.json").read_text())["rows"]

    # ---- 1. original input -------------------------------------------------
    banner(1, "Original: 10,11,12,13,0,1,-4,-3,-10,-9,-8,-7 -> runs 4,2,2,4")
    first, second = split_into_files(original)
    records = concatenate_records([adapt(first, writer, reader), adapt(second, writer, reader)])
    print("  positions:", [r["position"] for r in records["rows"]])
    catalog = build_run_catalog(records, reader, "amount")
    assert [s.count for s in catalog.segments] == [4, 2, 2, 4]
    assert verify_catalog(records, catalog) == []
    for seg in catalog.segments:
        print(f"  run {seg.segment_id}: positions {seg.start}..{seg.end} "
              f"count={seg.count} read={seg.direction}")
    result_two, _ = run_machine(records, catalog, 2, "original-2way")
    result_three, _ = run_machine(records, catalog, 3, "original-3way")
    assert result_two.total_cost == 24
    assert result_three.total_cost == 16

    # Both machines must yield the SAME ordered rows.
    same_two = client(MATERIALS / "reader-contract.json", OUT / "original-2way-sorted.json")
    same_three = client(MATERIALS / "reader-contract.json", OUT / "original-3way-sorted.json")
    assert same_two == same_three
    two_rows = json.loads((OUT / "original-2way-sorted.json").read_text())["rows"]
    three_rows = json.loads((OUT / "original-3way-sorted.json").read_text())["rows"]
    assert [(r["row_id"], r["values"]["amount"]) for r in two_rows] == [
        (r["row_id"], r["values"]["amount"]) for r in three_rows
    ]
    print("  both machines produced the same ordered rows:", same_two)

    # ---- 2. change -10 -> -2 ----------------------------------------------
    banner(2, "Modified: only -10 -> -2 joins the third run to its predecessor")
    records_m = concatenate_records([adapt(modified, writer, reader)])
    catalog_m = build_run_catalog(records_m, reader, "amount")
    counts_m = [s.count for s in catalog_m.segments]
    print("  new run sizes:", counts_m)
    plan_m2 = plan_merges(counts_m, 2)
    plan_m3 = plan_merges(counts_m, 3)
    print(f"  2-way min rewrite = {plan_m2.total_cost} rows; "
          f"3-way min rewrite = {plan_m3.total_cost} rows")
    run_machine(records_m, catalog_m, 2, "modified-2way")
    run_machine(records_m, catalog_m, 3, "modified-3way")

    banner(3, "Both machines give the same ascending, stable row stream")
    print("  sorted amounts:", [r["values"]["amount"] for r in two_rows])
    print("\nAll order-demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
