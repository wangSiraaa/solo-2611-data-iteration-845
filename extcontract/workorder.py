"""Archive-sorter work orders: minimum-rewrite adjacent merges of runs.

The archive machine receives the run catalog (see :mod:`extcontract.runs`) and
independently schedules the merges.  Two rules define the problem exactly:

  * **Only adjacent piles merge**, adjacency being the pile order inherited
    from the runs' original positions in the stream -- *not* the order of their
    minimum keys.  Merging two or three adjacent piles produces one new pile.
  * **Every row is rewritten once per merge that touches it**, so a merge of
    piles totalling ``r`` rows costs ``r``.  The goal is the minimum total
    rewritten rows.

The user supplies the maximum input arity ``k`` (2 or 3).  A ``k``-way machine
may merge 2 or 3 adjacent piles when ``k == 3`` and exactly 2 when ``k == 2``.

Why a DP, not a tree enumeration
--------------------------------
An arbitrary merge history is a full (2- or 3-ary) tree over the ordered runs.
There are exponentially many such trees.  But the cost of the *last* merge over
an interval ``[i, j]`` is always ``W(i, j)`` = the total rows in that interval,
independent of how the children were formed.  That interval-substructure makes
the optimum computable by dynamic programming over contiguous intervals:

    best[i][i] = 0
    pair[i][j] = min_{i<=m<j}  best[i][m] + best[m+1][j]
    triple[i][j] = min_{i<b<j} pair[i][b] + best[b+1][j]
    best[i][j] = W(i, j) + min(pair[i][j], triple[i][j])

``pair`` is the cost of a final 2-way combine of two finished sub-intervals;
``triple`` folds the first two of three sub-intervals through ``pair`` and the
last through ``best``.  Both tables are filled by increasing interval length.

Costs
-----
* Time: ``O(n**3)`` -- ``O(n**2)`` intervals, ``O(n)`` splits each; the
  ``triple`` term reuses the already-computed ``pair`` table so it stays cubic
  instead of ``O(n**5)``.
* Space: ``O(n**2)`` for ``best``/``pair`` and the reconstruction choices.
  With the documented limit of 120 runs this is ~120^2 cells, trivial.

Executing the plan is ``O(R log k)`` per merge step over ``R`` output rows for
arity ``k`` (a k-way stable merge), and the total rewrite work is exactly the
reported cost.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .errors import ContractError, LimitError
from .runs import RunCatalog, RunSegment, sort_key

# Documented envelope: the cubic interval DP is comfortable for 120 runs
# (~1.7M inner comparisons) and is rejected beyond it rather than silently
# becoming slow.
MAX_SEGMENTS = 120


@dataclass(frozen=True)
class MergeStep:
    step: int
    arity: int
    input_ranges: tuple[tuple[int, int], ...]
    input_row_counts: tuple[int, ...]
    output_range: tuple[int, int]
    output_rows: int
    cost: int

    @property
    def input_sizes(self) -> tuple[int, ...]:
        return self.input_row_counts

    @property
    def output_size(self) -> int:
        return self.output_rows

    def to_json(self) -> dict:
        return {
            "step": self.step,
            "arity": self.arity,
            "inputs": [
                {"runs": [r0, r1], "rows": rows}
                for (r0, r1), rows in zip(self.input_ranges, self.input_row_counts)
            ],
            "output": {"runs": [self.output_range[0], self.output_range[1]],
                       "rows": self.output_rows},
            "rewrite_cost": self.cost,
        }


@dataclass(frozen=True)
class MergePlan:
    max_arity: int
    segment_row_counts: tuple[int, ...]
    steps: tuple[MergeStep, ...]
    total_cost: int
    feasible: bool

    def to_json(self) -> dict:
        return {
            "max_arity": self.max_arity,
            "segment_row_counts": list(self.segment_row_counts),
            "feasible": self.feasible,
            "total_rewrite_cost": self.total_cost,
            "steps": [s.to_json() for s in self.steps],
        }


def plan_merges(
    segment_row_counts: Sequence[int], max_arity: int
) -> MergePlan:
    """Compute a minimum-total-rewrite adjacent merge schedule.

    ``segment_row_counts`` are the run lengths in original stream order; the
    list index *is* the pile order (adjacency), never the key order.
    """
    if max_arity not in (2, 3):
        raise ContractError(f"max input arity must be 2 or 3, got {max_arity!r}")
    counts = tuple(int(c) for c in segment_row_counts)
    if any(c < 1 for c in counts):
        raise ContractError("run row counts must be positive integers")
    n = len(counts)
    if n > MAX_SEGMENTS:
        raise LimitError(
            f"{n} runs exceeds the documented limit of {MAX_SEGMENTS}; the "
            f"plan enumerates interval subproblems, not merge trees"
        )
    if n <= 1:
        # Empty input or a single run: nothing is rewritten.
        return MergePlan(max_arity, counts, (), 0, True)

    prefix = [0] * (n + 1)
    for idx, c in enumerate(counts):
        prefix[idx + 1] = prefix[idx] + c

    def width(i: int, j: int) -> int:
        return prefix[j + 1] - prefix[i]

    best: list[list[Optional[int]]] = [[None] * n for _ in range(n)]
    pair: list[list[Optional[int]]] = [[None] * n for _ in range(n)]
    pair_split: list[list[Optional[int]]] = [[None] * n for _ in range(n)]
    choice: list[list[Optional[tuple]]] = [[None] * n for _ in range(n)]
    for i in range(n):
        best[i][i] = 0

    for length in range(2, n + 1):
        for i in range(0, n - length + 1):
            j = i + length - 1

            # 2-way final combine: split into [i..m] and [m+1..j].
            best_pair: Optional[int] = None
            best_m: Optional[int] = None
            for m in range(i, j):
                left, right = best[i][m], best[m + 1][j]
                if left is None or right is None:
                    continue
                cand = left + right
                if best_pair is None or cand < best_pair:
                    best_pair, best_m = cand, m
            pair[i][j] = best_pair
            pair_split[i][j] = best_m

            total = width(i, j)
            best_choice: Optional[tuple] = None
            best_val: Optional[int] = None
            if best_pair is not None:
                best_val = best_pair + total
                best_choice = ("pair", best_m)

            # 3-way final combine (only for a 3-way machine): three contiguous
            # groups [i..a], [a+1..b], [b+1..j]; the first two fold via pair.
            if max_arity == 3:
                for b in range(i + 1, j):
                    first_two = pair[i][b]
                    right = best[b + 1][j]
                    if first_two is None or right is None:
                        continue
                    cand = first_two + total + right
                    if best_val is None or cand < best_val:
                        best_val = cand
                        best_choice = ("triple", b)
            best[i][j] = best_val
            choice[i][j] = best_choice

    if best[0][n - 1] is None:
        return MergePlan(max_arity, counts, (), 0, False)

    steps: list[MergeStep] = []

    def emit(i: int, j: int) -> None:
        if i == j:
            return
        kind = choice[i][j]
        if kind is None:  # pragma: no cover - guarded by feasibility above
            raise ContractError("internal: unreachable interval in merge plan")
        if kind[0] == "pair":
            m = kind[1]
            emit(i, m)
            emit(m + 1, j)
            children = [(i, m), (m + 1, j)]
        else:
            b = kind[1]
            a = pair_split[i][b]
            emit(i, a)
            emit(a + 1, b)
            emit(b + 1, j)
            children = [(i, a), (a + 1, b), (b + 1, j)]
        steps.append(
            MergeStep(
                step=len(steps),
                arity=len(children),
                input_ranges=tuple(children),
                input_row_counts=tuple(width(x, y) for (x, y) in children),
                output_range=(i, j),
                output_rows=width(i, j),
                cost=width(i, j),
            )
        )

    emit(0, n - 1)
    total_cost = sum(s.cost for s in steps)
    return MergePlan(max_arity, counts, tuple(steps), total_cost, True)


# ---------------------------------------------------------------------------
# Executing a plan: really merge the rows (stable, ascending)
# ---------------------------------------------------------------------------

def _segment_rows(records: dict, segment: RunSegment) -> list[dict]:
    rows = records["rows"]
    return [rows[p] for p in segment.positions()]


def _row_key(row: dict, key_field: str, kind: str) -> tuple[int, Any, int]:
    # Tie-break on the original global position: this is exactly the stability
    # requirement (equal keys keep their original order).
    return (*sort_key(row["values"][key_field], kind), row["position"])


def _kway_merge(piles: list[list[dict]], key_field: str, kind: str) -> list[dict]:
    heap: list[tuple] = []
    iterators = [iter(pile) for pile in piles]
    for idx, it in enumerate(iterators):
        row = next(it, None)
        if row is not None:
            heapq.heappush(heap, (_row_key(row, key_field, kind), idx, row))
    out: list[dict] = []
    while heap:
        _, idx, row = heapq.heappop(heap)
        out.append(row)
        nxt = next(iterators[idx], None)
        if nxt is not None:
            heapq.heappush(heap, (_row_key(nxt, key_field, kind), idx, nxt))
    return out


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of really performing a merge plan."""

    records: dict          # reader-records/v1, ascending + stable
    executed_steps: list[dict]
    ordered_positions: list[int]

    @property
    def total_rewrite_cost(self) -> int:
        return sum(step["merged_rows"] for step in self.executed_steps)


def execute_plan(
    records: dict, catalog: RunCatalog, plan: MergePlan
) -> ExecutionResult:
    """Really perform the plan's merges and return the stable sorted stream.

    The returned ``records`` is a ``reader-records/v1`` document whose rows are
    ascending on the key with equal keys in original order, directly consumable
    by ``reader_client.py``.  ``executed_steps`` replays each merge with the
    resulting row count so the work order can be recomputed independently.
    """
    if not plan.feasible:
        raise ContractError("cannot execute an infeasible merge plan")
    if list(plan.segment_row_counts) != [s.count for s in catalog.segments]:
        raise ContractError("merge plan does not match the run catalog")

    kind = catalog.key_type.get("kind", "NUMERIC")
    key_field = catalog.key_field
    n = len(catalog.segments)
    if n == 0:
        return ExecutionResult(
            records={
                "format": records.get("format"),
                "reader_version": records.get("reader_version"),
                "rows": [],
            },
            executed_steps=[],
            ordered_positions=[],
        )

    # Interval (i, j) -> sorted pile rows.  Single runs are read in the
    # segment's direction; composed piles are filled by the plan's steps.
    piles: dict[tuple[int, int], list[dict]] = {
        (seg.segment_id, seg.segment_id): _segment_rows(records, seg)
        for seg in catalog.segments
    }

    executed: list[dict] = []
    for step in plan.steps:
        children = [piles[rng] for rng in step.input_ranges]
        merged = _kway_merge(children, key_field, kind)
        if len(merged) != step.output_rows:  # pragma: no cover - invariant guard
            raise ContractError("merge produced a different row count than planned")
        piles[step.output_range] = merged
        executed.append(
            {
                "step": step.step,
                "inputs": [
                    {"runs": list(rng), "rows": cnt}
                    for rng, cnt in zip(step.input_ranges, step.input_row_counts)
                ],
                "merged_rows": len(merged),
                "rewrite_cost": len(merged),
            }
        )

    final_rows = piles[(0, n - 1)]
    return ExecutionResult(
        records={
            "format": records.get("format"),
            "reader_version": records.get("reader_version"),
            "rows": [dict(r) for r in final_rows],
        },
        executed_steps=executed,
        ordered_positions=[r["position"] for r in final_rows],
    )


def verify_sorted(final_records: dict, catalog: RunCatalog) -> list[str]:
    """Independently check the final stream is ascending and stable."""
    problems: list[str] = []
    rows = final_records["rows"]
    key_field = catalog.key_field
    kind = catalog.key_type.get("kind", "NUMERIC")
    prev = None
    for row in rows:
        key = sort_key(row["values"][key_field], kind)
        if prev is not None:
            if key < prev[0]:
                problems.append(f"row {row['position']} is smaller than its predecessor")
            elif key == prev[0] and row["position"] < prev[1]:
                problems.append(
                    f"equal-key row {row['position']} breaks original order "
                    f"(after {prev[1]})"
                )
        prev = (key, row["position"])
    return problems
