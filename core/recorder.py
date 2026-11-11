"""Recording control: inject ``recorder.js``, track navigation, drain the event log.

The recorder is injected through the *browser context* (not the page) so every
new document — including a full page load after a form submit — is instrumented.
Events are buffered in the page and drained once, at stop, so the compiled macro
never depends on a live binding having survived a navigation.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from .browser import BrowserSession

logger = logging.getLogger(__name__)

RECORDER_JS = Path(__file__).with_name("recorder.js")
DEFAULT_HINT = (
    "Recording. Perform the workflow once in the browser (fill the form, click the buttons), "
    "then call mimic_record_stop with a short name."
)


class Recorder:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.active = False
        self.name = ""
        self.start_url = ""
        self.started_at = 0.0
        self._navs: List[Dict[str, Any]] = []
        self._last_url = ""
        self._page = None
        self._nav_attached = None
        self._live: List[Dict[str, Any]] = []

    # ----------------------------------------------------------------- control
    def start(self, url: str, *, name: str = "") -> Dict[str, Any]:
        if self.active:
            raise RuntimeError("a recording is already in progress; call mimic_record_stop first")
        info = self.session.call(self._start(url), timeout=120)
        self.name = name
        self.start_url = url
        self.active = True
        self.started_at = time.time()
        info.update({"recording": True, "name": name, "hint": DEFAULT_HINT})
        return info

    async def _start(self, url: str) -> Dict[str, Any]:
        page = await self.session._ensure_page()
        context = self.session._context
        self._page = page
        self._navs = []
        self._live = []
        self._last_url = ""

        if not RECORDER_JS.exists():  # pragma: no cover - packaging error
            raise RuntimeError(f"recorder.js is missing at {RECORDER_JS}")

        try:
            await context.expose_binding("__mimicEmit", self._on_emit)
        except Exception:
            logger.debug("mimic: recorder binding already exposed")

        await context.add_init_script(path=str(RECORDER_JS))

        self._detach_nav()
        page_ref = page

        def on_nav(frame) -> None:
            try:
                if frame == page_ref.main_frame:
                    self._record_nav(frame.url)
            except Exception:
                pass

        page.on("framenavigated", on_nav)
        self._nav_attached = (page, on_nav)

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        try:
            title = await page.title()
        except Exception:
            title = ""
        return {"url": page.url, "title": title}

    def _on_emit(self, source, payload) -> None:  # noqa: ARG002 - Playwright signature
        if not self.active:
            return
        if isinstance(payload, dict):
            self._live.append(payload)
            del self._live[:-200]

    def _record_nav(self, url: str) -> None:
        if not url or url.startswith("about:"):
            return
        if url == self._last_url:
            return
        self._last_url = url
        self._navs.append({"kind": "navigate", "url": url, "ts": int(time.time() * 1000)})

    # -------------------------------------------------------------------- stop
    def stop(self) -> Dict[str, Any]:
        if not self.active:
            raise RuntimeError("no recording in progress; call mimic_record_start first")
        try:
            payload = self.session.call(self._stop(), timeout=120)
        finally:
            self.active = False
            self._detach_nav()
        events = self._merge(payload.get("events") or [])
        return {
            "events": events,
            "url": payload.get("url") or "",
            "title": payload.get("title") or "",
            "start_url": self.start_url,
            "duration_ms": int((time.time() - self.started_at) * 1000),
            "event_count": len(events),
        }

    async def _stop(self) -> Dict[str, Any]:
        page = self._page or await self.session._ensure_page()
        events: List[Dict[str, Any]] = []
        try:
            events = await page.evaluate(
                "() => (typeof window.__mimicFlush === 'function' ? window.__mimicFlush() : [])"
            ) or []
        except Exception:
            logger.warning("mimic: could not drain the recorder buffer", exc_info=True)
        try:
            url = page.url
            title = await page.title()
        except Exception:
            url, title = "", ""
        return {"events": events, "url": url, "title": title}

    def _detach_nav(self) -> None:
        if not self._nav_attached:
            return
        page, callback = self._nav_attached
        self._nav_attached = None
        try:
            page.remove_listener("framenavigated", callback)
        except Exception:
            pass

    # ------------------------------------------------------------------ digest
    def _merge(self, buffered: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = [event for event in buffered if isinstance(event, dict)]
        merged.extend(self._navs)
        merged.sort(key=lambda event: event.get("ts") or 0)
        deduped: List[Dict[str, Any]] = []
        for event in merged:
            if deduped:
                previous = deduped[-1]
                same_kind = previous.get("kind") == event.get("kind")
                same_target = (previous.get("target") or {}).get("xpath") == (event.get("target") or {}).get("xpath")
                same_value = previous.get("value") == event.get("value")
                if same_kind and same_target and same_value and event.get("kind") in ("fill", "select", "check"):
                    deduped[-1] = event
                    continue
            deduped.append(event)
        return deduped

    def status(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "start_url": self.start_url,
            "events": len(self._live),
            "duration_ms": int((time.time() - self.started_at) * 1000) if self.active else 0,
        }
