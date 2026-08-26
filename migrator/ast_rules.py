from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ImportRewrite:
    old_module: str
    new_module: str


@dataclass(frozen=True)
class KeywordRewrite:
    function_name: str
    old_keyword: str
    new_keyword: str


@dataclass(frozen=True)
class AttributeRewrite:
    old_chain: str
    new_chain: str


@dataclass(frozen=True)
class SourceEdit:
    start: int
    end: int
    replacement: bytes
    rule_id: str
    priority: int


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def build_dotted_expr(name: str, *, ctx: ast.expr_context | None = None) -> ast.expr:
    """Compatibility helper for callers that need an AST expression."""
    parts = name.split(".")
    node: ast.expr = ast.Name(id=parts[0], ctx=ast.Load())
    for part in parts[1:]:
        node = ast.Attribute(value=node, attr=part, ctx=ast.Load())
    if ctx is not None and isinstance(node, (ast.Name, ast.Attribute)):
        node.ctx = ctx
    return node


def _line_byte_offsets(source: str) -> list[int]:
    offsets = [0]
    running = 0
    for line in source.splitlines(keepends=True):
        running += len(line.encode("utf-8"))
        offsets.append(running)
    return offsets


def _span(node: ast.AST, offsets: list[int]) -> tuple[int, int]:
    if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
        raise ValueError("AST node does not expose source positions")
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    return start, end


def _rewrite_root(name: str, bound_names: dict[str, str]) -> str:
    root, dot, rest = name.partition(".")
    replacement = bound_names.get(root)
    if not replacement:
        return name
    return replacement + (dot + rest if dot else "")


def _non_overlapping(edits: list[SourceEdit]) -> list[SourceEdit]:
    chosen: list[SourceEdit] = []
    for candidate in sorted(
        edits,
        key=lambda item: (-item.priority, -(item.end - item.start), item.start),
    ):
        overlaps = any(
            candidate.start < existing.end and existing.start < candidate.end
            for existing in chosen
        )
        if not overlaps:
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: item.start)


def _replace_within_span(
    data: bytes,
    start: int,
    end: int,
    old: str,
    new: str,
    *,
    rule_id: str,
    priority: int,
) -> SourceEdit | None:
    old_bytes = old.encode("utf-8")
    segment = data[start:end]
    relative = segment.find(old_bytes)
    if relative < 0:
        return None
    absolute = start + relative
    return SourceEdit(
        start=absolute,
        end=absolute + len(old_bytes),
        replacement=new.encode("utf-8"),
        rule_id=rule_id,
        priority=priority,
    )


def transform_source(
    source: str,
    *,
    import_rewrites: Iterable[ImportRewrite] = (),
    keyword_rewrites: Iterable[KeywordRewrite] = (),
    attribute_rewrites: Iterable[AttributeRewrite] = (),
) -> tuple[str, tuple[str, ...]]:
    """Apply structural edits while preserving comments, strings, and formatting.

    The AST is used to locate semantic nodes, but edits are written back to the
    original UTF-8 source instead of serializing the whole tree with ``ast.unparse``.
    This keeps unrelated comments and formatting intact.
    """
    tree = ast.parse(source)
    data = source.encode("utf-8")
    offsets = _line_byte_offsets(source)
    import_map = {rule.old_module: rule.new_module for rule in import_rewrites}
    keyword_rules = tuple(keyword_rewrites)
    attribute_map = {rule.old_chain: rule.new_chain for rule in attribute_rewrites}
    edits: list[SourceEdit] = []
    bound_names: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                replacement = import_map.get(alias.name)
                if replacement is None:
                    continue
                start, end = _span(alias, offsets)
                edit = _replace_within_span(
                    data,
                    start,
                    end,
                    alias.name,
                    replacement,
                    rule_id=f"import:{alias.name}->{replacement}",
                    priority=90,
                )
                if edit is not None:
                    edits.append(edit)
                if alias.asname is None:
                    bound_names[alias.name.split(".")[0]] = replacement.split(".")[0]

        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            replacement = import_map.get(node.module)
            if replacement is None:
                continue
            start, end = _span(node, offsets)
            edit = _replace_within_span(
                data,
                start,
                end,
                node.module,
                replacement,
                rule_id=f"from_import:{node.module}->{replacement}",
                priority=90,
            )
            if edit is not None:
                edits.append(edit)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain = dotted_name(node)
            if chain and chain in attribute_map:
                replacement = attribute_map[chain]
                start, end = _span(node, offsets)
                edits.append(
                    SourceEdit(
                        start=start,
                        end=end,
                        replacement=replacement.encode("utf-8"),
                        rule_id=f"attribute:{chain}->{replacement}",
                        priority=100,
                    )
                )

        if isinstance(node, ast.Call):
            function_name = dotted_name(node.func)
            if function_name:
                effective_name = _rewrite_root(function_name, bound_names)
                for rule in keyword_rules:
                    if rule.function_name not in {function_name, effective_name}:
                        continue
                    for keyword in node.keywords:
                        if keyword.arg != rule.old_keyword:
                            continue
                        start, end = _span(keyword, offsets)
                        edit = _replace_within_span(
                            data,
                            start,
                            end,
                            rule.old_keyword,
                            rule.new_keyword,
                            rule_id=(
                                f"keyword:{effective_name}:"
                                f"{rule.old_keyword}->{rule.new_keyword}"
                            ),
                            priority=90,
                        )
                        if edit is not None:
                            edits.append(edit)

        if isinstance(node, ast.Name) and node.id in bound_names:
            replacement = bound_names[node.id]
            start, end = _span(node, offsets)
            edits.append(
                SourceEdit(
                    start=start,
                    end=end,
                    replacement=replacement.encode("utf-8"),
                    rule_id=f"bound_name:{node.id}->{replacement}",
                    priority=50,
                )
            )

    selected = _non_overlapping(edits)
    output = data
    for edit in reversed(selected):
        output = output[: edit.start] + edit.replacement + output[edit.end :]

    applied: list[str] = []
    for edit in selected:
        if edit.rule_id not in applied:
            applied.append(edit.rule_id)
    return output.decode("utf-8"), tuple(applied)


def conservative_example(source: str) -> tuple[str, tuple[str, ...]]:
    """Small example rule pack used by documentation/tests."""
    return transform_source(
        source,
        import_rewrites=(
            ImportRewrite("legacy_client", "modern_client"),
        ),
        keyword_rewrites=(
            KeywordRewrite("modern_client.request", "timeout_seconds", "timeout"),
        ),
        attribute_rewrites=(
            AttributeRewrite("legacy_client.errors.Timeout", "modern_client.TimeoutError"),
        ),
    )
