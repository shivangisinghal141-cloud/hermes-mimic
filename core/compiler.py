"""Compile raw recorded events into a deterministic, parameterized macro.

This is where "a demo recording" becomes "a tool with a typed signature".

Three transforms happen here:

* **Parameterization.** A recorded ``fill`` value ("Acme Ltd") is promoted to a
  typed parameter with that value as its default, named after the field
  (``customer``, ``invoice_number``, ...). That is what turns "do this once"
  into "do this for these 8 rows".
* **A selector ladder.** Every target keeps up to six independent ways to find it,
  ordered by stability, so one renamed attribute does not end the macro.
* **Checkpoints and verification.** Every step is a checkpoint, and data steps
  verify their own effect (the input really holds this row's value).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .macro import RESERVED_PARAMS, new_macro, slug
from .selectors import sort_strategies, strategy

# Roles whose visible text is a stable name for the element.
TEXT_ROLES = {
    "button",
    "link",
    "heading",
    "tab",
    "menuitem",
    "option",
    "checkbox",
    "radio",
    "switch",
}


def url_key(url: str) -> str:
    """Short, stable part of a URL used for navigation assertions."""
    if not url:
        return ""
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return target if len(target) <= 120 else url


def _same_target(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if not left or not right:
        return False
    for key in ("css", "xpath"):
        if left.get(key) and left.get(key) == right.get(key):
            return True
    return False


def param_name(target: Dict[str, Any], used: set) -> str:
    """Name a recorded field: testid, name attr, id, label, placeholder, in that order."""
    for candidate in (
        target.get("testid"),
        target.get("nameAttr"),
        target.get("id"),
        target.get("label"),
        target.get("accessibleName"),
        target.get("placeholder"),
    ):
        name = slug(candidate)
        if name and name not in RESERVED_PARAMS and name not in used:
            return name

    base = slug(
        target.get("nameAttr")
        or target.get("id")
        or target.get("label")
        or target.get("placeholder")
        or target.get("tag")
    ) or "field"
    if base in RESERVED_PARAMS:
        base = f"{base}_value"
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def param_description(target: Dict[str, Any]) -> str:
    bits = []
    if target.get("label"):
        bits.append(f'label "{target["label"]}"')
    if target.get("placeholder"):
        bits.append(f'placeholder "{target["placeholder"]}"')
    if target.get("id"):
        bits.append(f'id "{target["id"]}"')
    what = target.get("tag") or "field"
    if target.get("type"):
        what += f' type="{target["type"]}"'
    return f"{what} on the recorded page" + (f" ({', '.join(bits)})" if bits else "")


def strategies_from_descriptor(descriptor: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the selector ladder for a recorded target."""
    out: List[Dict[str, Any]] = []

    testid = (descriptor.get("testid") or "").strip()
    if testid:
        out.append(strategy("testid", testid))

    css = (descriptor.get("css") or "").strip()
    if css:
        out.append(strategy("css", css))  # authored ids are scored 98 by strategy()

    role = (descriptor.get("role") or "").strip()
    name = (descriptor.get("accessibleName") or "").strip()
    if role and name:
        out.append(strategy("role", role, role=role, name=name))

    label = (descriptor.get("label") or "").strip()
    if label:
        out.append(strategy("label", label))

    text = (descriptor.get("text") or "").strip()
    if text and len(text) <= 120 and role in TEXT_ROLES:
        out.append(strategy("text", text))

    xpath = (descriptor.get("xpath") or "").strip()
    if xpath:
        out.append(strategy("xpath", xpath))

    return sort_strategies(out)


def compile_recording(
    *,
    name: str,
    events: List[Dict[str, Any]],
    start_url: str,
    final_url: str = "",
    title: str = "",
    description: str = "",
    strict: bool = True,
) -> Dict[str, Any]:
    """Turn recorded events into a macro dict."""
    events = [event for event in (events or []) if isinstance(event, dict)]
    events.sort(key=lambda event: event.get("ts") or 0)

    steps: List[Dict[str, Any]] = []
    params: List[Dict[str, Any]] = []
    used_names: set = set()

    def add(step: Dict[str, Any]) -> None:
        step["id"] = len(steps) + 1
        step["checkpoint"] = True
        steps.append(step)

    add({"action": "goto", "url": start_url, "verify": {"kind": "url_contains", "value": url_key(start_url)}})

    # Keep only replayable events.
    filtered: List[Dict[str, Any]] = []
    for index, event in enumerate(events):
        kind = event.get("kind")
        target = event.get("target") or {}
        if kind == "navigate":
            url = (event.get("url") or "").strip()
            if url and not url.startswith("about:"):
                filtered.append({**event, "url": url})
            continue
        if kind not in ("click", "fill", "select", "check", "press") or not target:
            continue

        following = events[index + 1] if index + 1 < len(events) else None
        # Clicking a <select> or a checkbox is the first half of "change the value";
        # the value event is the step worth keeping.
        if kind == "click" and following is not None:
            next_target = following.get("target") or {}
            if following.get("kind") in ("select", "check") and _same_target(target, next_target):
                continue

        # A debounced fill can flush twice for one field; keep the last value.
        if kind == "fill" and filtered:
            previous = filtered[-1]
            if previous.get("kind") == "fill" and _same_target(previous.get("target") or {}, target):
                filtered[-1] = event
                continue

        filtered.append(event)

    current_url = start_url
    for event in filtered:
        kind = event["kind"]

        if kind == "navigate":
            url = event["url"]
            if url == current_url:
                continue
            if steps and steps[-1].get("action") == "goto" and steps[-1].get("url") == url:
                continue
            add({"action": "goto", "url": url, "verify": {"kind": "url_contains", "value": url_key(url)}})
            current_url = url
            continue

        target = event["target"]
        strategies = strategies_from_descriptor(target)
        if not strategies:
            continue

        if kind == "click":
            add(
                {
                    "action": "click",
                    "target": {"strategies": strategies},
                    "verify": {"kind": "acted"},
                }
            )
            continue

        if kind == "press":
            add(
                {
                    "action": "press",
                    "key": event.get("key") or "Enter",
                    "target": {"strategies": strategies},
                    "verify": {"kind": "acted"},
                }
            )
            continue

        if kind == "check":
            add(
                {
                    "action": "check" if event.get("checked") else "uncheck",
                    "target": {"strategies": strategies},
                    "verify": {"kind": "checked", "value": bool(event.get("checked"))},
                }
            )
            continue

        if kind in ("fill", "select"):
            raw_value = event.get("value")
            text = "" if raw_value is None else str(raw_value)
            if text == "":
                add(
                    {
                        "action": kind,
                        "value": "",
                        "target": {"strategies": strategies},
                        "verify": {"kind": "value", "value": ""},
                    }
                )
                continue
            field = param_name(target, used_names)
            used_names.add(field)
            params.append(
                {
                    "name": field,
                    "type": "string",
                    "description": param_description(target),
                    "default": text,
                    "required": True,
                }
            )
            add(
                {
                    "action": kind,
                    "param": field,
                    "target": {"strategies": strategies},
                    "verify": {"kind": "value", "param": field},
                }
            )

    if not description:
        description = (
            f"Replays the browser workflow recorded on {url_key(start_url) or start_url} "
            f"({len(steps)} steps, {len(params)} parameterized fields)."
        )

    macro = new_macro(
        name=name,
        description=description,
        source_url=start_url,
        params=params,
        steps=steps,
        strict=strict,
        final_url=final_url,
        title=title,
        recorded_events=len(events),
    )
    return macro


def interactions(macro: Dict[str, Any]) -> int:
    """Number of steps that actually touch the page (everything but the first goto)."""
    return max(len([step for step in macro.get("steps") or [] if step.get("action") != "goto"]), 0)


def param_template(default: Optional[str]) -> str:
    return "" if not default else default
