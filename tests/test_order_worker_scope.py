"""Regression tests for names used by the order worker."""
import ast
import inspect
import textwrap
from pathlib import Path

from src.services.order_service import OrderService
from src.workers.order_worker import OrderWorker


def test_process_order_does_not_shadow_module_select_import():
    tree = ast.parse(textwrap.dedent(inspect.getsource(OrderWorker._process_order)))
    local_select_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "sqlalchemy"
        and any(alias.name == "select" for alias in node.names)
    ]

    assert local_select_imports == []


def test_giveaway_integration_orders_use_explicit_synthetic_ids():
    script_path = Path(__file__).parents[1] / "scripts" / "verify_giveaway_integration.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    order_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Order"
    ]

    assert order_calls
    assert all(any(keyword.arg == "id" for keyword in call.keywords) for call in order_calls)


def _direct_function_nodes(function_node):
    stack = list(ast.iter_child_nodes(function_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _imported_names(node):
    if isinstance(node, ast.Import):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [alias.asname or alias.name for alias in node.names]
    return []


def test_function_imports_do_not_shadow_module_imports():
    project_root = Path(__file__).parents[1]
    findings = []

    for path in (project_root / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        module_imports = {
            name
            for statement in tree.body
            for name in _imported_names(statement)
        }
        for function_node in ast.walk(tree):
            if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in _direct_function_nodes(function_node):
                for name in _imported_names(node):
                    if name in module_imports:
                        findings.append(
                            f"{path.relative_to(project_root)}:{node.lineno} "
                            f"{function_node.name} shadows {name}"
                        )

    assert findings == []


def test_admin_retry_locks_order_before_balance_debit():
    tree = ast.parse(textwrap.dedent(inspect.getsource(OrderService.retry_order)))
    locking_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
    ]

    assert locking_calls
