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


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def build_dotted_expr(name: str, *, ctx: ast.expr_context | None = None) -> ast.expr:
    parts = name.split(".")
    node: ast.expr = ast.Name(id=parts[0], ctx=ast.Load())
    for part in parts[1:]:
        node = ast.Attribute(value=node, attr=part, ctx=ast.Load())
    if ctx is not None and isinstance(node, (ast.Name, ast.Attribute)):
        node.ctx = ctx
    return node


class StructuralMigrationTransformer(ast.NodeTransformer):
    """AST transformer for migration rules that should not depend on formatting.

    Text replacement is useful for simple known changes, but it can accidentally
    rewrite comments, strings or unrelated identifiers. Structural rules target
    actual Python syntax nodes and therefore provide a safer deterministic layer.
    """

    def __init__(
        self,
        *,
        import_rewrites: Iterable[ImportRewrite] = (),
        keyword_rewrites: Iterable[KeywordRewrite] = (),
        attribute_rewrites: Iterable[AttributeRewrite] = (),
    ) -> None:
        self.import_rewrites = {rule.old_module: rule.new_module for rule in import_rewrites}
        self.keyword_rewrites = list(keyword_rewrites)
        self.attribute_rewrites = {
            rule.old_chain: rule.new_chain for rule in attribute_rewrites
        }
        self.applied: list[str] = []

    def visit_Import(self, node: ast.Import) -> ast.AST:
        aliases: list[ast.alias] = []
        changed = False
        for alias in node.names:
            replacement = self.import_rewrites.get(alias.name)
            if replacement:
                aliases.append(ast.alias(name=replacement, asname=alias.asname))
                self.applied.append(f"import:{alias.name}->{replacement}")
                changed = True
            else:
                aliases.append(alias)
        if changed:
            return ast.copy_location(ast.Import(names=aliases), node)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        node = self.generic_visit(node)
        if not isinstance(node, ast.ImportFrom):
            return node
        if node.level == 0 and node.module in self.import_rewrites:
            old = node.module or ""
            new = self.import_rewrites[old]
            self.applied.append(f"from_import:{old}->{new}")
            return ast.copy_location(
                ast.ImportFrom(module=new, names=node.names, level=0),
                node,
            )
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node = self.generic_visit(node)
        if not isinstance(node, ast.Attribute):
            return node
        chain = dotted_name(node)
        if chain and chain in self.attribute_rewrites:
            replacement = self.attribute_rewrites[chain]
            self.applied.append(f"attribute:{chain}->{replacement}")
            return ast.copy_location(build_dotted_expr(replacement, ctx=node.ctx), node)
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        if not isinstance(node, ast.Call):
            return node

        function_name = dotted_name(node.func)
        if not function_name:
            return node

        for rule in self.keyword_rewrites:
            if function_name != rule.function_name:
                continue
            for keyword in node.keywords:
                if keyword.arg == rule.old_keyword:
                    keyword.arg = rule.new_keyword
                    self.applied.append(
                        f"keyword:{function_name}:{rule.old_keyword}->{rule.new_keyword}"
                    )
        return node


def transform_source(
    source: str,
    *,
    import_rewrites: Iterable[ImportRewrite] = (),
    keyword_rewrites: Iterable[KeywordRewrite] = (),
    attribute_rewrites: Iterable[AttributeRewrite] = (),
) -> tuple[str, tuple[str, ...]]:
    tree = ast.parse(source)
    transformer = StructuralMigrationTransformer(
        import_rewrites=import_rewrites,
        keyword_rewrites=keyword_rewrites,
        attribute_rewrites=attribute_rewrites,
    )
    transformed = transformer.visit(tree)
    ast.fix_missing_locations(transformed)
    output = ast.unparse(transformed)
    if source.endswith("\n"):
        output += "\n"
    return output, tuple(transformer.applied)


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
