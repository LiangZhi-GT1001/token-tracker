"""公司 LiteLLM 网关配额数据源：查 /key/info（5h 窗口）与 /v2/user/info（1mo 窗口），
映射为 RateLimits（5h→five_hour、1mo→seven_day）。账号级、跨会话，文件缓存 + 短 TTL，
statusline 高频刷新只读缓存；key 从 env ANTHROPIC_AUTH_TOKEN / macOS keychain codex-company
解析，绝不落盘。供 codex-statusline 与 claude-statusline 在「无原生限额」时兜底显示公司配额。

# ponytail: 公司网关判定用 host 子串启发式（aigw/seeway/phigent）；接入第二个网关时再改 config 列表。
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime

from ..config import CONFIG_DIR
from .types import RateLimits

QUOTA_FILE = os.path.join(CONFIG_DIR, "tt-company-quota.json")
ENDPOINT = os.environ.get("TT_COMPANY_ENDPOINT", "https://aigw.phigent.cn")
FETCH_TIMEOUT = 6
TTL_SECONDS = 300
_COMPANY_HOSTS = ("aigw", "seeway", "phigent")


def is_company_provider(provider: str) -> bool:
    """Codex 会话的 model_provider 是否公司网关（company-seeway 等）。"""
    p = (provider or "").lower()
    return "company" in p or any(h in p for h in _COMPANY_HOSTS)


def is_company_base_url(url: str) -> bool:
    """CC 的 ANTHROPIC_BASE_URL 是否公司网关。"""
    return any(h in (url or "").lower() for h in _COMPANY_HOSTS)


def _key() -> str | None:
    for cand in (os.environ.get("ANTHROPIC_AUTH_TOKEN"), _keychain("codex-company")):
        if cand:
            return cand
    return None


def _keychain(service: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _fetch(path: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{ENDPOINT}{path}",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.load(resp)


def _pct(spend, budget):
    """spend ÷ budget → 0~100 的百分比；budget 缺失/非法返回 None。"""
    try:
        if not budget:
            return None
        pct = float(spend) / float(budget) * 100
        return max(0.0, min(100.0, round(pct, 1)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_reset(raw) -> int | None:
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def build_rate_limits(key_info: dict, user_info: dict) -> RateLimits | None:
    """把两个接口返回映射为 RateLimits；按 budget_duration 归属 5h/1mo，不绑定具体端点。"""
    five_pct = five_reset = None
    seven_pct = seven_reset = None
    for info, spend, budget, reset in (
        (key_info, key_info.get("spend"), key_info.get("max_budget"), key_info.get("budget_reset_at")),
        (user_info, user_info.get("spend"), user_info.get("max_budget"), user_info.get("budget_reset_at")),
    ):
        if budget is None:
            continue
        pct = _pct(spend, budget)
        resets = _parse_reset(reset)
        if "h" in str(info.get("budget_duration") or "").lower():
            five_pct, five_reset = pct, resets
        else:
            seven_pct, seven_reset = pct, resets
    if five_pct is None and seven_pct is None:
        return None
    return RateLimits(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_reset,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_reset,
    )


def _read_cache() -> RateLimits | None:
    try:
        with open(QUOTA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        age = datetime.now(UTC).timestamp() - float(data.get("_fetched_at"))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > TTL_SECONDS:
        return None
    return RateLimits(
        five_hour_pct=data.get("five_hour_pct"),
        five_hour_resets_at=data.get("five_hour_resets_at"),
        seven_day_pct=data.get("seven_day_pct"),
        seven_day_resets_at=data.get("seven_day_resets_at"),
    )


def _write_cache(rl: RateLimits) -> None:
    try:
        os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
        data = {
            "_fetched_at": datetime.now(UTC).timestamp(),
            "five_hour_pct": rl.five_hour_pct,
            "five_hour_resets_at": rl.five_hour_resets_at,
            "seven_day_pct": rl.seven_day_pct,
            "seven_day_resets_at": rl.seven_day_resets_at,
        }
        tmp = QUOTA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, QUOTA_FILE)
    except OSError:
        pass


def load_rate_limits() -> RateLimits | None:
    """缓存内直接返回；过期则同步取一次（接口实测 <300ms）。任何失败 fail-open 返回 None。"""
    try:
        cached = _read_cache()
        if cached is not None:
            return cached
        key = _key()
        if not key:
            return None
        key_info = (_fetch("/key/info", key) or {}).get("info") or {}
        user_info = _fetch("/v2/user/info", key) or {}
        rl = build_rate_limits(key_info, user_info)
        if rl is None:
            return None
        _write_cache(rl)
        return rl
    except Exception:
        return None
