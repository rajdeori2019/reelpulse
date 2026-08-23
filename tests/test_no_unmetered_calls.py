"""No module may reach the network without going through the limiter.

This is the test I wish had existed earlier. Two call sites — the Instagram
setup doctor and Reddit's OAuth token exchange — were written outside the
limiter and stayed that way for weeks, because nothing was watching. Both were
low-volume, which is exactly why nobody noticed; and the auth endpoint is the
single worst place to be un-throttled, since a retry loop there is what gets an
app blocked rather than merely slowed.

So this walks the source with the AST and asserts the invariant structurally: if
a function sends an HTTP request, that same function must also acquire budget
first. It fails on code that does not exist yet, which is the only kind of
protection worth having here.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PACKAGE = ROOT / "reelpulse"

# Anything that puts bytes on the wire.
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "request",
              "urlopen", "send"}
HTTP_OWNERS = {"requests", "session", "http", "httpx", "urllib", "client",
               "urlopen"}

# limits.py is the accounting itself; it makes no calls of its own, but if it
# ever grows one it should not be asked to meter itself.
EXEMPT_FILES = {"limits.py"}


def _http_call_name(node: ast.Call) -> str | None:
    """Return a readable name if this call sends a request, else None."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in HTTP_VERBS:
        owner = func.value
        # requests.get(...) / httpx.post(...)
        if isinstance(owner, ast.Name) and owner.id in HTTP_OWNERS:
            return f"{owner.id}.{func.attr}"
        # self.session.get(...) / self._client.post(...)
        if isinstance(owner, ast.Attribute) and owner.attr in HTTP_OWNERS:
            return f"{owner.attr}.{func.attr}"
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return "urlopen"
    return None


def _acquires_budget(fn: ast.AST) -> bool:
    """Does this function ask the limiter for permission anywhere inside it?"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "acquire":
                return True
    return False


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _sources():
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in EXEMPT_FILES:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_every_http_call_site_acquires_budget_first():
    offenders = []
    for path, tree in _sources():
        for fn in _functions(tree):
            calls = [(_http_call_name(n), n.lineno) for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and _http_call_name(n)]
            if calls and not _acquires_budget(fn):
                rel = path.relative_to(ROOT)
                for name, line in calls:
                    offenders.append(f"{rel}:{line} {fn.name}() calls {name}()")

    assert not offenders, (
        "these functions send HTTP requests without acquiring budget first:\n  "
        + "\n  ".join(offenders)
        + "\n\nRoute the call through Collector.get_json(), or bracket it with "
          "limiter.acquire(...) before and limiter.observe(...) after."
    )


def test_every_http_call_site_records_the_outcome():
    """acquire() alone is not enough: budget asked for and never booked leaves
    the ledger reading low, which is worse than no ledger — it licenses the next
    run to spend money that is already gone."""
    offenders = []
    for path, tree in _sources():
        for fn in _functions(tree):
            if not any(_http_call_name(n) for n in ast.walk(fn)
                       if isinstance(n, ast.Call)):
                continue
            books = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"observe", "record"}
                for n in ast.walk(fn))
            if not books:
                offenders.append(f"{path.relative_to(ROOT)} {fn.name}()")

    assert not offenders, (
        "these functions spend budget but never book it:\n  "
        + "\n  ".join(offenders))


def test_the_guard_would_actually_catch_a_regression(tmp_path):
    """A structural test that cannot fail is decoration. This proves it bites."""
    bad = tmp_path / "sneaky.py"
    bad.write_text("import requests\n"
                   "def fetch():\n"
                   "    return requests.get('https://example.com')\n")
    tree = ast.parse(bad.read_text())
    fns = list(_functions(tree))
    assert len(fns) == 1
    assert _http_call_name(next(n for n in ast.walk(fns[0])
                                if isinstance(n, ast.Call))) == "requests.get"
    assert not _acquires_budget(fns[0])


@pytest.mark.parametrize("snippet,expected", [
    ("requests.post(url)", "requests.post"),
    ("self.session.get(url)", "session.get"),
    ("httpx.request('GET', url)", "httpx.request"),
    ("urlopen(url)", "urlopen"),
    ("d.get('k')", None),          # dict access is not a request
    ("re.match(p, s)", None),
])
def test_the_detector_tells_requests_from_lookalikes(snippet, expected):
    """`.get()` is the most overloaded method name in Python. A guard that
    cannot tell a dict lookup from an HTTP GET would be ignored within a week."""
    call = ast.parse(snippet).body[0].value
    assert _http_call_name(call) == expected
