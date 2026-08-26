from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .ast_rules import AttributeRewrite, ImportRewrite, KeywordRewrite, transform_source
from .cost import CostLedger
from .project_scan import build_inventory, migration_plan, plan_as_markdown
from .repository import GitRepository
from .runners import PytestSandboxRunner
from .sandbox import LocalSandbox
from .semantic_diff import compare_public_api


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(text)


def command_scan(args: argparse.Namespace) -> int:
    inventory = build_inventory(args.root)
    plan = migration_plan(inventory, high_risk_imports=args.high_risk_import)
    if args.format == "markdown":
        _write_or_print(plan_as_markdown(plan), args.output)
        return 0

    payload = {
        "root": str(inventory.root),
        "files": len(inventory.files),
        "total_lines": inventory.total_lines,
        "invalid_files": [asdict(item) for item in inventory.invalid_files],
        "plan": [asdict(unit) for unit in plan],
    }
    _write_or_print(json.dumps(payload, indent=2), args.output)
    return 0


def command_transform(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    source = source_path.read_text(encoding="utf-8")

    imports = tuple(
        ImportRewrite(old, new)
        for old, new in (item.split("=", 1) for item in args.import_rewrite)
    )
    attributes = tuple(
        AttributeRewrite(old, new)
        for old, new in (item.split("=", 1) for item in args.attribute_rewrite)
    )
    keywords = []
    for raw in args.keyword_rewrite:
        function_name, mapping = raw.split(":", 1)
        old_keyword, new_keyword = mapping.split("=", 1)
        keywords.append(KeywordRewrite(function_name, old_keyword, new_keyword))

    transformed, applied = transform_source(
        source,
        import_rewrites=imports,
        keyword_rewrites=tuple(keywords),
        attribute_rewrites=attributes,
    )

    output_path = Path(args.output) if args.output else source_path.with_suffix(".migrated.py")
    output_path.write_text(transformed, encoding="utf-8")
    print(f"wrote {output_path}")
    if applied:
        print("applied structural rules:")
        for item in applied:
            print(f"  - {item}")
    else:
        print("no structural rules matched")
    return 0


def command_semantic_diff(args: argparse.Namespace) -> int:
    before = Path(args.before).read_text(encoding="utf-8")
    after = Path(args.after).read_text(encoding="utf-8")
    diff = compare_public_api(before, after)
    text = json.dumps(diff.as_dict(), indent=2)
    _write_or_print(text, args.output)
    return 2 if diff.breaking and args.fail_on_breaking else 0


def command_sandbox_test(args: argparse.Namespace) -> int:
    candidate = Path(args.candidate).read_text(encoding="utf-8")
    tests = Path(args.tests).read_text(encoding="utf-8")
    sandbox = LocalSandbox(
        timeout_seconds=args.timeout,
        max_output_bytes=args.max_output_bytes,
    )
    runner = PytestSandboxRunner(sandbox)
    result = runner(candidate, tests)
    payload = {
        "passed": result.passed,
        "name": result.name,
        "kind": result.kind.value,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    _write_or_print(json.dumps(payload, indent=2), args.output)
    return 0 if result.passed else 2


def command_git_checkpoint(args: argparse.Namespace) -> int:
    repository = GitRepository(args.root)
    checkpoint = repository.checkpoint()
    print(json.dumps(asdict(checkpoint), indent=2))
    if args.require_clean and checkpoint.dirty:
        return 2
    return 0


def command_git_patch(args: argparse.Namespace) -> int:
    repository = GitRepository(args.root)
    path = repository.write_patch(args.output, base=args.base)
    print(path)
    return 0


def command_cost_summary(args: argparse.Namespace) -> int:
    ledger = CostLedger(args.ledger)
    print(json.dumps(ledger.summary(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-migrator",
        description="Analyze and migrate Python code with deterministic, testable rules.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="build a dependency-aware repository migration plan")
    scan.add_argument("root", nargs="?", default=".")
    scan.add_argument("--high-risk-import", action="append", default=[])
    scan.add_argument("--format", choices=("json", "markdown"), default="markdown")
    scan.add_argument("--output")
    scan.set_defaults(handler=command_scan)

    transform = subparsers.add_parser("transform", help="apply structural AST migration rules")
    transform.add_argument("source")
    transform.add_argument("--output")
    transform.add_argument(
        "--import-rewrite",
        action="append",
        default=[],
        metavar="OLD=NEW",
    )
    transform.add_argument(
        "--attribute-rewrite",
        action="append",
        default=[],
        metavar="OLD=NEW",
    )
    transform.add_argument(
        "--keyword-rewrite",
        action="append",
        default=[],
        metavar="FUNCTION:OLD=NEW",
    )
    transform.set_defaults(handler=command_transform)

    semantic = subparsers.add_parser(
        "semantic-diff",
        help="compare public Python API signatures before and after migration",
    )
    semantic.add_argument("before")
    semantic.add_argument("after")
    semantic.add_argument("--output")
    semantic.add_argument("--fail-on-breaking", action="store_true")
    semantic.set_defaults(handler=command_semantic_diff)

    sandbox = subparsers.add_parser(
        "sandbox-test",
        help="run a candidate and pytest harness in the bounded temporary sandbox",
    )
    sandbox.add_argument("candidate")
    sandbox.add_argument("tests")
    sandbox.add_argument("--timeout", type=float, default=20.0)
    sandbox.add_argument("--max-output-bytes", type=int, default=256_000)
    sandbox.add_argument("--output")
    sandbox.set_defaults(handler=command_sandbox_test)

    checkpoint = subparsers.add_parser("git-checkpoint", help="show the current Git migration checkpoint")
    checkpoint.add_argument("root", nargs="?", default=".")
    checkpoint.add_argument("--require-clean", action="store_true")
    checkpoint.set_defaults(handler=command_git_checkpoint)

    patch = subparsers.add_parser("git-patch", help="export the current repository diff as a patch artifact")
    patch.add_argument("root", nargs="?", default=".")
    patch.add_argument("--base", default="HEAD")
    patch.add_argument("--output", default="artifacts/migration.patch")
    patch.set_defaults(handler=command_git_patch)

    cost = subparsers.add_parser("cost-summary", help="summarize optional LLM token/cost usage")
    cost.add_argument("--ledger", default="artifacts/llm_usage.jsonl")
    cost.set_defaults(handler=command_cost_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
