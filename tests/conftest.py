"""Cấu hình chung: CSDL app trong bộ nhớ, tắt scheduler nền và rate-limit giữa các test."""
import os

os.environ.setdefault("FRAUD_APP_DB", ":memory:")
os.environ.setdefault("FRAUD_PIPELINE", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    import accounts

    accounts.limiter.reset()
    yield
    accounts.limiter.reset()
