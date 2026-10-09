"""company 网关配额数据源：key/info(5h) + v2/user/info(1mo) → RateLimits 映射。"""
from datetime import UTC, datetime

from token_tracker.adapters import company


def test_build_rate_limits_maps_5h_and_1mo():
    key_info = {
        "spend": 7.0,
        "max_budget": 7.0,
        "budget_duration": "5h",
        "budget_reset_at": "2026-10-09T10:00:00+00:00",
    }
    user_info = {
        "spend": 14.24,
        "max_budget": 73.6,
        "budget_duration": "1mo",
        "budget_reset_at": "2026-11-01T00:00:00Z",
    }
    rl = company.build_rate_limits(key_info, user_info)
    assert rl is not None
    assert rl.five_hour_pct == 100.0
    assert rl.five_hour_resets_at == int(datetime(2026, 10, 9, 10, 0, tzinfo=UTC).timestamp())
    assert rl.seven_day_pct == 19.3  # 14.24 / 73.6 * 100
    assert rl.seven_day_resets_at == int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())


def test_build_rate_limits_negative_spend_clamps_to_zero():
    rl = company.build_rate_limits(
        {"spend": -0.17, "max_budget": 7.0, "budget_duration": "5h", "budget_reset_at": None},
        {},
    )
    assert rl is not None
    assert rl.five_hour_pct == 0.0


def test_build_rate_limits_empty_returns_none():
    assert company.build_rate_limits({}, {}) is None
    assert company.build_rate_limits({"spend": 1.0, "max_budget": 0}, {}) is None


def test_is_company_provider_and_base_url():
    assert company.is_company_provider("company-seeway")
    assert company.is_company_provider("seeway")
    assert company.is_company_provider("deepseek") is False
    assert company.is_company_base_url("https://aigw.seewayai.com")
    assert company.is_company_base_url("https://api.anthropic.com") is False
