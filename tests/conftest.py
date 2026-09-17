"""Shared fixtures.

The tests run against the repo's own hosts/ and services/ — the example fleet is
the fixture. Tests that need a broken fleet build one in a temp directory and
point the loader at it with model.set_root.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lib import model  # noqa: E402


@pytest.fixture(autouse=True)
def real_root():
    """Every test starts pointed at the real repo, whatever the last one did."""
    model.set_root(REPO_ROOT)
    yield
    model.set_root(REPO_ROOT)


@pytest.fixture
def repo():
    return model.load_repo()


class Fleet:
    """A writable copy of the repo's declarative tree.

    Edit the files under it, call use(), and the loader sees your version
    instead of the real one.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def __truediv__(self, other: str) -> Path:
        return self.root / other

    def use(self) -> Path:
        model.set_root(self.root)
        return self.root


@pytest.fixture
def fleet(tmp_path):
    for name in ("hosts", "services", "cloudflare"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    return Fleet(tmp_path)
