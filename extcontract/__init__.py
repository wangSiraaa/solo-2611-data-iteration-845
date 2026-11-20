"""External contract toolkit: exact finite-type compatibility, adaptation
planning, file adaptation, and revision-aware atomic ingestion.

Public entry points (see README for the full walkthrough):

    load_contract / parse_contract        contract documents
    check_compatibility                   row-free whole-domain planning
    replace_field_type                    contract revisions
    adapt_rows / adapt_file_document      file -> reader-records/v1
    IngestionLibrary                      stateful, revision-aware ingestion
    build_run_catalog / concatenate_records
                                          ordered-run catalog for the archive
    plan_merges / execute_plan            min-rewrite adjacent merge work order

The supplied ``materials/745/reader_client.py`` is never imported or
modified; its CLI consumes the ``reader-records/v1`` documents this package
produces.
"""

from .types import (
    DecimalType,
    IntType,
    TextType,
    decode_writer_value,
    encode_reader_value,
    parse_type,
    rational_string,
    type_to_json,
)
from .contract import Contract, Field, load_contract, parse_contract
from .compat import CompatibilityReport, ColumnPlan, check_compatibility, replace_field_type
from .adapter import (
    AdaptationResult,
    CanonicalRow,
    RECORDS_FORMAT,
    RowError,
    adapt_file_document,
    adapt_rows,
    write_records,
)
from .ingest import IngestionLibrary, IngestResult, Receipt, RowState
from .runs import (
    CATALOG_FORMAT,
    RunCatalog,
    RunSegment,
    build_run_catalog,
    catalog_from_adaptation,
    catalog_from_documents,
    catalog_from_rows,
    concatenate_records,
    verify_catalog,
)
from .workorder import (
    ExecutionResult,
    MergePlan,
    MergeStep,
    execute_plan,
    plan_merges,
    verify_sorted,
)
from .errors import (
    ContractError,
    ConversionError,
    DomainError,
    ExtContractError,
    LimitError,
)

__all__ = [
    "DecimalType",
    "IntType",
    "TextType",
    "decode_writer_value",
    "encode_reader_value",
    "parse_type",
    "rational_string",
    "type_to_json",
    "Contract",
    "Field",
    "load_contract",
    "parse_contract",
    "CompatibilityReport",
    "ColumnPlan",
    "check_compatibility",
    "replace_field_type",
    "AdaptationResult",
    "CanonicalRow",
    "RECORDS_FORMAT",
    "RowError",
    "adapt_file_document",
    "adapt_rows",
    "write_records",
    "IngestionLibrary",
    "IngestResult",
    "Receipt",
    "RowState",
    "CATALOG_FORMAT",
    "RunCatalog",
    "RunSegment",
    "build_run_catalog",
    "catalog_from_adaptation",
    "catalog_from_documents",
    "catalog_from_rows",
    "concatenate_records",
    "verify_catalog",
    "MergePlan",
    "MergeStep",
    "ExecutionResult",
    "execute_plan",
    "plan_merges",
    "verify_sorted",
    "ContractError",
    "ConversionError",
    "DomainError",
    "ExtContractError",
    "LimitError",
]

__version__ = "1.0.0"
