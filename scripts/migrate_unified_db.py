#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.unified_migration import (  # noqa: E402
    MigrationError,
    migrate_legacy_databases,
    open_intake_gate,
    open_runner_gate,
    report_dict,
    validate_unified_database,
)


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate cms-tg-ingest legacy databases")
    parser.add_argument("--tasks")
    parser.add_argument("--submissions")
    parser.add_argument("--output")
    parser.add_argument("--report")
    parser.add_argument("--validate")
    parser.add_argument("--print-migration-id", action="store_true")
    parser.add_argument("--open-runner-gate")
    parser.add_argument("--open-intake-gate")
    parser.add_argument("--migration-id")
    args = parser.parse_args(argv)
    try:
        if args.tasks and args.submissions and args.output:
            report = migrate_legacy_databases(args.tasks, args.submissions, args.output)
            payload = report_dict(report)
            if args.report:
                _write_report(Path(args.report), payload)
            print(
                f"matched={report.matched_submissions} synthetic={report.synthetic_tasks} "
                f"unmapped={report.unmapped_rows} migration_id={report.migration_id}"
            )
            return 0
        if args.validate:
            result = validate_unified_database(args.validate)
            if args.print_migration_id:
                print(result["migration_id"])
            else:
                print(
                    f"schema={result['schema_version']} write_gate={result['write_gate']} "
                    f"legacy_import_executable={result['legacy_import_executable']}"
                )
            return 0
        if args.open_runner_gate:
            if not args.migration_id:
                raise MigrationError("migration id required")
            open_runner_gate(args.open_runner_gate, args.migration_id)
            return 0
        if args.open_intake_gate:
            if not args.migration_id:
                raise MigrationError("migration id required")
            open_intake_gate(args.open_intake_gate, args.migration_id)
            return 0
        parser.print_usage()
        return 2
    except (MigrationError, OSError, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
