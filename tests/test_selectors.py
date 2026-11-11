from mimic.core.compiler import strategies_from_descriptor
from mimic.core.selectors import describe, is_authored_id, normalize_candidate, sort_strategies, strategy


def test_authored_ids_only():
    assert is_authored_id("create-invoice")
    assert is_authored_id("customer")
    # generated / framework ids change between renders and must not be identity
    assert not is_authored_id("react-select-7-input")
    assert not is_authored_id("field-a1b2c3d4e5")
    assert not is_authored_id("input_123456")
    assert not is_authored_id(":r1a:")
    assert not is_authored_id("")


def test_strategy_scores_prefer_testid_then_authored_id_then_role():
    assert strategy("testid", "save")["score"] == 100
    assert strategy("css", "#save")["score"] == 98
    assert strategy("css", "div > span:nth-of-type(2)")["score"] == 70
    assert strategy("role", "button", role="button", name="Save")["score"] == 95
    assert strategy("xpath", "/html/body/button[1]")["score"] == 50


def test_sort_keeps_declaration_order_on_ties():
    items = [strategy("css", ".a"), strategy("css", ".b")]
    assert [item["value"] for item in sort_strategies(items)] == [".a", ".b"]


def test_descriptor_ladder_is_ordered_and_complete():
    descriptor = {
        "tag": "button",
        "id": "create-invoice",
        "testid": "",
        "css": "#create-invoice",
        "role": "button",
        "accessibleName": "Create invoice",
        "label": "",
        "text": "Create invoice",
        "xpath": "/html/body/form/div/button[1]",
    }
    ladder = strategies_from_descriptor(descriptor)
    assert [item["kind"] for item in ladder] == ["css", "role", "text", "xpath"]
    assert ladder[0]["value"] == "#create-invoice"
    assert ladder[1]["name"] == "Create invoice"


def test_descriptor_without_testid_still_has_fallbacks():
    descriptor = {"tag": "input", "css": "input[name=q]", "role": "textbox", "accessibleName": "Search"}
    ladder = strategies_from_descriptor(descriptor)
    assert [item["kind"] for item in ladder] == ["role", "css"]


def test_normalize_candidate_rejects_junk():
    assert normalize_candidate({"kind": "nope", "value": "x"}) is None
    assert normalize_candidate({"kind": "css", "value": "  "}) is None
    assert normalize_candidate("css") is None
    role = normalize_candidate({"kind": "role", "value": "button", "name": "Pay"})
    assert role["kind"] == "role" and role["role"] == "button" and role["name"] == "Pay"


def test_describe_is_human_readable():
    assert describe({"kind": "role", "value": "button", "role": "button", "name": "Pay"}) == 'role=button[name="Pay"]'
    assert describe({"kind": "testid", "value": "save-btn"}) == "testid=save-btn"
