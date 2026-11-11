"""Runner and healer decision logic, without a browser binary.

`tests/test_e2e.py` proves the selectors work in real Chromium. This test proves
the part around them — drift detection, one-call repair, macro patching,
checkpoint resume — against a small page model, so it runs on any machine.
"""

from __future__ import annotations

import asyncio

from mimic.core.compiler import compile_recording
from mimic.core.macro import validate
from mimic.core.runner import Runner
from mimic.core.store import MacroStore


def invoice_elements():
    return [
        {
            "key": "customer",
            "tag": "input",
            "css": "#customer",
            "role": "textbox",
            "name": "Customer",
            "label": "Customer",
            "text": "",
            "xpath": "/html/body/form/div[1]/input[1]",
            "value": "",
        },
        {
            "key": "invoice_number",
            "tag": "input",
            "css": "#invoice_number",
            "role": "textbox",
            "name": "Invoice number",
            "label": "Invoice number",
            "text": "",
            "xpath": "/html/body/form/div[2]/input[1]",
            "value": "",
        },
        {
            "key": "amount",
            "tag": "input",
            "css": "#amount",
            "role": "spinbutton",
            "name": "Amount",
            "label": "Amount",
            "text": "",
            "xpath": "/html/body/form/div[3]/input[1]",
            "value": "",
        },
        {
            "key": "notes",
            "tag": "textarea",
            "css": "#notes",
            "role": "textbox",
            "name": "Notes",
            "label": "Notes",
            "text": "",
            "xpath": "/html/body/form/div[4]/textarea[1]",
            "value": "",
        },
        {
            "key": "terms",
            "tag": "select",
            "css": "#terms",
            "role": "combobox",
            "name": "Payment terms",
            "label": "Payment terms",
            "text": "",
            "xpath": "/html/body/form/div[5]/select[1]",
            "value": "net45",
            "options": {"Net 15": "net15", "Net 30": "net30", "Net 45": "net45"},
        },
        {
            "key": "submit",
            "tag": "button",
            "css": "#create-invoice",
            "role": "button",
            "name": "Create invoice",
            "label": "",
            "text": "Create invoice",
            "xpath": "/html/body/form/div[6]/button[1]",
            "value": "",
            "submit": True,
        },
        {
            "key": "result",
            "tag": "div",
            "css": "#result",
            "role": "",
            "name": "",
            "label": "",
            "text": "",
            "xpath": "/html/body/form/div[7]",
            "value": "",
        },
    ]


class FakeLocator:
    def __init__(self, page, **query):
        self.page = page
        self.query = query

    def _matches(self):
        return [element for element in self.page.elements if self.page.matches(element, self.query)]

    def _one(self):
        found = self._matches()
        if len(found) != 1:
            raise RuntimeError(f"locator matched {len(found)} elements")
        return found[0]

    async def count(self):
        return len(self._matches())

    async def click(self, timeout=None):
        element = self._one()
        self.page.clicks.append(element["key"])
        if element.get("submit"):
            self.page.result = (
                f"Invoice {self.page.value_of('invoice_number')} created for {self.page.value_of('customer')}"
            )

    async def fill(self, value, timeout=None):
        self._one()["value"] = value

    async def input_value(self, timeout=None):
        return self._one().get("value", "")

    async def inner_text(self, timeout=None):
        return self._one().get("text", "")

    async def is_checked(self):
        return bool(self._one().get("checked"))

    async def press(self, key, timeout=None):
        self._one()

    async def select_option(self, label=None, value=None, timeout=None):
        element = self._one()
        options = element.get("options") or {}
        if label is not None and label in options:
            element["value"] = options[label]
            return
        if value is not None and value in options.values():
            element["value"] = value
            return
        raise RuntimeError(f"no option {label or value!r}")

    async def check(self, timeout=None):
        self._one()["checked"] = True

    async def uncheck(self, timeout=None):
        self._one()["checked"] = False


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.keys.append(key)


class FakePage:
    def __init__(self, elements=None):
        self.url = "https://ap.example.com/new"
        self.html = "<html><body><form><input id='customer'><button id='create-invoice'>Create invoice</button></form></body></html>"
        self.elements = elements if elements is not None else invoice_elements()
        self.clicks = []
        self.keys = []
        self.result = ""
        self.gotos = 0
        self.keyboard = FakeKeyboard(self)

    # -------------------------------------------------------------- page model
    def matches(self, element, query):
        kind = query["kind"]
        value = query.get("value")
        if kind == "css":
            return element.get("css") == value or element.get("alt_css") == value
        if kind == "xpath":
            return element.get("xpath") == value
        if kind == "role":
            name = query.get("name")
            return element.get("role") == value and (not name or element.get("name") == name)
        if kind == "label":
            return element.get("label") == value
        if kind == "text":
            return element.get("text") == value
        if kind == "testid":
            return element.get("testid") == value
        return False

    def find(self, key):
        return next(element for element in self.elements if element["key"] == key)

    def value_of(self, key):
        return self.find(key).get("value", "")

    def set_values(self, **values):
        for key, value in values.items():
            self.find(key)["value"] = value

    def reset(self):
        for element in self.elements:
            if element["key"] == "terms":
                element["value"] = "net45"
            elif element.get("tag") in ("input", "textarea"):
                element["value"] = ""

    # --------------------------------------------------------------- Playwright
    def locator(self, selector):
        # Playwright encodes XPath as the string "xpath=/html/..." through .locator()
        if isinstance(selector, str) and selector.startswith("xpath="):
            return FakeLocator(self, kind="xpath", value=selector[len("xpath=") :])
        return FakeLocator(self, kind="css", value=selector)

    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self, kind="role", value=role, name=name)

    def get_by_label(self, value, exact=True):
        return FakeLocator(self, kind="label", value=value)

    def get_by_text(self, value, exact=True):
        return FakeLocator(self, kind="text", value=value)

    def get_by_test_id(self, value):
        return FakeLocator(self, kind="testid", value=value)

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos += 1
        self.url = url
        self.result = ""
        self.reset()

    async def content(self):
        return self.html

    async def evaluate(self, script):
        return [{"tag": element["tag"], "id": element.get("css", "").lstrip("#")} for element in self.elements]

    async def title(self):
        return "Fake AP"


class FakeSession:
    def __init__(self, page):
        self.page = page

    async def _ensure_page(self):
        return self.page

    def call(self, coro, timeout=None):
        return asyncio.run(coro)


class FakeHealer:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def available(self):
        return True

    def propose(self, **kwargs):
        self.calls += 1
        return {"candidates": self.candidates, "reason": "the submit button was renamed", "llm_calls": 1, "usage": {"input_tokens": 10}}


def descriptor(element, field):
    return {
        "tag": element["tag"],
        "id": element.get("css", "").lstrip("#"),
        "nameAttr": field,
        "role": element.get("role", ""),
        "accessibleName": element.get("name", ""),
        "label": element.get("label", ""),
        "text": element.get("text", ""),
        "testid": element.get("testid", ""),
        "css": element.get("css", ""),
        "xpath": element.get("xpath", ""),
        "value": element.get("value", ""),
    }


def build_macro(page):
    events = []
    stamp = 0
    for field, value in (
        ("customer", "Acme Ltd"),
        ("invoice_number", "INV-1"),
        ("amount", "1200"),
        ("notes", "net 30"),
    ):
        stamp += 1
        events.append({"kind": "fill", "ts": stamp, "value": value, "target": descriptor(page.find(field), field)})
    stamp += 1
    events.append({"kind": "select", "ts": stamp, "value": "net45", "target": descriptor(page.find("terms"), "terms")})
    stamp += 1
    events.append({"kind": "click", "ts": stamp, "target": descriptor(page.find("submit"), "")})
    return compile_recording(name="invoice_entry", events=events, start_url=page.url)


def test_replay_then_heal_then_free_again(tmp_path):
    page = FakePage()
    store = MacroStore(tmp_path / "data")
    macro = build_macro(page)
    assert validate(macro) == []

    params = {"customer": "Beta Corp", "invoice_number": "INV-2", "amount": "99", "notes": "rush"}
    first = Runner(FakeSession(page), store=store, healer=FakeHealer([]), strict=True).run(macro, params)
    assert first["ok"], first
    assert first["llm_calls"] == 0 and first["heals"] == 0
    assert page.result == "Invoice INV-2 created for Beta Corp"
    assert page.clicks == ["submit"]

    # the site renames its submit button: id, accessible name and text change
    submit = page.find("submit")
    submit["css"] = "#do-it"
    submit["name"] = "Do it now"
    submit["text"] = "Do it now"
    submit["alt_css"] = "button.primary"

    healer = FakeHealer([{"kind": "css", "value": "button.primary", "confidence": 0.9}])
    healed = Runner(FakeSession(page), store=store, healer=healer, strict=True).run(macro, params)

    assert healed["ok"], healed
    assert healed["status"] == "healed"
    assert healed["heals"] == 1 and healer.calls == 1
    assert [step["id"] for step in healed["steps"] if step.get("healed")]
    assert page.result == "Invoice INV-2 created for Beta Corp"

    saved = store.get("invoice_entry")
    saved_submit = [step for step in saved["steps"] if step["action"] == "click"][-1]
    assert saved_submit["target"]["strategies"][0]["value"] == "button.primary"
    assert saved_submit["heal_count"] == 1

    quiet = FakeHealer([])
    again = Runner(FakeSession(page), store=store, healer=quiet, strict=True).run(macro, params)
    assert again["ok"] and again["heals"] == 0 and again["llm_calls"] == 0
    assert quiet.calls == 0


def test_degraded_step_is_drift_not_silent_fallback(tmp_path):
    """The xpath still matches the renamed button: strict mode still says drift."""
    page = FakePage()
    store = MacroStore(tmp_path / "data")
    macro = build_macro(page)

    submit = page.find("submit")
    submit["css"] = "#do-it"
    submit["name"] = "Do it now"
    submit["text"] = "Do it now"

    healer = FakeHealer([])  # proposes nothing
    result = Runner(FakeSession(page), store=store, healer=healer, strict=True).run(macro, {})

    assert result["ok"] is False
    assert result["failed_step_id"] == [step for step in macro["steps"] if step["action"] == "click"][-1]["id"]
    assert "no proposed selector" in result["steps"][-1]["heal"]["reason"] or result["steps"][-1]["heal"]

    # non-strict mode accepts the fallback (the action still happens)
    relaxed = Runner(FakeSession(page), store=store, healer=FakeHealer([]), strict=False).run(macro, {})
    assert relaxed["ok"], relaxed
    click_steps = [step for step in relaxed["steps"] if step["action"] == "click"]
    assert click_steps[-1]["degraded"] is True


def test_rows_run_once_and_fail_fast(tmp_path):
    page = FakePage()
    macro = build_macro(page)
    runner = Runner(FakeSession(page), store=None, healer=FakeHealer([]), strict=True)

    batch = runner.run_rows(
        macro,
        {"invoice_number": "INV-3", "amount": "10", "notes": "batch"},
        [{"customer": "Row A"}, {"customer": "Row B"}],
    )
    assert batch["ok"] and batch["rows_run"] == 2
    assert batch["llm_calls"] == 0
    assert page.result == "Invoice INV-3 created for Row B"


def test_checkpoint_resume_starts_at_the_failed_step(tmp_path):
    page = FakePage()
    store = MacroStore(tmp_path / "data")
    macro = build_macro(page)
    params = {"customer": "Resume Co", "invoice_number": "INV-9", "amount": "42", "notes": "x"}

    amount = page.find("amount")
    saved = dict(amount)
    amount.update({"css": "", "role": "", "name": "", "label": "", "xpath": ""})  # the field disappears

    failed = Runner(FakeSession(page), store=store, healer=None, strict=True).run(macro, params)
    assert failed["ok"] is False
    failed_step = failed["failed_step_id"]
    assert failed_step is not None
    assert store.last_run("invoice_entry")["failed_step_id"] == failed_step

    # the site comes back; resume without replaying the earlier steps
    amount.update(saved)
    resumed = Runner(FakeSession(page), store=store, healer=None, strict=True).run(macro, params, resume=True)

    assert resumed["ok"], resumed
    assert resumed["steps"][0]["id"] == failed_step
    assert resumed["resume_from"]
    assert page.result == "Invoice INV-9 created for Resume Co"
