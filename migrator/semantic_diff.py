from __future__ import annotations

import ast
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SymbolSignature:
    kind: str
    name: str
    signature: str


@dataclass(frozen=True)
class ApiDiff:
    added: tuple[SymbolSignature, ...]
    removed: tuple[SymbolSignature, ...]
    changed: tuple[tuple[SymbolSignature, SymbolSignature], ...]

    @property
    def breaking(self) -> bool:
        return bool(self.removed or self.changed)

    def as_dict(self) -> dict:
        return {
            "breaking": self.breaking,
            "added": [asdict(item) for item in self.added],
            "removed": [asdict(item) for item in self.removed],
            "changed": [
                {"before": asdict(before), "after": asdict(after)}
                for before, after in self.changed
            ],
        }


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    defaults_offset = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        prefix = "" if index >= len(args.posonlyargs) else "posonly:"
        defaulted = index >= defaults_offset
        parts.append(f"{prefix}{arg.arg}{'=?' if defaulted else ''}")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(f"kw:{arg.arg}{'=?' if default is not None else ''}")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"({', '.join(parts)})"


def public_api(source: str) -> dict[str, SymbolSignature]:
    tree = ast.parse(source)
    symbols: dict[str, SymbolSignature] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            symbols[node.name] = SymbolSignature("function", node.name, _function_signature(node))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
                    methods.append(f"{child.name}{_function_signature(child)}")
            symbols[node.name] = SymbolSignature("class", node.name, " | ".join(sorted(methods)))
    return symbols


def compare_public_api(before: str, after: str) -> ApiDiff:
    old = public_api(before)
    new = public_api(after)
    added = tuple(new[name] for name in sorted(new.keys() - old.keys()))
    removed = tuple(old[name] for name in sorted(old.keys() - new.keys()))
    changed_items = []
    for name in sorted(old.keys() & new.keys()):
        if old[name] != new[name]:
            changed_items.append((old[name], new[name]))
    return ApiDiff(added, removed, tuple(changed_items))
