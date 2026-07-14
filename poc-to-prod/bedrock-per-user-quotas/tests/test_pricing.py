import pytest

from app.pricing import (
    FALLBACK_PRICE,
    MICRO,
    cost_micro_usd,
    get_price,
    reset_price_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_price_cache()
    yield
    reset_price_cache()


def test_known_model_cost():
    # openai.gpt-oss-120b: 0.15 / 0.60 per MTok (placeholder table)
    micro = cost_micro_usd("openai.gpt-oss-120b", 1_000_000, 1_000_000)
    assert micro == int((0.15 + 0.60) * MICRO)


@pytest.mark.parametrize(
    ("mantle_id", "native_id"),
    [
        ("openai.gpt-oss-120b", "openai.gpt-oss-120b-1:0"),
        ("openai.gpt-oss-20b", "openai.gpt-oss-20b-1:0"),
    ],
)
def test_native_bedrock_ids_share_mantle_prices(mantle_id, native_id):
    assert get_price(native_id) == get_price(mantle_id)


def test_unknown_model_uses_most_expensive_fallback():
    assert get_price("some.future-model") == FALLBACK_PRICE
    micro = cost_micro_usd("some.future-model", 1_000_000, 0)
    assert micro == int(FALLBACK_PRICE.input_per_mtok * MICRO)


def test_cost_rounds_up_and_never_negative():
    assert cost_micro_usd("openai.gpt-oss-120b", 1, 0) >= 1  # sub-micro rounds up
    assert cost_micro_usd("openai.gpt-oss-120b", -5, -5) == 0


def test_env_override(monkeypatch):
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"my.model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}',
    )
    reset_price_cache()
    assert cost_micro_usd("my.model", 1_000_000, 1_000_000) == 3 * MICRO
    # A deployment snapshot is authoritative; local defaults are not overlaid.
    assert get_price("openai.gpt-oss-120b") == FALLBACK_PRICE


def test_configured_unknown_model_fallback(monkeypatch):
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok": 40.0, "output_per_mtok": 90.0}',
    )
    reset_price_cache()
    assert cost_micro_usd("unknown.model", 1_000_000, 1_000_000) == 130 * MICRO


def test_cache_tokens_priced_with_multipliers():
    # gpt-oss-120b input = $0.15/MTok. Defaults: write x1.25, read x0.1.
    plain = cost_micro_usd("openai.gpt-oss-120b", 1_000_000, 0)
    write = cost_micro_usd("openai.gpt-oss-120b", 0, 0, cache_write_tokens=1_000_000)
    read = cost_micro_usd("openai.gpt-oss-120b", 0, 0, cache_read_tokens=1_000_000)
    assert write == int(plain * 1.25)
    assert read == int(plain * 0.1)
    # A cache read must be much cheaper than plain input — that's the point.
    assert read < plain < write
