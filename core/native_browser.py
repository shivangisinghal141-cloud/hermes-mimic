"""Utilities for attaching to the user's real browser instead of a fresh Chromium.

Two approaches, in preference order:

1. **CDP auto-discovery** — probe ``http://127.0.0.1:<port>/json/version`` for a
   browser that is already listening. The user may have launched Chrome/Edge with
   ``--remote-debugging-port=9222``, or a previous MIMIC session may have left one
   running. If found, return its URL immediately (zero user interaction).

2. **Profile import** — when ``native_browser: true`` is set but no CDP endpoint is
   found, copy cookies + localStorage from the user's real Chrome/Chromium/Edge
   profile into the Playwright persistent-context profile so the first run is
   already logged in. This is a one-time bootstrap; subsequent runs use the already
   populated Playwright profile.

Neither approach is mandatory. If ``native_browser`` is false (the default) MIMIC
behaves exactly as before: a clean isolated Chromium profile.

Supported browser profile locations (tried in order):
  - $CHROME_USER_DATA / Default
  - ~/.config/google-chrome/Default
  - ~/.config/chromium/Default
  - ~/snap/chromium/current/.config/chromium/Default
  - ~/.config/microsoft-edge/Default
  - macOS: ~/Library/Application Support/Google/Chrome/Default
  - macOS: ~/Library/Application Support/Microsoft Edge/Default
"""

from __future__ import annotations

import logging
import shutil
import socket
from pathlib import Path
from typing import Optional
from urllib.request import urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

# Files worth copying for session continuity (Cookies is the critical one).
_PROFILE_FILES = (
    "Cookies",
    "Login Data",
    "Local State",
    "Web Data",
    "Preferences",
    "Secure Preferences",
)
_PROFILE_DIRS = (
    "Local Storage",
    "Session Storage",
    "IndexedDB",
)

CDP_PROBE_PORTS = (9222, 9223, 9224, 9225)
CDP_PROBE_TIMEOUT = 1.0  # seconds per port


def _probe_cdp(port: int) -> Optional[str]:
    """Return the CDP URL if a browser is already listening on this port."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urlopen(url, timeout=CDP_PROBE_TIMEOUT) as resp:
            if resp.status == 200:
                logger.info("mimic: found browser at %s", url)
                return f"http://127.0.0.1:{port}"
    except (URLError, OSError, socket.timeout):
        pass
    return None


def discover_cdp_url() -> Optional[str]:
    """Probe common CDP ports and return the first live endpoint, or None."""
    for port in CDP_PROBE_PORTS:
        result = _probe_cdp(port)
        if result:
            return result
    return None


def _candidate_profiles() -> list[Path]:
    """Return real browser profile directories that exist on this machine."""
    home = Path.home()
    candidates = [
        home / ".config" / "google-chrome" / "Default",
        home / ".config" / "chromium" / "Default",
        home / "snap" / "chromium" / "current" / ".config" / "chromium" / "Default",
        home / ".config" / "microsoft-edge" / "Default",
        # macOS paths
        home / "Library" / "Application Support" / "Google" / "Chrome" / "Default",
        home / "Library" / "Application Support" / "Microsoft Edge" / "Default",
    ]
    # Also honour $CHROME_USER_DATA if set
    import os
    env_data = os.environ.get("CHROME_USER_DATA")
    if env_data:
        candidates.insert(0, Path(env_data) / "Default")

    return [p for p in candidates if p.exists()]


def import_profile(dest: Path) -> bool:
    """Copy cookies and local storage from the user's real Chrome profile into *dest*.

    Returns True if anything was copied, False if no real profile was found or the
    dest is already populated (we don't overwrite an existing Cookies file so the
    Playwright profile accumulates logins on its own after the first bootstrap).
    """
    # Don't clobber a Playwright profile that already has its own Cookies.
    if (dest / "Cookies").exists():
        logger.debug("mimic: Playwright profile already has Cookies, skipping import")
        return False

    profiles = _candidate_profiles()
    if not profiles:
        logger.info("mimic: no real browser profile found — starting with empty session")
        return False

    src = profiles[0]
    logger.info("mimic: bootstrapping Playwright profile from %s", src)
    dest.mkdir(parents=True, exist_ok=True)
    copied = False

    for filename in _PROFILE_FILES:
        src_file = src / filename
        if src_file.exists():
            try:
                shutil.copy2(src_file, dest / filename)
                copied = True
            except Exception:
                logger.debug("mimic: could not copy %s", src_file, exc_info=True)

    for dirname in _PROFILE_DIRS:
        src_dir = src / dirname
        if src_dir.exists():
            dst_dir = dest / dirname
            try:
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                copied = True
            except Exception:
                logger.debug("mimic: could not copy %s", src_dir, exc_info=True)

    if copied:
        logger.info("mimic: profile import done from %s", src)
    return copied


def launch_instructions(port: int = 9222) -> str:
    """Return a human-readable string telling the user how to launch Chrome with CDP."""
    return (
        f"Launch Chrome with --remote-debugging-port={port} so MIMIC can attach to your "
        f"real browser (with all your logins):\n\n"
        f"  Linux/macOS:  google-chrome --remote-debugging-port={port} --no-first-run\n"
        f"  macOS (app):  open -a 'Google Chrome' --args --remote-debugging-port={port}\n"
        f"  Windows:      chrome.exe --remote-debugging-port={port}\n\n"
        f"Then set  cdp_url: http://127.0.0.1:{port}  in the MIMIC plugin settings "
        f"(or set MIMIC_CDP_URL env var) and retry."
    )
