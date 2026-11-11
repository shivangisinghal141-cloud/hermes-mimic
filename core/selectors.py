"""The selector engine — the whole product lives here.

Six strategies, tried in a stability order:

    testid  100  data-testid / data-test / data-qa / data-cy
    css      98  ``#id`` when the id looks authored (not a generated hash)
    role     95  accessible role + accessible name
    label    90  form control label
    text     80  exact visible text (buttons, links, headings)
    css      70  structural CSS path
    xpath    50  absolute XPath, last resort

A strategy only counts as a match when it resolves to *exactly one* element.
"0 matches" is a miss; "3 matches" is ambiguous and also a miss, because a
macro that clicks the first of three buttons is a bug waiting for a demo.

When the primary strategy misses but a later one matches, the step is reported
as ``degraded``: the page drifted, and strict mode repairs the primary selector
instead of quietly leaning on a fuzzy fallback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Stability score per strategy kind. Higher = tried first, and preferred by the
# compiler when it builds a target from a recorded descriptor.
KIND_PRIORITY: Dict[str, int] = {
    "testid": 100,
    "css": 70,  # bumped to 98 for authored ids, see strategy()
    "role": 95,
    "label": 90,
    "text": 80,
    "xpath": 50,
}

STRATEGY_KINDS = ("testid", "role", "label", "text", "css", "xpath")

_ID_SHAPE = None
_ID_GENERATED = None
_ID_FRAMEWORKS = (
    "react-",
    "radix-",
    "mui-",
    "headlessui-",
    "chakra-",
    "ant-",
    "el-",
    "rc_",
    "ember",
    "ext-gen",
    "yui_",
    "gwt-",
    "downshift-",
    "select2-",
    "j_id",
    "vue-",
    "svelte-",
)


def is_authored_id(value: str) -> bool:
    """True when a value is a hand-written element id (not a selector or a hash).

    Framework ids such as ``react-select-7-input`` or ``:r1a:`` change between
    renders; treating them as identity would make every run "drift" and call the
    model for no reason. Structural CSS (``input[name=q]``) is not an id either.
    """
    import re

    global _ID_SHAPE, _ID_GENERATED
    if _ID_SHAPE is None:
        _ID_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
        _ID_GENERATED = re.compile(r"[0-9a-f]{8,}|[0-9]{4,}|--[0-9a-z]{6,}$", re.IGNORECASE)

    candidate = (value or "").strip()
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if not _ID_SHAPE.match(candidate):
        return False
    lowered = candidate.lower()
    if lowered.startswith(_ID_FRAMEWORKS):
        return False
    return _ID_GENERATED.search(candidate) is None


def strategy(kind: str, value: str, **extra: Any) -> Dict[str, Any]:
    """Build one selector candidate."""
    if kind not in STRATEGY_KINDS:
        raise ValueError(f"unknown selector kind: {kind!r}")
    score = KIND_PRIORITY[kind]
    if kind == "css" and is_authored_id(value):
        score = 98
    item: Dict[str, Any] = {"kind": kind, "value": value, "score": score}
    item.update({k: v for k, v in extra.items() if v not in (None, "")})
    return item


def sort_strategies(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order candidates by stability, keeping the author's order on ties."""
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (-int(pair[1].get("score", 0)), pair[0]))
    return [item for _, item in indexed]


def describe(item: Dict[str, Any]) -> str:
    """Short human label for logs and step reports."""
    kind = item.get("kind")
    if kind == "role":
        role = item.get("role") or item.get("value")
        name = item.get("name") or ""
        return f'role={role}[name="{name}"]' if name else f"role={role}"
    return f"{kind}={item.get('value')}"


def locator_for(page: Any, item: Dict[str, Any]) -> Any:
    """Turn a strategy dict into a Playwright locator."""
    kind = item.get("kind")
    value = item.get("value") or ""
    if kind == "role":
        role = item.get("role") or value
        name = item.get("name") or ""
        if name:
            return page.get_by_role(role, name=name, exact=True)
        return page.get_by_role(role)
    if kind == "label":
        return page.get_by_label(value, exact=True)
    if kind == "text":
        return page.get_by_text(value, exact=True)
    if kind == "testid":
        return page.get_by_test_id(value)
    if kind == "css":
        return page.locator(value)
    if kind == "xpath":
        return page.locator("xpath=" + value)
    raise ValueError(f"unknown selector kind: {kind!r}")


def normalize_candidate(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce a model-proposed selector into a strategy dict, or None."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    value = str(raw.get("value") or "").strip()
    if kind == "role":
        role = str(raw.get("role") or value or "").strip()
        if not role:
            return None
        return strategy("role", role, role=role, name=str(raw.get("name") or "").strip())
    if kind not in STRATEGY_KINDS or not value:
        return None
    return strategy(kind, value)


@dataclass
class Resolution:
    """Result of trying every strategy against a live page."""

    locator: Any = None
    strategy: Optional[Dict[str, Any]] = None
    degraded: bool = False
    tried: List[Dict[str, str]] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.locator is not None

    def summary(self) -> str:
        if self.locator is not None:
            head = describe(self.strategy or {})
            if self.degraded:
                return f"degraded: primary selector missed, matched via {head}"
            return f"matched {head}"
        if not self.tried:
            return "no selector strategies recorded"
        reasons = sorted({row["result"] for row in self.tried})
        return "no unique match (" + ", ".join(reasons) + ")"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "matched": describe(self.strategy) if self.strategy else None,
            "degraded": self.degraded,
            "elapsed_ms": self.elapsed_ms,
            "tried": self.tried[-12:],
        }


async def resolve(
    page: Any,
    strategies: List[Dict[str, Any]],
    *,
    timeout_ms: int = 4000,
    poll_ms: int = 120,
) -> Resolution:
    """Wait for one of the strategies to match exactly one element.

    Playwright's ``count()`` does not auto-wait, so this polls until the deadline
    and returns the first unique match in priority order.
    """
    started = time.monotonic()
    deadline = started + max(timeout_ms, 0) / 1000.0
    tried: List[Dict[str, str]] = []
    while True:
        tried = []
        for index, item in enumerate(strategies or []):
            label = describe(item)
            try:
                locator = locator_for(page, item)
                count = await locator.count()
            except Exception as exc:  # bad selector expression, detached frame, ...
                tried.append({"strategy": label, "result": f"error:{type(exc).__name__}"})
                continue
            if count == 1:
                tried.append({"strategy": label, "result": "unique"})
                return Resolution(
                    locator=locator,
                    strategy=item,
                    degraded=index != 0,
                    tried=tried,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            tried.append({"strategy": label, "result": "not_found" if count == 0 else f"ambiguous:{count}"})
        if time.monotonic() >= deadline:
            return Resolution(None, None, False, tried, int((time.monotonic() - started) * 1000))
        await asyncio.sleep(poll_ms / 1000.0)
