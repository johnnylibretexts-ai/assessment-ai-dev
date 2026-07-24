#!/usr/bin/env python3
"""Read-only audit of current hint citations against exact question sources."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database, DraftRepository  # noqa: E402
from app.hint_audit import audit_current_hints, read_only_sqlite_url  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "ASSESSMENT_AI_DATABASE_URL",
            "sqlite:///./data/assessment-ai.db",
        ),
        help="SQLite database URL; opened with mode=ro.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a stable JSON report.",
    )
    args = parser.parse_args()

    try:
        database = Database(read_only_sqlite_url(args.database_url))
        try:
            findings = audit_current_hints(DraftRepository(database))
        finally:
            database.dispose()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps({"finding_count": len(findings), "findings": findings}))
    elif not findings:
        print("No invalid current hint citations found.")
    else:
        for finding in findings:
            print(
                "draft={draft_id} edit={edit_count} hint_version={hint_version} "
                "rung={rung} allowed={allowed_citations} "
                "invalid={invalid_citations}".format(**finding)
            )
        print(f"Invalid current hint rungs: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
