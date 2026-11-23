#!/usr/bin/env python3
"""Inspect agy_advise invocation logs and record post-hoc quality feedback."""

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from telemetry import (  # noqa: E402
    FEEDBACK_LABELS,
    format_summary,
    record_feedback,
    resolve_feedback_path,
    resolve_log_path,
    summarize_logs,
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=None, help="Optional projects.sqlite3 path to locate sibling logs.")

    parser = argparse.ArgumentParser(
        description="Summarize agy_advise invocation logs or record post-hoc quality feedback."
    )
    subparsers = parser.add_subparsers(dest="command")

    summary_cmd = subparsers.add_parser(
        "summary", parents=[common], help="Print aggregated invocation & feedback metrics (default)."
    )
    summary_cmd.add_argument("--project", default=None, help="Filter by project_id.")
    summary_cmd.add_argument("--days", type=int, default=None, help="Only include records from the last N days.")
    summary_cmd.add_argument("--recent", type=int, default=10, help="Number of recent calls to display.")
    summary_cmd.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON instead of Markdown.")

    fb_cmd = subparsers.add_parser(
        "feedback", parents=[common], help="Record post-hoc quality feedback for an invocation_id."
    )
    fb_cmd.add_argument("invocation_id", help="invocation.id returned by agy_advise.")
    fb_cmd.add_argument("label", choices=FEEDBACK_LABELS, help="Quality outcome label.")
    fb_cmd.add_argument("--note", default="", help="Brief reason or lesson learned.")
    fb_cmd.add_argument("--project", default=None, help="Optional project_id tag.")
    fb_cmd.add_argument("--phase", default=None, help="Optional phase tag.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # Default to "summary" when invoked with no subcommand (or only summary flags)
    if not args_list or (
        args_list[0].startswith("-")
        and args_list[0] not in ("-h", "--help")
        and "summary" not in args_list
        and "feedback" not in args_list
    ):
        args_list = ["summary", *args_list]

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.command == "feedback":
        target = record_feedback(
            invocation_id=args.invocation_id,
            label=args.label,
            note=args.note,
            project_id=args.project,
            phase=args.phase,
            db_path=args.db,
        )
        print(f"Recorded feedback ({args.label}) for {args.invocation_id} in {target}")
        return 0

    log_path = resolve_log_path(args.db)
    feedback_path = resolve_feedback_path(args.db)
    summary = summarize_logs(
        log_path,
        feedback_path,
        project_id=args.project,
        days=args.days,
        recent=args.recent,
    )
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
