"""Load the plugin directory as the package ``mimic``.

Hermes imports this directory by its installed name (``mimic``). The repository
is checked out under an unrelated directory name, so tests load ``__init__.py``
explicitly with its own search path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_package() -> None:
    if "mimic" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "mimic",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mimic"] = module
    spec.loader.exec_module(module)


_load_package()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "plugin-data"
    target.mkdir()
    monkeypatch.setenv("MIMIC_DATA_DIR", str(target))
    return target
