import ast
import re
from pathlib import Path

# The only allowed occurrence: reading Yandex's flag that the buyer asked to drop the order.
# The robot never sends such a request; it only skips flagged orders.
ALLOWED = {'BUYER_STOP_FLAG = "cancelRequested"  # read-only flag: the buyer asked to drop the order'}

# State name for an order cancelled by a human: a string, not a cancel call.
STATE_NAMES = ("CANCELLED_BY_HUMAN",)


def _has_cancel(line: str) -> bool:
    for name in STATE_NAMES:
        line = line.replace(name, "")
    return bool(re.search(r"cancel", line, re.I))


SRC = Path(__file__).parent.parent / "src"
ORDERBOT = SRC / "orderbot"


def _allowed_ranges(p: Path) -> list[tuple[int, int]]:
    """Line ranges of `cancel_by_human` methods and `*CancelError` classes in a marketplace
    module, found with ast (no text heuristics)."""
    tree = ast.parse(p.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "cancel_by_human":
            out.append((node.lineno, node.end_lineno))
        elif isinstance(node, ast.ClassDef) and node.name.endswith("CancelError"):
            out.append((node.lineno, node.end_lineno))
    return out


def test_src_has_no_cancel_calls():
    files = list(SRC.rglob("*.py"))
    assert files
    for p in files:
        if p.name == "alerts.py" and p.parent.name == "orderbot":
            continue  # pure text module: result_line kinds only, see test below
        if p == ORDERBOT / "bot.py":
            continue  # names the human cancel button; it reaches cancelling only via HumanActions
        if p == ORDERBOT / "human_actions.py":
            continue  # the single call site of the human buttons
        ranges = _allowed_ranges(p) if p.parent == ORDERBOT / "marketplaces" else []
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if line.strip() in ALLOWED:
                continue
            if any(a <= n <= b for a, b in ranges):
                continue
            assert not _has_cancel(line), (p, n, line)


def test_pipeline_and_rules_never_call_cancel():
    for name in ("pipeline.py", "rules.py", "main.py", "bot.py"):
        f = ORDERBOT / name
        assert f.exists(), f
        assert "cancel_by_human" not in f.read_text(), f


def test_pipeline_and_rules_never_import_human_actions():
    for name in ("pipeline.py", "rules.py"):
        f = ORDERBOT / name
        assert f.exists(), f
        assert "human_actions" not in f.read_text(), f


def test_state_name_is_not_a_cancel_call():
    assert not _has_cancel('STATES = {"CANCELLED_BY_HUMAN"}')
    assert _has_cancel("await mp.cancel_order(x)  # CANCELLED_BY_HUMAN")


def test_alerts_module_has_no_api_access():
    src = (Path(__file__).parent.parent / "src" / "orderbot" / "alerts.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+httpx", src, re.M)
    assert not re.search(r"^\s*(import|from)\s+orderbot\.(marketplaces|pipeline)", src, re.M)
    assert not re.search(r"^\s*from\s+orderbot\s+import\s+.*\b(marketplaces|pipeline)\b", src, re.M)


def test_bot_module_cannot_reach_marketplaces():
    src = (ORDERBOT / "bot.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+httpx", src, re.M)
    assert not re.search(r"^\s*(import|from)\s+orderbot\.marketplaces", src, re.M)
    assert not re.search(r"^\s*from\s+orderbot\s+import\s+.*\bmarketplaces\b", src, re.M)
