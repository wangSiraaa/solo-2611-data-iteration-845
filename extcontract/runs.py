"""Ordered-run catalog over the *real* adapted record stream.

The adapter (:mod:`extcontract.adapter`) emits ``reader-records/v1`` rows in
input order.  This module is the new, separate entry point that

  1. concatenates the rows of one or more adapted files in that exact input
     order (the "real adapter file and row input order"),
  2. stamps every row with its original global position (``position``), and
  3. partitions the stream into naturally ordered runs ("segments") and emits
     an independently checkable run catalog for the archive sorter.

Ordering contract
-----------------
One numeric reader field is chosen as the ascending key.  Keys are compared as
*exact rationals* (``fractions.Fraction``), never as text: ``-10 < -2`` and
``2 < 10`` because they are numbers, not strings.  ``null`` sorts *after* every
number.  Equal keys are ordered by their original global position, so the sort
is stable.

Segmentation rule (natural merge runs)
--------------------------------------
Starting from the first unprocessed row:

  * if the next key is *strictly* smaller, take the longest *strictly*
    decreasing prefix and read that segment **reversed** (so it becomes
    ascending).  A strictly decreasing run can never step across an equal key,
    which is what keeps equal keys in original order;
  * otherwise take the longest *non-decreasing* prefix and read it forward.
    A trailing single row is a one-row segment.

Every segment is therefore ascending when read in its stated direction.  This
is the classic natural-merge run detector (ascending = non-decreasing,
descending = strictly decreasing) needed for stability.

Costs
-----
Building the catalog scans the rows once: ``O(m)`` time to compare keys and
``O(m + r)`` space for the key list and the ``r`` segment descriptors (``m``
rows, ``r`` runs).  Nothing is sorted here; the runs capture the existing
order.  The archive sorter, not this module, pays the merge cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .adapter import RECORDS_FORMAT, AdaptationResult, adapt_rows
from .contract import Contract
from .errors import ContractError

CATALOG_FORMAT = "ordered-run-catalog/v1"

# A comparable key: (null_flag, Fraction).  null_flag 1 means null, which is
# greater than every numeric key (null_flag 0).
NULL_KEY = (1, Fraction(0))


def sort_key(encoded: Any, key_kind: str = "NUMERIC") -> tuple[int, Fraction]:
    """Comparable exact key for an encoded reader value.

    ``encoded`` is the ``reader-records/v1`` form: a minimal rational string,
    ``None`` for null, or a string for TEXT.  TEXT columns are rejected as sort
    keys -- the contract demands a *numeric* field, and numbers are compared as
    values, never lexicographically.
    """
    if encoded is None:
        return NULL_KEY
    if key_kind == "TEXT":
        raise ContractError(
            "sort key must be a numeric field; TEXT values are never sorted "
            "numerically"
        )
    try:
        return (0, Fraction(encoded))
    except (TypeError, ValueError, ZeroDivisionError):
        raise ContractError(f"sort key value {encoded!r} is not an exact rational") from None


# ---------------------------------------------------------------------------
# Concatenation of adapted files
# ---------------------------------------------------------------------------

def concatenate_records(
    documents: Iterable[dict], *, require_unique_row_ids: bool = True
) -> dict:
    """Concatenate ``reader-records/v1`` documents in the given order.

    Rows keep their document order; each is stamped with a 0-based global
    ``position``.  The reader contract version must agree across documents.
    With ``require_unique_row_ids`` (the default) a duplicate ``row_id`` is
    rejected, because the supplied ``reader_client.py`` demands unique ids.
    """
    docs = list(documents)
    if not docs:
        return {"format": RECORDS_FORMAT, "reader_version": None, "rows": []}

    version = docs[0].get("reader_version")
    for doc in docs:
        if doc.get("format") != RECORDS_FORMAT:
            raise ContractError("only reader-records/v1 documents can be concatenated")
        if doc.get("reader_version") != version:
            raise ContractError("cannot concatenate documents from different reader versions")

    rows: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        for row in doc["rows"]:
            row_id = row.get("row_id")
            if require_unique_row_ids:
                if row_id in seen:
                    raise ContractError(f"duplicate row_id {row_id!r} across concatenated files")
                seen.add(row_id)
            stamped = dict(row)
            stamped["position"] = len(rows)
            rows.append(stamped)
    return {"format": RECORDS_FORMAT, "reader_version": version, "rows": rows}


# ---------------------------------------------------------------------------
# Run catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunSegment:
    """One naturally ordered run.

    ``start`` / ``end`` are inclusive 0-based global positions.  ``direction``
    is ``"forward"`` (read ``start -> end``) or ``"reverse"`` (read
    ``end -> start``); either way the rows come out ascending.
    """

    segment_id: int
    start: int
    end: int
    count: int
    direction: str
    first_key: Any
    last_key: Any

    def positions(self) -> list[int]:
        if self.direction == "forward":
            return list(range(self.start, self.end + 1))
        return list(range(self.end, self.start - 1, -1))

    def to_json(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
            "count": self.count,
            "direction": self.direction,
            # Explicit, machine-checkable instruction for how to read the run.
            "read": "start->end" if self.direction == "forward" else "end->start",
            "first_key": self.first_key,
            "last_key": self.last_key,
        }


@dataclass(frozen=True)
class RunCatalog:
    key_field: str
    key_type: dict
    record_count: int
    segments: tuple[RunSegment, ...]

    def to_json(self) -> dict:
        return {
            "format": CATALOG_FORMAT,
            "key_field": self.key_field,
            "key_type": self.key_type,
            "record_count": self.record_count,
            "segment_count": len(self.segments),
            "segments": [s.to_json() for s in self.segments],
        }

    @property
    def runs(self) -> tuple[RunSegment, ...]:
        """Alias for ``segments`` (the natural runs)."""
        return self.segments


def _field_type_json(field: Any) -> dict:
    from .types import type_to_json

    return type_to_json(field.type)


def build_run_catalog(records: dict, reader: Contract, key_field: str) -> RunCatalog:
    """Partition a concatenated, position-stamped record stream into runs.

    ``records`` must be the output of :func:`concatenate_records` (or a single
    adapted document); ``reader`` supplies the key field's declared type.
    """
    fields = reader.field_by_id()
    if key_field not in fields:
        raise ContractError(f"sort key field {key_field!r} is not in the reader contract")
    field = fields[key_field]
    if field.type.kind == "TEXT":
        raise ContractError(
            f"sort key field {key_field!r} is TEXT; choose a numeric field"
        )

    rows = records.get("rows", [])
    keys = [sort_key(row["values"][key_field], field.type.kind) for row in rows]
    n = len(keys)
    raw_keys = [row["values"][key_field] for row in rows]

    segments: list[RunSegment] = []
    i = 0
    while i < n:
        sid = len(segments)
        if i + 1 < n and keys[i + 1] < keys[i]:
            j = i
            while j + 1 < n and keys[j + 1] < keys[j]:
                j += 1
            segments.append(
                RunSegment(
                    segment_id=sid,
                    start=i,
                    end=j,
                    count=j - i + 1,
                    direction="reverse",
                    first_key=raw_keys[j],
                    last_key=raw_keys[i],
                )
            )
            i = j + 1
        else:
            j = i
            while j + 1 < n and keys[j + 1] >= keys[j]:
                j += 1
            segments.append(
                RunSegment(
                    segment_id=sid,
                    start=i,
                    end=j,
                    count=j - i + 1,
                    direction="forward",
                    first_key=raw_keys[i],
                    last_key=raw_keys[j],
                )
            )
            i = j + 1

    return RunCatalog(
        key_field=key_field,
        key_type=_field_type_json(field),
        record_count=n,
        segments=tuple(segments),
    )


def catalog_from_adaptation(
    adaptation: AdaptationResult, reader: Contract, key_field: str
) -> tuple[dict, RunCatalog]:
    """Convenience wrapper: adapt one row list, concatenate, and catalog it."""
    if adaptation.status != "ADAPTED":
        raise ContractError(f"adaptation is {adaptation.status}, not ADAPTED")
    records = concatenate_records([adaptation.records])
    return records, build_run_catalog(records, reader, key_field)


def catalog_from_rows(
    writer: Contract, reader: Contract, rows: list, key_field: str
) -> tuple[dict, RunCatalog]:
    """Adapt raw writer rows with the real adapter, then build the catalog."""
    return catalog_from_adaptation(adapt_rows(writer, reader, rows), reader, key_field)


def catalog_from_documents(
    writer: Contract, reader: Contract, documents: list[dict], key_field: str
) -> tuple[dict, RunCatalog]:
    """Adapt several file documents in order, concatenate, then build the catalog."""
    adapted = []
    for doc in documents:
        from .adapter import adapt_file_document

        result = adapt_file_document(writer, reader, doc)
        if result.status != "ADAPTED":
            raise ContractError(f"file adaptation is {result.status}, not ADAPTED")
        adapted.append(result.records)
    records = concatenate_records(adapted)
    return records, build_run_catalog(records, reader, key_field)


# ---------------------------------------------------------------------------
# Independent verification
# ---------------------------------------------------------------------------

def verify_catalog(records: dict, catalog: RunCatalog) -> list[str]:
    """Re-check a catalog from the record stream alone.  Returns problems.

    Checks: segments tile the positions contiguously in order; counts match;
    and every segment is non-decreasing in its stated read direction.  An empty
    list means the catalog is valid.
    """
    problems: list[str] = []
    rows = records.get("rows", [])
    if catalog.record_count != len(rows):
        problems.append(
            f"catalog record_count {catalog.record_count} != actual {len(rows)}"
        )
    cursor = 0
    kind = catalog.key_type.get("kind", "NUMERIC")
    for seg in catalog.segments:
        if seg.start != cursor:
            problems.append(
                f"segment {seg.segment_id} starts at {seg.start}, expected {cursor}"
            )
        if seg.end < seg.start or seg.count != seg.end - seg.start + 1:
            problems.append(f"segment {seg.segment_id} has inconsistent bounds")
        positions = seg.positions()
        keys = [sort_key(rows[p]["values"][catalog.key_field], kind) for p in positions]
        for a, b in zip(keys, keys[1:]):
            if b < a:
                problems.append(
                    f"segment {seg.segment_id} is not ascending when read {seg.direction}"
                )
                break
        cursor = seg.end + 1
    if cursor != len(rows):
        problems.append(f"segments cover {cursor} positions, expected {len(rows)}")
    return problems
