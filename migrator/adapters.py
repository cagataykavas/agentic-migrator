from __future__ import annotations

import ast
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceUnit:
    path: str
    language: str
    content: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    diagnostics: tuple[str, ...] = ()


class MigrationAdapter(ABC):
    language: str
    suffixes: tuple[str, ...]

    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.suffixes

    @abstractmethod
    def validate(self, source: str) -> ValidationResult:
        raise NotImplementedError

    def normalize(self, source: str) -> str:
        return source.replace("\r\n", "\n").replace("\r", "\n")


class PythonAdapter(MigrationAdapter):
    language = "python"
    suffixes = (".py",)

    def validate(self, source: str) -> ValidationResult:
        try:
            ast.parse(source)
        except SyntaxError as exc:
            location = f"line {exc.lineno}:{exc.offset}" if exc.lineno else "unknown location"
            return ValidationResult(False, (f"SyntaxError at {location}: {exc.msg}",))
        return ValidationResult(True)


class JavaAdapter(MigrationAdapter):
    language = "java"
    suffixes = (".java",)

    _type_pattern = re.compile(r"\b(class|interface|enum|record)\s+[A-Za-z_$][\w$]*")

    def validate(self, source: str) -> ValidationResult:
        diagnostics: list[str] = []
        depth = 0
        in_string = False
        escaped = False
        for char in source:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    diagnostics.append("closing brace appears before matching opening brace")
                    break

        if depth != 0:
            diagnostics.append(f"unbalanced braces: final depth={depth}")
        if in_string:
            diagnostics.append("unterminated string literal detected by lightweight validator")
        if source.strip() and not self._type_pattern.search(source):
            diagnostics.append("no class/interface/enum/record declaration found")
        return ValidationResult(not diagnostics, tuple(diagnostics))


class JavaScriptAdapter(MigrationAdapter):
    language = "javascript"
    suffixes = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")

    def validate(self, source: str) -> ValidationResult:
        pairs = {"(": ")", "[": "]", "{": "}"}
        closing = {value: key for key, value in pairs.items()}
        stack: list[str] = []
        quote: str | None = None
        escaped = False

        for char in source:
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'", "`"}:
                quote = char
            elif char in pairs:
                stack.append(char)
            elif char in closing and (not stack or stack.pop() != closing[char]):
                return ValidationResult(False, (f"unmatched delimiter: {char}",))

        diagnostics: list[str] = []
        if stack:
            diagnostics.append(f"unclosed delimiters: {''.join(stack)}")
        if quote is not None:
            diagnostics.append(f"unterminated quote: {quote}")
        return ValidationResult(not diagnostics, tuple(diagnostics))


class AdapterRegistry:
    def __init__(self, adapters: tuple[MigrationAdapter, ...] | None = None) -> None:
        self.adapters = adapters or (PythonAdapter(), JavaAdapter(), JavaScriptAdapter())

    def adapter_for(self, path: str | Path) -> MigrationAdapter:
        for adapter in self.adapters:
            if adapter.supports(path):
                return adapter
        raise ValueError(f"no migration adapter registered for {Path(path).suffix or '<no suffix>'}")

    def source_unit(self, path: str | Path, content: str) -> SourceUnit:
        adapter = self.adapter_for(path)
        normalized = adapter.normalize(content)
        return SourceUnit(str(path), adapter.language, normalized)

    def validate(self, path: str | Path, content: str) -> ValidationResult:
        return self.adapter_for(path).validate(content)

    def supported_suffixes(self) -> tuple[str, ...]:
        return tuple(sorted({suffix for adapter in self.adapters for suffix in adapter.suffixes}))
