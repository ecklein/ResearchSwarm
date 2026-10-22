"""Thin argparse interface to the portable trace API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from researchswarm.exceptions import ResearchSwarmError
from researchswarm.storage.sqlite import SQLiteStorage
from researchswarm.trace import (
    ReplayEngine,
    RunTrace,
    TraceExportOptions,
    TraceValidator,
    verify_replay,
)


async def _export(args: argparse.Namespace) -> None:
    # Read-only backup gives a consistent database snapshot and avoids upgrading or
    # creating tables in the source database, including legacy Phase 1 databases.
    source = Path(args.db).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    if Path(args.output).resolve() == source:
        raise ValueError("trace output must not overwrite the source database")
    with tempfile.TemporaryDirectory(prefix="researchswarm-trace-") as temporary:
        snapshot = Path(temporary) / "snapshot.db"
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(snapshot)
        try:
            connection.backup(destination)
        finally:
            destination.close()
            connection.close()
        storage = SQLiteStorage(snapshot)
        try:
            trace = await RunTrace.from_storage(
                args.run_id,
                storage,
                TraceExportOptions(
                    include_task_inputs=not args.exclude_task_inputs,
                    include_task_outputs=not args.exclude_task_outputs,
                    include_artifact_data=args.include_artifact_data,
                ),
            )
        finally:
            await storage.close()
    if args.directory:
        trace.to_directory(args.output)
    else:
        trace.to_json(args.output)
    print(json.dumps(trace.manifest(), indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="researchswarm")
    groups = parser.add_subparsers(dest="group", required=True)
    trace_parser = groups.add_parser("trace", help="export, validate, or replay a run trace")
    commands = trace_parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--db", required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--directory", action="store_true")
    export.add_argument("--include-artifact-data", action="store_true")
    export.add_argument("--exclude-task-inputs", action="store_true")
    export.add_argument("--exclude-task-outputs", action="store_true")
    for name in ("validate", "replay"):
        command = commands.add_parser(name)
        command.add_argument("path")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            asyncio.run(_export(args))
            return 0
        trace = RunTrace.load(args.path)
        if args.command == "validate":
            result = TraceValidator.validate(trace)
            print(
                json.dumps(
                    {"valid": result.valid, "errors": result.errors, "warnings": result.warnings},
                    indent=2,
                )
            )
        else:
            state = ReplayEngine.replay(trace)
            result = verify_replay(trace)
            print(
                json.dumps(
                    {
                        "state": state.to_dict(),
                        "verification": {
                            "valid": result.valid,
                            "errors": result.errors,
                            "warnings": result.warnings,
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0 if result.valid else 1
    except (ResearchSwarmError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(f"researchswarm: {exc}", file=sys.stderr)
        return 2
