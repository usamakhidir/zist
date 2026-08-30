
from __future__ import annotations

import ast
import math
import operator

SCORING_METHODS = {
    "BINARY": "Binary / Completion",
    "TARGET": "Target Attainment",
    "AT_MOST": "At-Most Target",
    "RANGE": "Optimal Range",
    "FREQUENCY": "Frequency / Count",
    "RATING": "Rating",
}

FORMULA_FUNCTIONS = {
    "MIN": min,
    "MAX": max,
    "ABS": abs,
    "ROUND": round,
}

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}

_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def calculate_score(
    method: str,
    actual: float | None,
    *,
    target: float | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    rating_max: float | None = None,
    max_points: float = 10.0,
) -> dict:
    method = method.upper()

    if actual is None:
        return {"achievement_pct": None, "points": None, "status": "No data"}

    actual = float(actual)
    max_points = max(0.0, float(max_points))

    if method == "BINARY":
        achievement = 1.0 if actual > 0 else 0.0

    elif method == "TARGET":
        if target is None or target <= 0:
            raise ValueError("Target Attainment requires a positive target.")
        achievement = _clamp(actual / target)

    elif method == "AT_MOST":
        if target is None or target <= 0:
            raise ValueError("At-Most Target requires a positive target.")
        achievement = 1.0 if actual <= target else _clamp(target / actual)

    elif method == "RANGE":
        if min_value is None or max_value is None or min_value > max_value:
            raise ValueError("Optimal Range requires valid minimum and maximum values.")
        if min_value == max_value:
            achievement = 1.0 if actual == min_value else 0.0
        elif min_value <= actual <= max_value:
            achievement = 1.0
        elif actual < min_value:
            achievement = _clamp(actual / min_value) if min_value > 0 else 0.0
        else:
            achievement = _clamp(max_value / actual) if actual > 0 else 0.0

    elif method == "FREQUENCY":
        if target is None or target <= 0:
            raise ValueError("Frequency / Count requires a positive target.")
        achievement = _clamp(actual / target)

    elif method == "RATING":
        if rating_max is None or rating_max <= 0:
            raise ValueError("Rating requires a positive rating maximum.")
        achievement = _clamp(actual / rating_max)

    else:
        raise ValueError(f"Unknown scoring method: {method}")

    points = achievement * max_points
    status = "Full" if achievement >= 0.999999 else ("None" if achievement <= 0 else "Partial")

    return {
        "achievement_pct": achievement * 100,
        "points": points,
        "status": status,
    }


class FormulaError(ValueError):
    pass


def evaluate_formula(formula: str, variables: dict[str, float | None]) -> float | None:
    """
    Evaluate a controlled arithmetic formula.

    Supported:
      + - * / %
      parentheses
      MIN(), MAX(), ABS(), ROUND()
      named metric variables

    No Python attribute access, imports, comprehensions, lambdas, or arbitrary
    function calls are permitted.
    """
    if not formula or not formula.strip():
        raise FormulaError("Formula is empty.")

    normalized = {str(k): v for k, v in variables.items()}
    if any(v is None for v in normalized.values()):
        return None

    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise FormulaError("Only numeric constants are allowed.")

        if isinstance(node, ast.Name):
            if node.id not in normalized:
                raise FormulaError(f"Unknown metric variable: {node.id}")
            value = normalized[node.id]
            if value is None:
                return None
            return float(value)

        if isinstance(node, ast.BinOp):
            left = visit(node.left)
            right = visit(node.right)
            if left is None or right is None:
                return None
            operation = _ALLOWED_BINOPS.get(type(node.op))
            if operation is None:
                raise FormulaError("That arithmetic operator is not supported.")
            if isinstance(node.op, ast.Div) and right == 0:
                raise FormulaError("Division by zero.")
            return float(operation(left, right))

        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            operation = _ALLOWED_UNARYOPS.get(type(node.op))
            if operation is None:
                raise FormulaError("That unary operator is not supported.")
            return float(operation(value))

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FORMULA_FUNCTIONS:
                raise FormulaError("Only MIN, MAX, ABS and ROUND are supported.")
            if node.keywords:
                raise FormulaError("Named function arguments are not supported.")
            args = [visit(arg) for arg in node.args]
            if any(arg is None for arg in args):
                return None
            try:
                return float(FORMULA_FUNCTIONS[node.func.id](*args))
            except Exception as exc:
                raise FormulaError(str(exc)) from exc

        raise FormulaError("This formula contains an unsupported expression.")

    result = visit(tree)
    if not math.isfinite(result):
        raise FormulaError("Formula result must be finite.")
    return result
