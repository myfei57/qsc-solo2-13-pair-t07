"""死代码审查。

按符号扫描 ``flashsmelter/``：列出定义了但从未被引用的模块级函数、类、方法与
配置字段。引用判定用 AST 收集名字与属性名，再排除定义处自身，因此会保守地报出
候选，由人工确认（例如同名符号可能掩盖真实引用）。

用法::

    python tools/dead_code_review.py
    python tools/dead_code_review.py --json
    python tools/dead_code_review.py --strict
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = PACKAGE_ROOT / "flashsmelter"
TESTS = PACKAGE_ROOT / "tests"

ENTRY_POINTS = {
    ("flashsmelter/__main__.py", "<module>"),
    ("flashsmelter/cli.py", "main"),
    ("flashsmelter/cli.py", "build_parser"),
    ("flashsmelter/console/__init__.py", "ConsoleServer"),
    ("flashsmelter/console/__init__.py", "ConsoleApp"),
    ("flashsmelter/application.py", "Application"),
    ("flashsmelter/application.py", "build_application"),
}


@dataclass
class Definition:
    module: str
    kind: str
    name: str
    qualified: str
    line: int
    private: bool = False


@dataclass
class Usage:
    per_module: dict[str, set[str]] = field(default_factory=dict)

    def add(self, module: str, token: str) -> None:
        self.per_module.setdefault(module, set()).add(token)


def _module_name(path: Path) -> str:
    return str(path.relative_to(PACKAGE_ROOT)).replace("\\", "/")


def _iter_python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _collect_definitions(path: Path, tree: ast.AST) -> list[Definition]:
    module = _module_name(path)
    definitions: list[Definition] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append(
                Definition(module, "function", node.name, node.name, node.lineno, node.name.startswith("_"))
            )
        elif isinstance(node, ast.ClassDef):
            definitions.append(
                Definition(module, "class", node.name, node.name, node.lineno, node.name.startswith("_"))
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f"{node.name}.{child.name}"
                    definitions.append(
                        Definition(
                            module, "method", child.name, qualified, child.lineno, child.name.startswith("_")
                        )
                    )
    return definitions


def _collect_usage(path: Path, tree: ast.AST) -> tuple[str, list[str]]:
    module = _module_name(path)
    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens.append(node.value)
    return module, tokens


def _ast_of(path: Path, cache: dict[Path, ast.AST]) -> ast.AST:
    if path not in cache:
        cache[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return cache[path]


def _config_fields() -> list[str]:
    tree = _ast_of(PACKAGE / "config.py", {})
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return [
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
            ]
    return []


def _count_same_file_references(definition: Definition, cache: dict[Path, ast.AST]) -> int:
    path = PACKAGE_ROOT / definition.module
    tree = _ast_of(path, cache)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == definition.name and node.lineno != definition.line:
            count += 1
        elif isinstance(node, ast.Name) and node.id == definition.name and node.lineno != definition.line:
            count += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == definition.name:
            count += 1
    return count


def _field_is_used(field_name: str, cache: dict[Path, ast.AST]) -> bool:
    for path in _iter_python_files(PACKAGE):
        if path == PACKAGE / "config.py":
            continue
        tree = _ast_of(path, cache)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == field_name:
                return True
            if isinstance(node, ast.Constant) and node.value == field_name:
                return True
    return False


def review() -> dict:
    cache: dict[Path, ast.AST] = {}
    definitions: list[Definition] = []
    usage = Usage()
    packages = list(_iter_python_files(PACKAGE))
    for path in packages:
        tree = _ast_of(path, cache)
        definitions.extend(_collect_definitions(path, tree))
        module, tokens = _collect_usage(path, tree)
        for token in tokens:
            usage.add(module, token)
    test_modules = []
    for path in _iter_python_files(TESTS):
        tree = _ast_of(path, cache)
        module, tokens = _collect_usage(path, tree)
        test_modules.append(module)
        for token in tokens:
            usage.add(module, token)

    unused: list[dict] = []
    for definition in definitions:
        # 双下划线协议方法由运行时隐式调用（__post_init__、__str__ 等），不做引用统计。
        if definition.name.startswith("__") and definition.name.endswith("__"):
            continue
        if (definition.module, definition.qualified) in ENTRY_POINTS:
            continue
        if (definition.module, definition.name) in ENTRY_POINTS:
            continue
        external = 0
        for module, tokens in usage.per_module.items():
            if module == definition.module:
                continue
            if definition.qualified in tokens or definition.name in tokens:
                external += 1
        same_file = _count_same_file_references(definition, cache)
        if external == 0 and same_file == 0:
            unused.append(
                {
                    "module": definition.module,
                    "kind": definition.kind,
                    "name": definition.qualified,
                    "line": definition.line,
                    "private": definition.private,
                }
            )

    config_unused = [name for name in _config_fields() if not _field_is_used(name, cache)]
    return {
        "modules": len(packages),
        "definitions": len(definitions),
        "test_modules": len(test_modules),
        "unused_symbols": unused,
        "unused_config_fields": config_unused,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描 flashsmelter 包中的死代码候选")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="存在候选时返回非零")
    args = parser.parse_args(argv)
    report = review()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"扫描模块 {report['modules']} 个，定义符号 {report['definitions']} 个，"
            f"测试模块 {report['test_modules']} 个"
        )
        if report["unused_symbols"]:
            print("未被引用的符号：")
            for item in report["unused_symbols"]:
                print(f"  - {item['module']}:{item['line']} {item['kind']} {item['name']}")
        else:
            print("未被引用的符号：无")
        if report["unused_config_fields"]:
            print("未被使用的配置字段：")
            for name in report["unused_config_fields"]:
                print(f"  - Settings.{name}")
        else:
            print("未被使用的配置字段：无")
    if args.strict and (report["unused_symbols"] or report["unused_config_fields"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
