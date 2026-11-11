"""Macro data model: pure JSON, no classes.

Macros are dicts on purpose. A learned macro is data that the runner reads, the
healer patches, and the store writes back — keeping it plain JSON means healing
is a two-line edit instead of a serialization problem, and it means a user can
read or hand-fix a macro with any editor.

Shape (schema_version 1)::

    {
      "schema_version": 1,
      "name": "invoice_entry",
      "description": "...",
      "source_url": "https://app.example.com/invoices/new",
      "params": [{"name": "customer", "type": "string", "default": "Acme"}],
      "steps": [
        {"id": 1, "action": "goto", "url": "...", "checkpoint": true,
         "verify": {"kind": "url_contains", "value": "/invoices"}},
        {"id": 2, "action": "fill", "param": "customer", "checkpoint": true,
         "target": {"strategies": [{"kind": "label", "value": "Customer", "score": 90}]},
         "verify": {"kind": "value", "param": "customer"}}
      ],
      "postconditions": [],
      "strict": true
    }
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

# Names the tool surface already uses; a recorded field may not steal them.
RESERVED_PARAMS = {
    "rows",
    "name",
    "macro",
    "params",
    "resume",
    "heal",
    "timeout_ms",
    "url",
    "page",
    "run_id",
}

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(value: Any) -> str:
    """snake_case identifier safe for file names, tool names and params."""
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    text = re.sub(r"_+", "_", text)
    if not text:
        return ""
    if not text[0].isalpha():
        text = "f_" + text
    return text[:48]


def tool_name(macro_name: str) -> str:
    """The tool the model sees for a macro: ``mimic_<name>``."""
    base = slug(macro_name) or "macro"
    return base if base.startswith("mimic_") else f"mimic_{base}"


def macro_name_from_tool(name: str) -> str:
    text = slug(name)
    return text[len("mimic_") :] if text.startswith("mimic_") else text


def render(value: Any, params: Dict[str, Any]) -> Any:
    """Substitute ``{{param}}`` placeholders inside a recorded literal."""
    if not isinstance(value, str):
        return value
    if "{{" not in value:
        return value
    return _TEMPLATE_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)


def new_macro(
    *,
    name: str,
    description: str,
    source_url: str,
    params: List[Dict[str, Any]],
    steps: List[Dict[str, Any]],
    strict: bool = True,
    final_url: str = "",
    title: str = "",
    recorded_events: int = 0,
) -> Dict[str, Any]:
    stamp = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "name": slug(name) or "macro",
        "description": description,
        "source_url": source_url,
        "final_url": final_url,
        "page_title": title,
        "params": params,
        "steps": steps,
        "postconditions": [],
        "strict": bool(strict),
        "compiled_at": stamp,
        "updated_at": stamp,
        "recorded_events": recorded_events,
        "stats": {"runs": 0, "ok": 0, "heals": 0, "llm_calls": 0, "last_run_at": "", "last_status": ""},
    }


def validate(data: Any) -> List[str]:
    """Return a list of problems; empty means the macro is runnable."""
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["macro is not an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {data.get('schema_version')!r}")
    if not slug(data.get("name")):
        problems.append("name is empty")
    params = data.get("params")
    if params is not None and not isinstance(params, list):
        problems.append("params must be a list")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps must be a non-empty list")
        return problems
    seen_ids = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"step {index} is not an object")
            continue
        step_id = step.get("id")
        if not isinstance(step_id, int):
            problems.append(f"step {index} has no integer id")
        elif step_id in seen_ids:
            problems.append(f"duplicate step id {step_id}")
        seen_ids.add(step_id)
        action = step.get("action")
        if action not in ("goto", "click", "fill", "select", "check", "uncheck", "press", "wait"):
            problems.append(f"step {step_id} has unknown action {action!r}")
        if action == "goto" and not step.get("url"):
            problems.append(f"step {step_id} goto has no url")
        if action not in ("goto", "wait"):
            strategies = (step.get("target") or {}).get("strategies") or []
            if not strategies:
                problems.append(f"step {step_id} has no selector strategies")
    return problems


def params_with_defaults(macro: Dict[str, Any], given: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for param in macro.get("params") or []:
        values[param["name"]] = param.get("default")
    for key, value in (given or {}).items():
        if value is not None:
            values[str(key)] = value
    return values


def required_params(macro: Dict[str, Any]) -> List[str]:
    return [p["name"] for p in macro.get("params") or [] if p.get("required")]


def _json_type(param_type: str) -> str:
    return {"number": "number", "integer": "integer", "boolean": "boolean"}.get(param_type or "string", "string")


def tool_schema(macro: Dict[str, Any]) -> Dict[str, Any]:
    """The live tool's schema: typed params from the macro, plus batch ``rows``."""
    properties: Dict[str, Any] = {}
    for param in macro.get("params") or []:
        entry: Dict[str, Any] = {
            "type": _json_type(param.get("type", "string")),
            "description": param.get("description") or f"Value for {param['name']}",
        }
        if param.get("default") not in (None, ""):
            entry["default"] = param["default"]
        properties[param["name"]] = entry

    row_properties = {k: dict(v) for k, v in properties.items()}
    properties["rows"] = {
        "type": "array",
        "description": (
            "Optional batch: one object per record, each overriding the parameters above. "
            "Every row replays the same recorded workflow with zero model calls."
        ),
        "items": {"type": "object", "properties": row_properties},
    }
    properties["resume"] = {
        "type": "boolean",
        "description": "Resume from the last failed checkpoint for this macro instead of starting over.",
        "default": False,
    }
    properties["heal"] = {
        "type": "boolean",
        "description": "Allow one model re-grounding call if a step breaks (default: plugin setting).",
    }
    schema: Dict[str, Any] = {
        "name": tool_name(macro.get("name", "")),
        "description": tool_description(macro),
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required_params(macro),
        },
    }
    return schema


def tool_description(macro: Dict[str, Any]) -> str:
    steps = macro.get("steps") or []
    params = [p["name"] for p in macro.get("params") or []]
    source = macro.get("source_url") or "the recorded site"
    recorded = ", ".join(f'"{name}"' for name in params) if params else "no parameters"
    description = macro.get("description") or (
        f"Replays the browser workflow recorded on {source} ({len(steps)} steps)."
    )
    return (
        f"{description} Runs deterministically with zero model calls — use it instead of "
        f"clicking through {source} by hand again. Recorded parameters: {recorded}. "
        f"Pass `rows` to run the same workflow over many records in one call."
    )


def macro_summary(macro: Dict[str, Any]) -> Dict[str, Any]:
    stats = dict(macro.get("stats") or {})
    return {
        "name": macro.get("name"),
        "tool": tool_name(macro.get("name", "")),
        "description": (macro.get("description") or "")[:200],
        "steps": len(macro.get("steps") or []),
        "params": [p["name"] for p in macro.get("params") or []],
        "source_url": macro.get("source_url"),
        "updated_at": macro.get("updated_at"),
        "stats": stats,
    }


def iter_targets(macro: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for step in macro.get("steps") or []:
        target = step.get("target")
        if isinstance(target, dict):
            yield target
