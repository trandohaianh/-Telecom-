"""Cấu hình chung: CSDL app trong bộ nhớ, tắt scheduler nền và rate-limit giữa các test."""

import os

os.environ.setdefault("FRAUD_APP_DB", ":memory:")
os.environ.setdefault("FRAUD_PIPELINE", "0")
# Tests score with the bundle every launcher serves (runtime/artifacts_v22), never the legacy
# root models/ whose feature contract predates new_callee_ratio_24h.
_bundle = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "artifacts_v22", "models"
)
if os.path.isfile(os.path.join(_bundle, "manifest.json")):
    os.environ.setdefault("FRAUD_MODEL_DIR", _bundle)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    import accounts

    accounts.limiter.reset()
    yield
    accounts.limiter.reset()
