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


def test_reserve_treats_zero_limit_as_unlimited(store):
    """#6: a per-dimension limit of 0 means 'not enforced', not 'block all'.
    A user capped on USD only (token limits 0) must not be denied on tokens."""
    store.put_user(user_id="usd_only", name="USD Only",
                   daily_usd=1.0, daily_input_tokens=0, daily_output_tokens=0)
    u = store.get_user("usd_only")
    # Token counts far above any normal per-request cap, but cheap enough to
    # stay within the $1 USD budget (1M in @0.15 + 1M out @0.60 = $0.75), so
    # only the (zeroed => unlimited) token dimensions could deny — they don't.
    decision = store.reserve(u, MODEL, est_input_tokens=1_000_000, max_output_tokens=1_000_000)
    assert decision.allowed


def test_sub_micro_positive_budget_does_not_round_to_unlimited(store):
    """#4: a positive budget below $0.000001 must NOT floor to 0 (which the
    '0 = unlimited' convention would read as no cap). It floors to 1 micro."""
    store.put_user(user_id="tiny", name="Tiny", daily_usd=0.0000004,
                   daily_input_tokens=0, daily_output_tokens=0)
    u = store.get_user("tiny")
    assert u.daily_usd_micro == 1  # 1 micro-USD, NOT 0/unlimited
    # And it actually enforces: any priced request exceeds a 1-micro cap.
    decision = store.reserve(u, MODEL, est_input_tokens=100, max_output_tokens=100)
    assert not decision.allowed
    # Exactly 0 stays unlimited (explicit opt-out, not a rounding artifact).
    store.set_user_limits("tiny", daily_usd=0.0)
    assert store.get_user("tiny").daily_usd_micro == 0


def test_reserve_zero_usd_limit_is_unlimited_cost(store):
    """A 0 USD limit likewise disables the cost dimension (block via status)."""
    store.put_user(user_id="no_usd_cap", name="No USD Cap",
                   daily_usd=0.0, daily_input_tokens=100, daily_output_tokens=100)
    u = store.get_user("no_usd_cap")
    decision = store.reserve(u, MODEL, est_input_tokens=10, max_output_tokens=10)
    assert decision.allowed  # cost unlimited; token dims have headroom


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
