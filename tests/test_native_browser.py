"""Tests for core/native_browser.py — CDP discovery and profile import."""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mimic.core.native_browser import (
    _candidate_profiles,
    _probe_cdp,
    discover_cdp_url,
    import_profile,
    launch_instructions,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeCDPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/json/version":
            body = json.dumps({"Browser": "Chrome/120", "webSocketDebuggerUrl": "ws://..."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


class _CDPServer(socketserver.TCPServer):
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# _probe_cdp
# ---------------------------------------------------------------------------

def test_probe_cdp_hits_live_server():
    with _CDPServer(("127.0.0.1", 0), _FakeCDPHandler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        result = _probe_cdp(port)
        t.join(timeout=3)
    assert result == f"http://127.0.0.1:{port}"


def test_probe_cdp_returns_none_on_closed_port():
    # Port 1 is privileged and always refused without root.
    result = _probe_cdp(1)
    assert result is None


# ---------------------------------------------------------------------------
# discover_cdp_url
# ---------------------------------------------------------------------------

def test_discover_finds_first_live_port():
    with _CDPServer(("127.0.0.1", 0), _FakeCDPHandler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        with patch("mimic.core.native_browser.CDP_PROBE_PORTS", (1, port, 9224)):
            result = discover_cdp_url()
        t.join(timeout=3)
    assert result == f"http://127.0.0.1:{port}"


def test_discover_returns_none_when_nothing_running():
    with patch("mimic.core.native_browser.CDP_PROBE_PORTS", (1,)):
        assert discover_cdp_url() is None


# ---------------------------------------------------------------------------
# import_profile
# ---------------------------------------------------------------------------

def test_import_copies_cookies(tmp_path):
    src = tmp_path / "real_profile"
    src.mkdir()
    (src / "Cookies").write_bytes(b"fake-sqlite-data")
    (src / "Login Data").write_bytes(b"fake-login")

    dest = tmp_path / "playwright_profile"

    with patch("mimic.core.native_browser._candidate_profiles", return_value=[src]):
        result = import_profile(dest)

    assert result is True
    assert (dest / "Cookies").exists()
    assert (dest / "Cookies").read_bytes() == b"fake-sqlite-data"


def test_import_skips_when_cookies_already_exist(tmp_path):
    src = tmp_path / "real_profile"
    src.mkdir()
    (src / "Cookies").write_bytes(b"real-cookies")

    dest = tmp_path / "playwright_profile"
    dest.mkdir()
    (dest / "Cookies").write_bytes(b"existing-cookies")

    with patch("mimic.core.native_browser._candidate_profiles", return_value=[src]):
        result = import_profile(dest)

    assert result is False
    # Existing file must not be overwritten.
    assert (dest / "Cookies").read_bytes() == b"existing-cookies"


def test_import_returns_false_when_no_profile_found(tmp_path):
    dest = tmp_path / "playwright_profile"
    with patch("mimic.core.native_browser._candidate_profiles", return_value=[]):
        result = import_profile(dest)
    assert result is False


def test_import_copies_local_storage_dir(tmp_path):
    src = tmp_path / "real_profile"
    ls = src / "Local Storage"
    ls.mkdir(parents=True)
    (ls / "data.ldb").write_bytes(b"ldb")

    dest = tmp_path / "playwright_profile"

    with patch("mimic.core.native_browser._candidate_profiles", return_value=[src]):
        import_profile(dest)

    assert (dest / "Local Storage" / "data.ldb").exists()


# ---------------------------------------------------------------------------
# launch_instructions
# ---------------------------------------------------------------------------

def test_launch_instructions_contains_port():
    text = launch_instructions(9999)
    assert "9999" in text
    assert "google-chrome" in text or "chrome" in text.lower()
