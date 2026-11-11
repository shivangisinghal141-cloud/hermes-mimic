import json

from mimic import MimicPlugin
from mimic.core.compiler import compile_recording
from mimic.core.macro import tool_name

CORE_TOOLS = {
    "mimic_record_start",
    "mimic_record_stop",
    "mimic_run",
    "mimic_list",
    "mimic_show",
    "mimic_heal",
    "mimic_delete",
}


class FakeUsage:
    input_tokens = 900
    output_tokens = 40
    total_tokens = 940
    cost_usd = 0.002


class FakeLlm:
    def complete_structured(self, **kwargs):
        self.kwargs = kwargs

        class Result:
            parsed = {"candidates": [{"kind": "css", "value": "button.primary"}], "reason": "renamed"}
            text = "{}"
            usage = FakeUsage()

        return Result()


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.skills = {}
        self.config = {"headless": True, "strict": True, "heal": True, "step_timeout_ms": 2000}
        self.llm = FakeLlm()

    def register_tool(self, name=None, toolset=None, schema=None, handler=None, **kwargs):
        if name in self.tools:
            raise RuntimeError(f"tool {name} already registered")
        self.tools[name] = {"schema": schema, "handler": handler, "toolset": toolset}

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_skill(self, name, path):
        self.skills[name] = str(path)


def _macro():
    events = [
        {
            "kind": "fill",
            "ts": 1,
            "value": "Acme Ltd",
            "target": {
                "tag": "input",
                "id": "customer",
                "nameAttr": "customer",
                "role": "textbox",
                "accessibleName": "Customer",
                "label": "Customer",
                "css": "#customer",
                "xpath": "/html/body/form/input[1]",
            },
        },
        {
            "kind": "click",
            "ts": 2,
            "target": {
                "tag": "button",
                "id": "create-invoice",
                "role": "button",
                "accessibleName": "Create invoice",
                "text": "Create invoice",
                "css": "#create-invoice",
                "xpath": "/html/body/form/button[1]",
            },
        },
    ]
    return compile_recording(name="invoice_entry", events=events, start_url="https://ap.example.com/new")


def test_register_exposes_core_tools_skill_and_hook(data_dir):
    ctx = FakeCtx()
    plugin = MimicPlugin(ctx).register()

    assert CORE_TOOLS <= set(ctx.tools)
    for name in CORE_TOOLS:
        assert ctx.tools[name]["schema"]["name"] == name
        assert ctx.tools[name]["schema"]["description"]
    assert "mimic" in ctx.skills
    assert "on_skill_lifecycle" in ctx.hooks
    assert plugin.store.root == data_dir


def test_macro_becomes_its_own_tool_with_typed_params(data_dir):
    ctx = FakeCtx()
    plugin = MimicPlugin(ctx).register()
    macro = _macro()
    plugin.store.save(macro)

    assert plugin.register_macro_tool(macro) is True
    assert "mimic_invoice_entry" in ctx.tools

    schema = ctx.tools["mimic_invoice_entry"]["schema"]
    properties = schema["parameters"]["properties"]
    assert properties["customer"]["type"] == "string"
    assert properties["customer"]["default"] == "Acme Ltd"
    assert properties["rows"]["type"] == "array"
    assert schema["parameters"]["required"] == ["customer"]
    assert "zero model calls" in schema["description"]

    listed = json.loads(ctx.tools["mimic_list"]["handler"]({}))
    assert listed["ok"] and listed["count"] == 1
    assert listed["macros"][0]["tool"] == "mimic_invoice_entry"

    shown = json.loads(ctx.tools["mimic_show"]["handler"]({"name": "mimic_invoice_entry"}))
    assert shown["ok"]
    assert shown["macro"]["name"] == "invoice_entry"
    assert shown["tool_schema"]["name"] == "mimic_invoice_entry"
    assert shown["exists"] is True


def test_skill_lists_learned_macros(data_dir):
    ctx = FakeCtx()
    plugin = MimicPlugin(ctx).register()
    plugin.store.save(_macro())
    text = open(plugin.publish_skill(), encoding="utf-8").read()
    assert "mimic_invoice_entry" in text
    assert "{{MACROS}}" not in text
    assert tool_name("invoice_entry") in text


def test_delete_requires_confirm_and_removes_the_data_file(data_dir):
    ctx = FakeCtx()
    plugin = MimicPlugin(ctx).register()
    macro = _macro()
    plugin.store.save(macro)

    refused = json.loads(ctx.tools["mimic_delete"]["handler"]({"name": "invoice_entry"}))
    assert refused["ok"] is False

    deleted = json.loads(ctx.tools["mimic_delete"]["handler"]({"name": "invoice_entry", "confirm": True}))
    assert deleted["ok"] is True
    assert plugin.store.get("invoice_entry") is None


def test_handlers_never_raise(data_dir):
    ctx = FakeCtx()
    MimicPlugin(ctx).register()
    for name, entry in ctx.tools.items():
        payload = json.loads(entry["handler"]({}))
        assert "ok" in payload, name
    # a caller with no arguments gets a clean refusal, not a traceback
    for name in ("mimic_run", "mimic_show", "mimic_heal", "mimic_delete", "mimic_record_start"):
        payload = json.loads(ctx.tools[name]["handler"]({}))
        assert payload["ok"] is False, name
        assert payload["error"], name
    # listing nothing is a legitimate success
    assert json.loads(ctx.tools["mimic_list"]["handler"]({}))["ok"] is True


def test_unknown_macro_is_a_clean_error(data_dir):
    ctx = FakeCtx()
    MimicPlugin(ctx).register()
    payload = json.loads(ctx.tools["mimic_run"]["handler"]({"name": "nope"}))
    assert payload["ok"] is False
    assert "no macro named" in payload["error"]
    assert payload["hint"]


def test_skill_lifecycle_hook_republishes(data_dir):
    ctx = FakeCtx()
    plugin = MimicPlugin(ctx).register()
    hook = ctx.hooks["on_skill_lifecycle"][0]
    hook(action="reused", skill_name="mimic", use_count=3)
    hook(action="removed", skill_name="mimic")
    usage = (plugin.store.skill_dir / "usage.jsonl").read_text(encoding="utf-8")
    assert '"action": "reused"' in usage
    assert plugin.store.skill_dir / "SKILL.md" in list(plugin.store.skill_dir.iterdir()) or (
        plugin.store.skill_dir / "SKILL.md"
    ).exists()
