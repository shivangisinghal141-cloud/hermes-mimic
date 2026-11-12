"""Export a compiled MIMIC macro as an importable n8n workflow JSON.

Every mimic step maps to one n8n Playwright node so the workflow is a
1-to-1 translation of what MIMIC recorded — no logic is lost and no extra
nodes are added beyond a trigger and an optional defaults Set node.

Usage (tool surface)::

    mimic_export_n8n(name="invoice_entry", path="/tmp/invoice_entry.n8n.json")

Then in n8n: Workflows → Import from file → pick the JSON.
Swap the Manual Trigger for a Schedule, Webhook, or Spreadsheet File trigger
to drive the workflow with live data.

Params become ``={{ $json.<param_name> }}`` expressions so they are filled
by whatever data the trigger provides.  The first node is a "Set" node that
supplies the recorded defaults; a trigger with its own fields overrides them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .macro import tool_name

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _id() -> str:
    return str(uuid.uuid4())


def _pos(index: int, row: int = 0) -> List[int]:
    """Simple left-to-right layout: 220px per node, 200px between rows."""
    return [index * 220, row * 200]


def _node(
    name: str,
    node_type: str,
    parameters: Dict[str, Any],
    position: List[int],
    *,
    type_version: int = 1,
    node_id: Optional[str] = None,
    notes: str = "",
) -> Dict[str, Any]:
    n: Dict[str, Any] = {
        "id": node_id or _id(),
        "name": name,
        "type": node_type,
        "typeVersion": type_version,
        "position": position,
        "parameters": parameters,
    }
    if notes:
        n["notes"] = notes
    return n


# ---------------------------------------------------------------------------
# selector → CSS string (best single selector from the ladder)
# ---------------------------------------------------------------------------

def _best_selector(step: Dict[str, Any]) -> str:
    """Pick the most stable selector string from a step's strategy ladder."""
    strategies = (step.get("target") or {}).get("strategies") or []
    for s in strategies:
        kind = s.get("kind")
        value = s.get("value") or ""
        if kind == "testid":
            return f'[data-testid="{value}"]'
        if kind == "css":
            return value
        if kind == "role":
            role = s.get("role") or value
            name_attr = s.get("name") or ""
            if name_attr:
                return f'[role="{role}"][aria-label="{name_attr}"]'
            return f'[role="{role}"]'
        if kind == "label":
            # Playwright label selector syntax — works in n8n's Playwright node
            return f'label={value}'
        if kind == "text":
            return f'text={value}'
        if kind == "xpath":
            return f'xpath={value}'
    return ""


# ---------------------------------------------------------------------------
# step → n8n node
# ---------------------------------------------------------------------------

def _step_to_node(step: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    action = step.get("action")
    step_id = step.get("id", index)

    if action == "goto":
        url = step.get("url") or ""
        return _node(
            name=f"Navigate ({step_id})",
            node_type="n8n-nodes-base.playwright",
            parameters={"operation": "navigate", "url": url},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: goto {url}",
        )

    if action == "wait":
        seconds = float(step.get("seconds") or 0.5)
        return _node(
            name=f"Wait ({step_id})",
            node_type="n8n-nodes-base.wait",
            parameters={"amount": seconds, "unit": "seconds"},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: wait {seconds}s",
        )

    selector = _best_selector(step)
    if not selector:
        return None  # no usable selector — skip

    if action == "click":
        return _node(
            name=f"Click ({step_id})",
            node_type="n8n-nodes-base.playwright",
            parameters={"operation": "click", "selector": selector},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: click {selector}",
        )

    if action in ("fill", "select"):
        param = step.get("param")
        if param:
            value_expr = f"={{{{ $json.{param} }}}}"
        else:
            value_expr = step.get("value") or ""
        op = "fill" if action == "fill" else "selectOption"
        return _node(
            name=f"{'Fill' if action == 'fill' else 'Select'} {param or selector} ({step_id})",
            node_type="n8n-nodes-base.playwright",
            parameters={"operation": op, "selector": selector, "value": value_expr},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: {action} param={param}",
        )

    if action in ("check", "uncheck"):
        return _node(
            name=f"{'Check' if action == 'check' else 'Uncheck'} ({step_id})",
            node_type="n8n-nodes-base.playwright",
            parameters={"operation": action, "selector": selector},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: {action}",
        )

    if action == "press":
        key = step.get("key") or "Enter"
        return _node(
            name=f"Press {key} ({step_id})",
            node_type="n8n-nodes-base.playwright",
            parameters={"operation": "press", "selector": selector, "key": key},
            position=_pos(index + 2),
            notes=f"mimic step {step_id}: press {key}",
        )

    return None  # unknown action


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def macro_to_n8n(macro: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a compiled MIMIC macro dict into an n8n workflow dict.

    The returned dict is valid n8n workflow JSON — write it to a .json file
    and import it directly (Workflows → Import from file).
    """
    name = macro.get("name") or "mimic_workflow"
    params = macro.get("params") or []
    steps = macro.get("steps") or []

    nodes: List[Dict[str, Any]] = []
    connections: Dict[str, Any] = {}

    # ---- 1. Manual trigger -------------------------------------------------
    trigger = _node(
        name="Manual Trigger",
        node_type="n8n-nodes-base.manualTrigger",
        parameters={},
        position=_pos(0),
        notes=(
            "Swap for Schedule Trigger, Webhook, or Spreadsheet File to drive "
            "this workflow with live data."
        ),
    )
    nodes.append(trigger)

    # ---- 2. Defaults node (Set) — supplies recorded default values ----------
    if params:
        assignments = [
            {
                "id": _id(),
                "name": p["name"],
                "value": p.get("default") or "",
                "type": "string",
            }
            for p in params
        ]
        defaults_node = _node(
            name="Defaults",
            node_type="n8n-nodes-base.set",
            parameters={
                "mode": "manual",
                "assignments": {"assignments": assignments},
                "options": {},
            },
            position=_pos(1),
            type_version=3,
            notes=(
                "Recorded default values. Override by passing fields from the "
                "trigger upstream. Each field is available as $json.<param_name>."
            ),
        )
        nodes.append(defaults_node)
        connections["Manual Trigger"] = {
            "main": [[{"node": "Defaults", "type": "main", "index": 0}]]
        }
        prev_name = "Defaults"
    else:
        prev_name = "Manual Trigger"

    # ---- 3. One node per step ----------------------------------------------
    step_nodes: List[Dict[str, Any]] = []
    for index, step in enumerate(steps):
        node = _step_to_node(step, index)
        if node is None:
            continue
        step_nodes.append(node)

    for i, node in enumerate(step_nodes):
        nodes.append(node)
        connections[prev_name] = {
            "main": [[{"node": node["name"], "type": "main", "index": 0}]]
        }
        prev_name = node["name"]

    # ---- 4. Wrap up --------------------------------------------------------
    workflow: Dict[str, Any] = {
        "name": tool_name(name),
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
        },
        "meta": {
            "mimic_macro": name,
            "mimic_source_url": macro.get("source_url") or "",
            "mimic_compiled_at": macro.get("compiled_at") or "",
            "templateCredsSetupCompleted": True,
        },
    }
    return workflow


def export_to_file(macro: Dict[str, Any], path: str) -> str:
    """Write an n8n workflow JSON file. Returns the absolute path written."""
    workflow = macro_to_n8n(macro)
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
    return str(out)
