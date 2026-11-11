from mimic.core.compiler import compile_recording, interactions
from mimic.core.macro import params_with_defaults, render, tool_name, validate


def descriptor(css, *, tag="input", role="textbox", name="", label="", text="", value="", testid=""):
    return {
        "tag": tag,
        "id": css.lstrip("#"),
        "nameAttr": css.lstrip("#"),
        "type": "text",
        "role": role,
        "accessibleName": name,
        "label": label,
        "text": text,
        "testid": testid,
        "css": css,
        "xpath": "/html/body" + css,
        "value": value,
    }


def test_fills_become_typed_parameters_with_defaults():
    events = [
        {"kind": "fill", "ts": 1, "value": "Acme Ltd", "target": descriptor("#customer", name="Customer", label="Customer")},
        {"kind": "fill", "ts": 2, "value": "INV-1001", "target": descriptor("#invoice_number", name="Invoice number", label="Invoice number")},
        {"kind": "fill", "ts": 3, "value": "1200", "target": descriptor("#amount", role="spinbutton", name="Amount", label="Amount")},
    ]
    macro = compile_recording(name="invoice_entry", events=events, start_url="https://ap.example.com/invoices/new")

    assert validate(macro) == []
    assert [param["name"] for param in macro["params"]] == ["customer", "invoice_number", "amount"]
    assert [param["default"] for param in macro["params"]] == ["Acme Ltd", "INV-1001", "1200"]
    assert macro["params"][0]["required"] is True
    assert interactions(macro) == 3
    assert all(step["checkpoint"] is True for step in macro["steps"])
    assert macro["steps"][0]["action"] == "goto"
    assert macro["steps"][1]["verify"] == {"kind": "value", "param": "customer"}


def test_duplicate_fill_of_same_field_keeps_last_value():
    target = descriptor("#customer", name="Customer", label="Customer")
    events = [
        {"kind": "fill", "ts": 1, "value": "A", "target": target},
        {"kind": "fill", "ts": 2, "value": "Acme", "target": target},
    ]
    macro = compile_recording(name="x", events=events, start_url="https://example.com")
    fills = [step for step in macro["steps"] if step["action"] == "fill"]
    assert len(fills) == 1
    assert macro["params"][0]["default"] == "Acme"


def test_click_before_select_is_dropped():
    target = descriptor("#terms", tag="select", role="combobox", name="Payment terms", label="Payment terms")
    events = [
        {"kind": "click", "ts": 1, "target": target},
        {"kind": "select", "ts": 2, "value": "net30", "target": target},
    ]
    macro = compile_recording(name="x", events=events, start_url="https://example.com")
    assert [step["action"] for step in macro["steps"]] == ["goto", "select"]
    assert macro["params"][0]["default"] == "net30"


def test_navigation_becomes_a_goto_step_and_skips_initial_load():
    events = [
        {"kind": "navigate", "ts": 1, "url": "https://example.com/form"},
        {"kind": "click", "ts": 2, "target": descriptor("#submit", tag="button", role="button", name="Create", text="Create")},
        {"kind": "navigate", "ts": 3, "url": "https://example.com/done"},
    ]
    macro = compile_recording(name="x", events=events, start_url="https://example.com/form")
    actions = [step["action"] for step in macro["steps"]]
    assert actions == ["goto", "click", "goto"]
    assert macro["steps"][2]["url"] == "https://example.com/done"


def test_tool_name_and_templating():
    assert tool_name("Invoice Entry!") == "mimic_invoice_entry"
    assert render("Hello {{customer}}", {"customer": "Acme"}) == "Hello Acme"
    assert render("No placeholders", {}) == "No placeholders"


def test_params_with_defaults_keeps_overrides_only():
    macro = {"params": [{"name": "a", "default": "1", "required": True}, {"name": "b", "default": "2"}]}
    values = params_with_defaults(macro, {"b": "9", "a": None})
    assert values == {"a": "1", "b": "9"}


def test_empty_recording_compiles_to_nothing_runnable():
    macro = compile_recording(name="x", events=[], start_url="https://example.com")
    assert interactions(macro) == 0
    assert validate(macro) == []  # valid JSON, but the plugin refuses to keep it
