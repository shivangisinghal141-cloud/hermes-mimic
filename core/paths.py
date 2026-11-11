"""Where MIMIC keeps what it learned.

Learned macros are *data*, never plugin code. Rule 03 of the plugin catalog bans
self-updating code, so MIMIC never rewrites ``__init__.py`` or any other plugin
file: everything it creates or heals lands under ``plugin_data_dir()``.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ID = "mimic"


def data_dir(plugin_id: str = PLUGIN_ID) -> Path:
    """Return (and create) the durable data directory for this plugin.

    Uses Hermes' ``plugins.plugin_storage.plugin_data_dir`` when it is available
    (it follows the active profile and survives plugin updates/removal), and
    falls back to ``$MIMIC_DATA_DIR`` or ``~/.hermes/plugin-data/<id>`` when the
    plugin is used outside Hermes (tests, CLI experiments).
    """
    try:  # pragma: no cover - exercised only inside Hermes
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        path = Path(plugin_data_dir(plugin_id))
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        override = os.environ.get("MIMIC_DATA_DIR")
        root = Path(override) if override else Path.home() / ".hermes" / "plugin-data" / plugin_id
        root.mkdir(parents=True, exist_ok=True)
        return root
