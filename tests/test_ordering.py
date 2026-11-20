"""Tests for the ordered-run catalog and the adjacent-merge work order.

Covers the archive contract:

  * natural-run segmentation (ascending = non-decreasing, descending = strictly
    decreasing so equal keys never reorder),
  * exact-rational key comparison with nulls last and stable equal keys,
  * interval-DP minimum rewrite cost for 2-way and 3-way machines,
  * optimality against exhaustive search on small inputs,
  * real execution producing stable sorted ``reader-records/v1`` and agreeing
    across both arities, checked with the supplied ``reader_client.py``,
  * the 120-run scale envelope.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from extcontract import (
    adapt_rows,
    build_run_catalog,
    concatenate_records,
    execute_plan,
    load_contract,
    parse_contract,
    plan_merges,
    verify_catalog,
    verify_sorted,
    write_records,
)

MATERIALS = Path("materials/745")
CLIENT = MATERIALS / "reader_client.py"

ORIGINAL = [10, 11, 12, 13, 0, 1, -4, -3, -10, -9, -8, -7]
MODIFIED = [10, 11, 12, 13, 0, 1, -4, -3, -2, -9, -8, -7]


def _contracts():
    writer = load_contract(MATERIALS / "writer-contract.json", role="writer")
    reader = load_contract(MATERIALS / "reader-contract.json", role="reader")
    return writer, reader


def _catalog(values, split=None, key_field="amount"):
    writer, reader = _contracts()
    rows = [
        {"row_id": f"r{i:02d}", "values": {"amount": v}}
        for i, v in enumerate(values)
    ]
    if split is None:
        docs = [adapt_rows(writer, reader, rows).records]
    else:
        docs = [
            adapt_rows(writer, reader, rows[:split]).records,
            adapt_rows(writer, reader, rows[split:]).records,
        ]
    records = concatenate_records(docs)
    return records, build_run_catalog(records, reader, key_field)


def _catalog_typed(keys_raw, writer_type, reader_type, key_field="k"):
    writer = parse_contract(
        {"version": "w", "fields": [{"field_id": key_field, "name": key_field,
                                     "type": writer_type, "nullable": True}]},
        role="writer",
    )
    reader = parse_contract(
        {"version": "r", "fields": [{"field_id": key_field, "name": key_field,
                                     "type": reader_type, "nullable": True}]},
        role="reader",
    )
    rows = [{"row_id": f"r{i:02d}", "values": {key_field: v}}
            for i, v in enumerate(keys_raw)]
    result = adapt_rows(writer, reader, rows)
    assert result.status == "ADAPTED", result.errors
    records = concatenate_records([result.records])
    return records, build_run_catalog(records, reader, key_field)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def test_original_run_sizes_4_2_2_4():
    records, catalog = _catalog(ORIGINAL, split=6)
    assert [r.count for r in catalog.runs] == [4, 2, 2, 4]
    assert verify_catalog(records, catalog) == []


def test_modified_run_sizes_4_2_3_3():
    _, catalog = _catalog(MODIFIED)
    assert [r.count for r in catalog.runs] == [4, 2, 3, 3]


def test_strict_decrease_does_not_cross_equal_keys():
    # 3, 2a, 2b, 1 -> two strictly decreasing runs; 2a stays before 2b.
    records, catalog = _catalog_typed(
        [3, 2, 2, 1],
        {"kind": "INT", "bits": 8},
        {"kind": "DECIMAL", "precision": 3, "scale": 0},
    )
    assert [r.count for r in catalog.runs] == [2, 2]
    assert all(r.direction == "reverse" for r in catalog.runs)
    assert verify_catalog(records, catalog) == []
    plan = plan_merges([r.count for r in catalog.runs], 2)
    result = execute_plan(records, catalog, plan)
    assert result.ordered_positions == [3, 1, 2, 0]
    assert [r["values"]["k"] for r in result.records["rows"]] == ["1", "2", "2", "3"]
    assert verify_sorted(result.records, catalog) == []


def test_empty_input_pipeline_is_free():
    records, catalog = _catalog([])
    assert records["rows"] == []
    assert list(catalog.runs) == []
    for arity in (2, 3):
        plan = plan_merges([r.count for r in catalog.runs], arity)
        assert plan.total_cost == 0
        result = execute_plan(records, catalog, plan)
        assert result.records["rows"] == []
        assert result.ordered_positions == []


def test_trailing_single_row_is_a_segment():
    # 2,3,5 is non-decreasing; the trailing 4 < 5 starts (and ends) a new run.
    _, catalog = _catalog_typed(
        [2, 3, 5, 4],
        {"kind": "INT", "bits": 8},
        {"kind": "DECIMAL", "precision": 3, "scale": 0},
    )
    assert [r.count for r in catalog.runs] == [3, 1]


# ---------------------------------------------------------------------------
# Key semantics: exact rationals, nulls last, stable equal keys
# ---------------------------------------------------------------------------

def test_exact_rational_not_text_order():
    # "10" as text sorts before "9"; as numbers 9 < 10. Also 1/10 < 1/2.
    records, catalog = _catalog_typed(
        ["10", "9", "1/10", "1/2"],
        {"kind": "DECIMAL", "precision": 3, "scale": 1},
        {"kind": "DECIMAL", "precision": 4, "scale": 2},
    )
    plan = plan_merges([r.count for r in catalog.runs], 2)
    result = execute_plan(records, catalog, plan)
    got = [r["values"]["k"] for r in result.records["rows"]]
    assert got == ["1/10", "1/2", "9", "10"]


def test_nulls_sort_after_numbers():
    records, catalog = _catalog_typed(
        [5, None, 1, None, -2],
        {"kind": "INT", "bits": 8},
        {"kind": "DECIMAL", "precision": 3, "scale": 0},
    )
    plan = plan_merges([r.count for r in catalog.runs], 3)
    result = execute_plan(records, catalog, plan)
    got = [r["values"]["k"] for r in result.records["rows"]]
    assert got == ["-2", "1", "5", None, None]
    assert verify_sorted(result.records, catalog) == []


def test_equal_keys_keep_original_order_when_merged():
    # Keys 2,1,2,2,0 force several runs; the three 2s must stay in original
    # (position) order after merging, for both arities.
    records, catalog = _catalog_typed(
        [2, 1, 2, 2, 0],
        {"kind": "INT", "bits": 8},
        {"kind": "DECIMAL", "precision": 3, "scale": 0},
    )
    for arity in (2, 3):
        plan = plan_merges([r.count for r in catalog.runs], arity)
        result = execute_plan(records, catalog, plan)
        assert verify_sorted(result.records, catalog) == []
        two_positions = [
            row["position"] for row in result.records["rows"]
            if row["values"]["k"] == "2"
        ]
        assert two_positions == sorted(two_positions)
        assert len(two_positions) == 3


# ---------------------------------------------------------------------------
# Minimum-rewrite costs
# ---------------------------------------------------------------------------

def test_original_minimum_costs_24_and_16():
    _, catalog = _catalog(ORIGINAL)
    sizes = [r.count for r in catalog.runs]
    assert plan_merges(sizes, 2).total_cost == 24
    assert plan_merges(sizes, 3).total_cost == 16


def test_modified_minimum_costs():
    _, catalog = _catalog(MODIFIED)
    sizes = [r.count for r in catalog.runs]
    # 2-way stays 24; 3-way improves to 17 for the new 4,2,3,3 split.
    assert plan_merges(sizes, 2).total_cost == 24
    assert plan_merges(sizes, 3).total_cost == 17


def test_empty_and_single_segment_cost_zero():
    assert plan_merges([], 2).total_cost == 0
    assert list(plan_merges([], 3).steps) == []
    assert plan_merges([7], 2).total_cost == 0
    assert plan_merges([7], 3).total_cost == 0
    assert list(plan_merges([7], 3).steps) == []


def test_plan_is_recomputable_and_adjacent():
    sizes = [4, 2, 2, 4]
    for arity in (2, 3):
        plan = plan_merges(sizes, arity)
        assert plan.total_cost == sum(s.cost for s in plan.steps)
        for step in plan.steps:
            # inputs are contiguous and mutually adjacent
            for (a0, a1), (b0, b1) in zip(step.input_ranges, step.input_ranges[1:]):
                assert b0 == a1 + 1
            assert step.output_range == (
                step.input_ranges[0][0], step.input_ranges[-1][1]
            )
            assert step.cost == sum(step.input_sizes) == step.output_size
            assert 2 <= len(step.input_ranges) <= arity


def _brute(counts, arity):
    @lru_cache(maxsize=None)
    def solve(piles):
        if len(piles) == 1:
            return 0
        best = None
        for size in range(2, arity + 1):
            for i in range(len(piles) - size + 1):
                merged = sum(piles[i : i + size])
                nxt = piles[:i] + (merged,) + piles[i + size :]
                cand = merged + solve(nxt)
                if best is None or cand < best:
                    best = cand
        return best

    return solve(tuple(counts))


@pytest.mark.parametrize("arity", [2, 3])
def test_dp_matches_exhaustive_search(arity):
    rng = random.Random(1234)
    for n in range(1, 7):
        for _ in range(120):
            counts = tuple(rng.randint(1, 5) for _ in range(n))
            assert plan_merges(counts, arity).total_cost == _brute(counts, arity)


def test_three_way_never_worse_than_two_way():
    rng = random.Random(99)
    for _ in range(200):
        counts = [rng.randint(1, 9) for _ in range(rng.randint(1, 8))]
        assert plan_merges(counts, 3).total_cost <= plan_merges(counts, 2).total_cost


# ---------------------------------------------------------------------------
# Real execution -> stable sorted reader-records/v1, identical across arities
# ---------------------------------------------------------------------------

def _run_client(contract_path, records_path):
    proc = subprocess.run(
        [sys.executable, str(CLIENT), str(contract_path), str(records_path)],
        capture_output=True,
        text=True,
    )
    return proc


def test_both_arities_produce_identical_sorted_rows(tmp_path):
    records, catalog = _catalog(ORIGINAL, split=6)
    finals = {}
    for arity in (2, 3):
        plan = plan_merges([r.count for r in catalog.runs], arity)
        result = execute_plan(records, catalog, plan)
        assert verify_sorted(result.records, catalog) == []
        finals[arity] = [
            (row["row_id"], row["values"]["amount"]) for row in result.records["rows"]
        ]
    assert finals[2] == finals[3]
    assert [v for _, v in finals[2]] == [
        "-10", "-9", "-8", "-7", "-4", "-3", "0", "1", "10", "11", "12", "13"
    ]

    # The real reader client reads both outputs identically.
    totals = []
    for arity, plan in ((2, plan_merges([r.count for r in catalog.runs], 2)),
                        (3, plan_merges([r.count for r in catalog.runs], 3))):
        result = execute_plan(records, catalog, plan)
        path = tmp_path / f"final-{arity}.json"
        write_records(result.records, path)
        proc = _run_client(MATERIALS / "reader-contract.json", path)
        assert proc.returncode == 0, proc.stderr
        totals.append(json.loads(proc.stdout))
    assert totals[0] == totals[1]
    assert totals[0]["row_count"] == 12


def test_execution_rewrites_exactly_the_reported_cost():
    records, catalog = _catalog(ORIGINAL)
    for arity in (2, 3):
        plan = plan_merges([r.count for r in catalog.runs], arity)
        result = execute_plan(records, catalog, plan)
        assert sum(step["merged_rows"] for step in result.executed_steps) == plan.total_cost


# ---------------------------------------------------------------------------
# Catalog verification / self-checking
# ---------------------------------------------------------------------------

def test_catalog_verifies_and_detects_tampering():
    records, catalog = _catalog(ORIGINAL)
    assert verify_catalog(records, catalog) == []
    # Break a run's direction: reading it forward is then not ascending.
    from extcontract.runs import RunCatalog, RunSegment
    tampered = RunCatalog(
        key_field=catalog.key_field,
        key_type=catalog.key_type,
        record_count=catalog.record_count,
        segments=tuple(
            RunSegment(
                segment_id=r.segment_id,
                start=r.start,
                end=r.end,
                count=r.count,
                direction="reverse" if r.direction == "forward" else "forward",
                first_key=r.first_key,
                last_key=r.last_key,
            )
            for r in catalog.segments
        ),
    )
    assert verify_catalog(records, tampered) != []


# ---------------------------------------------------------------------------
# 120 segments, multiple rows each
# ---------------------------------------------------------------------------

def test_120_segments_plan_is_cheap_and_valid():
    import time

    rng = random.Random(7)
    # 120 alternating runs (ascending, then strictly descending) of 2..5 rows
    # each, with clean boundaries so every run is a genuine new segment.
    values = []
    edge = 0
    for s in range(120):
        size = rng.randint(2, 5)
        if s % 2 == 0:
            start = edge + 1
            values.extend(range(start, start + size))
            edge = start + size - 1
        else:
            start = edge - 1
            values.extend(range(start, start - size, -1))
            edge = start - size + 1
    _, catalog = _catalog_typed(
        values,
        {"kind": "INT", "bits": 32},
        {"kind": "DECIMAL", "precision": 10, "scale": 0},
    )
    assert len(catalog.runs) == 120
    sizes = [r.count for r in catalog.runs]

    start = time.perf_counter()
    plan2 = plan_merges(sizes, 2)
    plan2_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    plan3 = plan_merges(sizes, 3)
    plan3_ms = (time.perf_counter() - start) * 1000
    print(f"120-run plan: 2-way={plan2_ms:.1f}ms 3-way={plan3_ms:.1f}ms")

    assert plan3.total_cost <= plan2.total_cost
    assert plan2.total_cost == sum(s.cost for s in plan2.steps)
    for step in plan3.steps:
        assert 2 <= len(step.input_ranges) <= 3
    # Cubic interval DP over <=120 runs stays well under a second.
    assert plan2_ms < 3000 and plan3_ms < 3000


def test_limit_rejects_more_than_120_segments():
    from extcontract import LimitError

    with pytest.raises(LimitError):
        plan_merges([1] * 121, 3)
