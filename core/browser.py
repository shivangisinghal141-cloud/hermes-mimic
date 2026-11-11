"""One Playwright browser, one owning thread, one event loop.

Two constraints shape this file:

1. Tool handlers are sync and may already run inside an event loop. Playwright's
   sync API refuses that, so the async API is the safe one.
2. A recording spans several tool calls (start → the user acts → stop), so the
   browser has to outlive a single call.

So a worker thread owns an asyncio loop for the lifetime of the plugin, and every
browser operation is submitted to it with ``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)


class BrowserSession:
    def __init__(
        self,
        *,
        headless: bool = True,
        cdp_url: str = "",
        profile_dir: Optional[Path] = None,
    ) -> None:
        self.headless = bool(headless)
        self.cdp_url = (cdp_url or "").strip()
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = None
        self._ready = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._closed = False

    # ------------------------------------------------------------------ thread
    def start(self) -> "BrowserSession":
        if self._thread is not None and self._thread.is_alive():
            return self
        import threading

        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="mimic-playwright", daemon=True)
        self._thread.start()
        if not self._ready.wait(20):
            raise RuntimeError("MIMIC: the browser thread did not start")
        return self

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def call(self, coro: Coroutine[Any, Any, Any], timeout: float = 180.0) -> Any:
        """Run a coroutine on the browser thread and block for its result."""
        self.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    # ----------------------------------------------------------------- browser
    async def _ensure_page(self):
        if self._pw is None:
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:  # pragma: no cover - dependency error path
                raise RuntimeError(
                    "MIMIC needs Playwright: pip install playwright && python -m playwright install chromium"
                ) from exc
            self._pw = await async_playwright().start()

        if self._context is None:
            args = ["--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage"]
            extra = os.environ.get("MIMIC_CHROMIUM_ARGS", "").strip()
            if extra:
                args.extend(part for part in extra.split() if part)
            viewport: Dict[str, int] = {"width": 1280, "height": 900}
            if self.cdp_url:
                # Attach to a browser the user already has open (their own session,
                # their own cookies) instead of launching a fresh profile.
                self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
                contexts = list(self._browser.contexts)
                self._context = contexts[0] if contexts else await self._browser.new_context()
            elif self.profile_dir is not None:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._context = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    viewport=viewport,
                    args=args,
                )
            else:
                self._browser = await self._pw.chromium.launch(headless=self.headless, args=args)
                self._context = await self._browser.new_context(viewport=viewport)

        if self._page is None or self._page.is_closed():
            pages = [page for page in self._context.pages if not page.is_closed()]
            self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    def page(self):
        return self.call(self._ensure_page())

    @property
    def context(self):
        self.page()
        return self._context

    # ------------------------------------------------------------------- close
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def _shutdown() -> None:
            for resource in (self._context, self._browser):
                if resource is None:
                    continue
                try:
                    await resource.close()
                except Exception:
                    pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass

        try:
            if self._thread is not None and self._thread.is_alive():
                try:
                    self.call(_shutdown(), timeout=20)
                except Exception:
                    pass
        finally:
            if self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except Exception:
                    pass
            if self._thread is not None:
                self._thread.join(timeout=5)

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass
