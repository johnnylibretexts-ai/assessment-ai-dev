#!/usr/bin/env python3
"""Verify that Assessment AI and ADAPT ship byte-identical framework seeds."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*-v1.json"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adapt-root",
        type=Path,
        required=True,
        help="Path to the ADAPT checkout or deployment source.",
    )
    args = parser.parse_args()

    assessment = _digests(ROOT / "app" / "catalogs")
    adapt = _digests(args.adapt_root.resolve() / "resources" / "frameworks")
    if not assessment or assessment != adapt:
        missing_in_adapt = sorted(set(assessment) - set(adapt))
        missing_in_assessment = sorted(set(adapt) - set(assessment))
        mismatched = sorted(
            name
            for name in set(assessment) & set(adapt)
            if assessment[name] != adapt[name]
        )
        parser.error(
            "framework catalogs differ; "
            f"missing_in_adapt={missing_in_adapt}, "
            f"missing_in_assessment_ai={missing_in_assessment}, "
            f"sha256_mismatch={mismatched}"
        )
    for name, digest in assessment.items():
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
