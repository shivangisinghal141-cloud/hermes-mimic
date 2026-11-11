"""End-to-end: a real Chromium records a real form, replay is free, healing repairs it.

This is the whole product in one test:

    record once by hand → compile to a parameterized macro → replay for rows at
    zero model calls → rename the submit button on the site → the step goes red →
    heal with one model call → patch the saved macro → replay is free again
"""

from __future__ import annotations

import functools
import http.server
import importlib.util
import socketserver
import threading
from pathlib import Path

import pytest

from mimic.core.browser import BrowserSession
from mimic.core.compiler import compile_recording
from mimic.core.macro import validate
from mimic.core.recorder import Recorder
from mimic.core.runner import Runner
from mimic.core.store import MacroStore

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="playwright is not installed",
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, *args):
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture()
def site(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    page = (ROOT / "demo" / "invoice.html").read_text(encoding="utf-8")
    (site_dir / "invoice.html").write_text(page, encoding="utf-8")
    handler = functools.partial(_QuietHandler, directory=str(site_dir))
    httpd = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"dir": site_dir, "url": f"http://127.0.0.1:{httpd.server_address[1]}/invoice.html"}
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIC_CHROMIUM_ARGS", "--no-sandbox")
    browser = BrowserSession(headless=True, profile_dir=tmp_path / "profile")
    try:
        browser.page()
    except Exception as exc:  # e.g. missing system libraries in a slim container
        browser.close()
        pytest.skip(f"Chromium cannot start here: {str(exc).splitlines()[0][:160]}")
    yield browser
    browser.close()


class FakeHealer:
    """Stands in for ctx.llm: proposes selectors the runner then validates itself."""

    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0
        self.last = {}

    def available(self):
        return True

    def propose(self, **kwargs):
        self.calls += 1
        self.last = kwargs
        return {
            "candidates": self.candidates,
            "reason": "the submit button's id and label changed",
            "llm_calls": 1,
            "usage": {"input_tokens": 1200, "output_tokens": 60, "cost_usd": 0.003},
        }


def _record(session, url):
    async def actions(page):
        await page.get_by_label("Customer", exact=True).fill("Acme Ltd")
        await page.get_by_label("Invoice number", exact=True).fill("INV-1001")
        await page.get_by_label("Amount", exact=True).fill("1200")
        await page.get_by_label("Payment terms", exact=True).select_option("net45")
        await page.get_by_label("Notes", exact=True).fill("Net 30")
        await page.get_by_role("button", name="Create invoice", exact=True).click()
        await page.wait_for_selector("#result")

    page = session.page()
    session.call(actions(page))
    return page


def test_record_compile_replay_batch_and_self_heal(tmp_path, site, session):
    store = MacroStore(tmp_path / "data")
    recorder = Recorder(session)
    recorder.start(site["url"], name="invoice_entry")
    page = _record(session, site["url"])
    raw = recorder.stop()

    assert raw["event_count"] >= 5, raw
    assert "Invoice INV-1001 created for Acme Ltd" in session.call(page.inner_text("#result"))

    macro = compile_recording(
        name="invoice_entry",
        events=raw["events"],
        start_url=site["url"],
        final_url=raw["url"],
        title=raw["title"],
    )
    assert validate(macro) == []
    names = [param["name"] for param in macro["params"]]
    assert names[:4] == ["customer", "invoice_number", "amount", "terms"]
    assert macro["params"][3]["default"] == "net45"
    assert macro["steps"][0]["action"] == "goto"
    click_steps = [step for step in macro["steps"] if step["action"] == "click"]
    assert click_steps, macro["steps"]
    assert click_steps[-1]["target"]["strategies"][0]["value"] == "#create-invoice"

    # ---- replay with new data: deterministic, zero model calls -----------------
    runner = Runner(session, store=store, healer=FakeHealer([]), strict=True)
    first = runner.run(
        macro,
        {"customer": "Beta Corp", "invoice_number": "INV-2002", "amount": "99", "notes": "rush"},
    )
    assert first["ok"], first
    assert first["llm_calls"] == 0 and first["heals"] == 0
    assert "Invoice INV-2002 created for Beta Corp" in session.call(page.inner_text("#result"))

    # ---- one compile, eight rows, still zero model calls -----------------------
    batch = Runner(session, store=store, healer=FakeHealer([]), strict=True).run_rows(
        macro,
        {"invoice_number": "INV-3003", "amount": "10", "notes": "batch", "terms": "net30"},
        [{"customer": "Row A"}, {"customer": "Row B"}, {"customer": "Row C"}],
    )
    assert batch["ok"], batch
    assert batch["rows_run"] == 3
    assert batch["llm_calls"] == 0 and batch["heals"] == 0
    assert "created for Row C" in session.call(page.inner_text("#result"))

    # ---- the site changes: the submit button is renamed ------------------------
    original = (site["dir"] / "invoice.html").read_text(encoding="utf-8")
    broken = original.replace('id="create-invoice"', 'id="do-it"').replace(
        ">Create invoice<", ">Do it now<"
    )
    (site["dir"] / "invoice.html").write_text(broken, encoding="utf-8")

    healer = FakeHealer(
        [
            {"kind": "css", "value": "button.primary", "confidence": 0.9},
            {"kind": "role", "value": "button", "role": "button", "name": "Do it now"},
        ]
    )
    repaired = Runner(session, store=store, healer=healer, strict=True).run(
        macro,
        {"customer": "Heal Co", "invoice_number": "INV-4040", "amount": "5", "notes": "x"},
    )

    assert repaired["ok"], repaired
    assert repaired["status"] == "healed"
    assert repaired["heals"] == 1
    assert healer.calls == 1
    healed_steps = [step for step in repaired["steps"] if step.get("healed")]
    assert healed_steps and healed_steps[0]["id"] == click_steps[-1]["id"]
    assert "INV-4040" in session.call(page.inner_text("#result"))

    # ---- the repair is data: the saved macro now carries the new selector ------
    saved = store.get("invoice_entry")
    saved_click = [step for step in saved["steps"] if step["action"] == "click"][-1]
    assert saved_click["target"]["strategies"][0]["value"] == "button.primary"
    assert saved_click["heal_count"] == 1
    assert saved["history"] and saved["history"][0]["step"] == saved_click["id"]

    # ---- next run is free again ------------------------------------------------
    quiet = FakeHealer([])
    clean = Runner(session, store=store, healer=quiet, strict=True).run(macro)
    assert clean["ok"], clean
    assert clean["heals"] == 0 and clean["llm_calls"] == 0
    assert quiet.calls == 0
    assert "created for Acme Ltd" in session.call(page.inner_text("#result"))
