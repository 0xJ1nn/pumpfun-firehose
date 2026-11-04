from __future__ import annotations

import pytest

from pumpfire.base58 import b58encode
from tests.helpers import MINT, USER


@pytest.fixture
def mint_str() -> str:
    return b58encode(MINT)


@pytest.fixture
def user_str() -> str:
    return b58encode(USER)
