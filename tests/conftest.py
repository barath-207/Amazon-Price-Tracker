"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure src is importable even without installing the package.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fx():
    return load_fixture


@pytest.fixture
def tmp_db(tmp_path):
    from tracker.database import Database

    return Database(tmp_path / "test.db")
