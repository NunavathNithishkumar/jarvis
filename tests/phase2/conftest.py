"""Phase 2 fixtures. Shared helpers live in p2harness.py (unique module name)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p2harness import Env  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
