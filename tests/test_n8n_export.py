"""Tests for the n8n workflow exporter (core/n8n.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimic.core.compiler import compile_recording
from mimic.core.n8n import export_to_file, macro_to_n8n


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

EVENTS = [
    {
        "kind": "fill",
        "ts": 1,
        "value": "Acme Ltd",
        "target": {
            "tag": "input",
            "label": "Customer",
            "accessibleName": "Customer",
            "role": "textbox",
            "css": "#customer",
            "xpath": "/html/body/form/input[1]",
        },
    },
    {
        "kind": "fill",
        "ts": 2,
        "value": "INV-001",
        "target": {
            "tag": "input",
            "label": "Invoice number",
            "accessibleName": "Invoice number",
            "role": "textbox",
            "css": "#invoice_number",
            "xpath": "/html/body/form/input[2]",
        },
    },
    {
        "kind": "select",
        "ts": 3,
        "value": "net30",
        "target": {
            "tag": "select",
            "label": "Terms",
            "accessibleName": "Terms",
            "role": "combobox",
            "css": "#terms",
            "xpath": "/html/body/form/select[1]",
        },
    },
    {
        "kind": "click",
        "ts": 4,
        "target": {
            "tag": "button",
            "id": "submit-btn",
            "accessibleName": "Submit",
            "role": "button",
            "text": "Submit",
            "css": "#submit-btn",
            "xpath": "/html/body/form/button",
        },
    },
]


@pytest.fixture()
def macro():
    return compile_recording(
        name="invoice_entry",
        events=EVENTS,
        start_url="https://example.com/invoices/new",
    )


# ---------------------------------------------------------------------------
# structure tests
# ---------------------------------------------------------------------------

def test_top_level_shape(macro):
    wf = macro_to_n8n(macro)
    assert wf["name"] == "mimic_invoice_entry"
    assert isinstance(wf["nodes"], list)
    assert isinstance(wf["connections"], dict)
    assert wf["active"] is False


def test_has_manual_trigger(macro):
    wf = macro_to_n8n(macro)
    triggers = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.manualTrigger"]
    assert len(triggers) == 1


def test_has_defaults_set_node(macro):
    """Macros with params must have a Set node carrying the recorded defaults."""
    wf = macro_to_n8n(macro)
    set_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.set"]
    assert len(set_nodes) == 1
    assignments = set_nodes[0]["parameters"]["assignments"]["assignments"]
    names = {a["name"] for a in assignments}
    assert "customer" in names
    assert "invoice_number" in names


def test_param_values_are_expressions(macro):
    """Fill / Select nodes must reference params as ={{ $json.<param> }} expressions."""
    wf = macro_to_n8n(macro)
    fill_nodes = [
        n for n in wf["nodes"]
        if n["type"] == "n8n-nodes-base.playwright"
        and n["parameters"].get("operation") in ("fill", "selectOption")
    ]
    assert fill_nodes, "expected at least one fill/select playwright node"
    for node in fill_nodes:
        val = node["parameters"].get("value", "")
        assert val.startswith("={{"), f"param not an expression in node {node['name']!r}: {val!r}"


def test_click_step_becomes_playwright_click(macro):
    wf = macro_to_n8n(macro)
    clicks = [
        n for n in wf["nodes"]
        if n["type"] == "n8n-nodes-base.playwright"
        and n["parameters"].get("operation") == "click"
    ]
    assert clicks, "expected a click node"


def test_connections_are_linear(macro):
    """Every node except the last must appear as a source in connections."""
    wf = macro_to_n8n(macro)
    all_names = [n["name"] for n in wf["nodes"]]
    # last node has no outgoing connection — all others must
    for name in all_names[:-1]:
        assert name in wf["connections"], f"{name!r} has no outgoing connection"


def test_node_ids_are_unique(macro):
    wf = macro_to_n8n(macro)
    ids = [n["id"] for n in wf["nodes"]]
    assert len(ids) == len(set(ids)), "duplicate node IDs"


def test_macro_without_params():
    """A macro with no fill/select steps must skip the Set defaults node."""
    bare = compile_recording(
        name="just_navigate",
        events=[
            {
                "kind": "click",
                "ts": 1,
                "target": {
                    "tag": "button",
                    "id": "go",
                    "css": "#go",
                    "xpath": "/html/body/button",
                },
            }
        ],
        start_url="https://example.com/",
    )
    wf = macro_to_n8n(bare)
    set_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.set"]
    assert len(set_nodes) == 0


# ---------------------------------------------------------------------------
# export_to_file
# ---------------------------------------------------------------------------

def test_export_writes_valid_json(tmp_path, macro):
    out = tmp_path / "invoice.n8n.json"
    written = export_to_file(macro, str(out))
    assert Path(written).exists()
    data = json.loads(Path(written).read_text())
    assert data["name"] == "mimic_invoice_entry"
    assert data["nodes"]


def test_export_creates_parent_dirs(tmp_path, macro):
    out = tmp_path / "deep" / "nested" / "wf.json"
    export_to_file(macro, str(out))
    assert out.exists()
