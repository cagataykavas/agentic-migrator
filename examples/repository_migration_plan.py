from __future__ import annotations

import argparse
from pathlib import Path

from migrator.project_scan import build_inventory, migration_plan, plan_as_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan a Python repository, infer local dependencies and generate a "
            "risk-aware migration order."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Repository root to inspect (default: current directory)",
    )
    parser.add_argument(
        "--high-risk-import",
        action="append",
        default=[],
        help="Import prefix that should increase migration risk; repeatable",
    )
    parser.add_argument(
        "--output",
        default="artifacts/migration_plan.md",
        help="Markdown output path",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    inventory = build_inventory(args.root)
    plan = migration_plan(
        inventory,
        high_risk_imports=args.high_risk_import,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown = plan_as_markdown(plan)
    output.write_text(markdown, encoding="utf-8")

    print(
        f"Scanned {len(inventory.files)} Python file(s), "
        f"{inventory.total_lines} total line(s)."
    )
    if inventory.invalid_files:
        print(f"Warning: {len(inventory.invalid_files)} file(s) did not parse.")
    print(f"Generated {len(plan)} migration unit(s).")
    print(f"Wrote {output}")
    print("\nHighest-risk units")
    for unit in sorted(plan, key=lambda row: row.risk_score, reverse=True)[:8]:
        print(
            f"{unit.risk_score:3d} | {unit.path:40s} | "
            f"{', '.join(unit.reasons)}"
        )


if __name__ == "__main__":
    main()
