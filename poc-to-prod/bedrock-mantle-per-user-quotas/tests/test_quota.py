import pytest

from app.pricing import MICRO, cost_micro_usd
from app.quota import QuotaStore, current_window


@pytest.fixture
def store(fake_dynamodb):
    return QuotaStore(dynamodb=fake_dynamodb)


@pytest.fixture
def user(store):
    store.put_user(
        user_id="alice", name="Alice",
        daily_usd=1.0, daily_input_tokens=100_000, daily_output_tokens=20_000,
    )
    return store.get_user("alice")


MODEL = "openai.gpt-oss-120b"


def test_get_or_provision_creates_with_defaults(store):
    user = store.get_or_provision_user("new-subject", name="New User")
    assert user.user_id == "new-subject"
    assert user.name == "New User"
    assert user.active
    assert user.daily_usd_micro > 0  # defaults applied

    # Second call returns the existing record, not a fresh one.
    again = store.get_or_provision_user("new-subject")
    assert again == user


def test_get_or_provision_does_not_reset_existing_limits(store, user):
    fetched = store.get_or_provision_user("alice")
    assert fetched.daily_input_tokens == 100_000  # kept, not defaulted


def test_reserve_within_budget_allows(store, user):
    decision = store.reserve(user, MODEL, est_input_tokens=1000, max_output_tokens=500)
    assert decision.allowed
    r = decision.reservation
    assert r.reserved_cost_micro == cost_micro_usd(MODEL, 1000, 500)
    usage = store.get_window_usage("alice")
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert usage["requests"] == 1


def test_reserve_denies_when_single_request_exceeds_budget(store, user):
    decision = store.reserve(user, MODEL, est_input_tokens=1, max_output_tokens=50_000)
    assert not decision.allowed  # output reservation alone > 20k daily output tokens
    assert store.get_window_usage("alice")["throttles"] == 1


def test_reserve_denies_when_accumulated_usage_hits_cost_limit(store, user):
    # Budget $1.00. Fallback-priced requests to burn it fast: use expensive model.
    expensive = "anthropic.claude-opus-4-7"  # 15/75 per MTok
    # One request reserving ~12k output tokens = ~$0.90.
    d1 = store.reserve(user, expensive, est_input_tokens=100, max_output_tokens=12_000)
    assert d1.allowed
    # Second identical request must be denied: 2 * 0.90 > 1.00.
    d2 = store.reserve(user, expensive, est_input_tokens=100, max_output_tokens=12_000)
    assert not d2.allowed
    assert d2.reason == "daily quota exceeded"


def test_settle_adjusts_down_to_actuals(store, user):
    decision = store.reserve(user, MODEL, est_input_tokens=1000, max_output_tokens=4096)
    charged = store.settle(decision.reservation, actual_input_tokens=900, actual_output_tokens=100)
    assert charged == cost_micro_usd(MODEL, 900, 100)
    usage = store.get_window_usage("alice")
    assert usage["input_tokens"] == 900
    assert usage["output_tokens"] == 100
    assert usage["cost_usd"] == pytest.approx(charged / MICRO)


def test_settle_failed_releases_everything(store, user):
    decision = store.reserve(user, MODEL, est_input_tokens=1000, max_output_tokens=4096)
    store.settle(decision.reservation, None, None, failed=True)
    usage = store.get_window_usage("alice")
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0
    assert usage["errors"] == 1
    assert usage["requests"] == 1  # the attempt is still counted


def test_settle_without_usage_keeps_reservation(store, user):
    decision = store.reserve(user, MODEL, est_input_tokens=1000, max_output_tokens=4096)
    charged = store.settle(decision.reservation, None, None)
    assert charged == decision.reservation.reserved_cost_micro
    usage = store.get_window_usage("alice")
    assert usage["input_tokens"] == 1000  # unchanged: conservative charge


def test_settle_with_cache_tokens_counts_them(store, user):
    """Prompt-cache traffic (Claude Code style) must hit budget + counters."""
    decision = store.reserve(user, MODEL, est_input_tokens=1000, max_output_tokens=4096)
    charged = store.settle(decision.reservation, actual_input_tokens=50,
                           actual_output_tokens=200,
                           cache_write_tokens=400, cache_read_tokens=9000)
    assert charged == cost_micro_usd(MODEL, 50, 200,
                                     cache_write_tokens=400, cache_read_tokens=9000)
    usage = store.get_window_usage("alice")
    assert usage["input_tokens"] == 50 + 400 + 9000  # cache counts as input volume
    assert usage["output_tokens"] == 200


def test_blocked_user_is_denied(store, user):
    store.set_user_status("alice", "blocked", "test")
    blocked = store.get_user("alice")
    decision = store.reserve(blocked, MODEL, 10, 10)
    assert not decision.allowed
    assert "blocked" in decision.reason


def test_budget_recovers_after_settle(store, user):
    """Reserve-heavy + settle-light must free budget for the next request."""
    expensive = "anthropic.claude-opus-4-7"
    d1 = store.reserve(user, expensive, est_input_tokens=100, max_output_tokens=12_000)
    assert d1.allowed
    # Settle down to almost nothing.
    store.settle(d1.reservation, actual_input_tokens=100, actual_output_tokens=50)
    # Now the same big reservation fits again.
    d2 = store.reserve(user, expensive, est_input_tokens=100, max_output_tokens=12_000)
    assert d2.allowed


def test_windows_are_isolated(store, user):
    decision = store.reserve(user, MODEL, 1000, 500)
    assert decision.allowed
    other = store.get_window_usage("alice", window="1999-01-01")
    assert other["requests"] == 0
    today = store.get_window_usage("alice", window=current_window())
    assert today["requests"] == 1
