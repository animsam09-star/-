"""수평 그래프 검증: 팩터 노출의 부호, 그룹 갭 랭킹, 시장 요인 분리를 확인.

여기서 검증하는 핵심 두 가지
1. 같은 동인이라도 업종에 따라 베타 부호가 갈려야 한다 (구리↑ → 제련 +, 전선 −).
2. 시장 요인을 분리하지 않으면 모든 종목이 시장을 통해 상관되어
   그룹과 무관한 종목까지 '미반영'으로 잡힌다.
"""

import numpy as np
import pandas as pd
import pytest

from src import factors, groups

N_DAYS = 130
GROUP = {"ETF:테스트그룹": ["000003", "000004", "000005"]}


@pytest.fixture
def market():
    """시장·구리 두 동인에 서로 다르게 노출된 종목들을 만든다."""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2026-01-01", periods=N_DAYS, freq="B").strftime("%Y%m%d")

    mkt = rng.normal(0.0003, 0.008, N_DAYS)
    copper = rng.normal(0.001, 0.015, N_DAYS)
    copper[-5:] += 0.02  # 최근 5일 구리 급등

    def px(r):
        return 10000 * np.cumprod(1 + r)

    smelter = mkt * 1.0 + copper * 0.9 + rng.normal(0, 0.004, N_DAYS)   # 구리 수혜
    cable = mkt * 1.0 - copper * 0.7 + rng.normal(0, 0.004, N_DAYS)     # 구리 피해
    leader = mkt * 1.1 + rng.normal(0, 0.005, N_DAYS)
    leader[-5:] += 0.025                                                # 그룹 리더 급등
    laggard = leader * 0.9 + rng.normal(0, 0.003, N_DAYS)
    laggard[-5:] -= 0.024                                               # 아직 미반응
    neutral = mkt * 0.9 + rng.normal(0, 0.006, N_DAYS)

    close = pd.DataFrame({
        "000001": px(smelter), "000002": px(cable), "000003": px(leader),
        "000004": px(laggard), "000005": px(neutral),
    }, index=dates)
    factor_panel = pd.DataFrame({"시장": px(mkt), "구리": px(copper)}, index=dates)
    market_ret = pd.Series(px(mkt), index=dates).pct_change()
    return close, factor_panel, market_ret


def test_factor_beta_sign_splits_by_business(market):
    """구리 상승은 제련에 수혜, 전선에 원가 부담 — 부호가 갈려야 한다."""
    close, factor_panel, _ = market
    exp = factors.compute_exposures(close, factor_panel, window=120, min_abs_corr=0.15)
    betas = {t: v["구리"]["beta"] for t, v in exp["exposures"].items() if "구리" in v}
    assert betas["000001"] > 0.3, "제련주는 구리 베타가 양수여야 한다"
    assert betas["000002"] < -0.2, "전선주는 구리 베타가 음수여야 한다"


def test_factor_move_is_captured(market):
    close, factor_panel, _ = market
    exp = factors.compute_exposures(close, factor_panel, window=120, min_abs_corr=0.15)
    assert exp["factor_moves"]["구리"]["ret5"] > 0.05


def test_gap_ranks_the_stock_that_has_not_moved(market):
    close, _, market_ret = market
    group_ret = groups.group_returns(close, GROUP)
    gaps = factors.compute_group_gaps(close, group_ret, GROUP, market_ret,
                                      window=120, recent=5, min_beta_corr=0.2)
    members = gaps["ETF:테스트그룹"]["members"]
    assert members[0]["ticker"] == "000004", "미반응 종목이 갭 1위여야 한다"
    assert members[0]["gap"] > 0.03
    # 이미 급등한 리더는 갭이 음수 (오히려 과반영)
    leader = next(m for m in members if m["ticker"] == "000003")
    assert leader["gap"] < 0


def test_gap_is_restricted_to_group_members(market):
    """시장 요인을 분리하지 않으면 그룹 밖 종목이 갭 상위로 잡히는 회귀를 막는다."""
    close, _, market_ret = market
    group_ret = groups.group_returns(close, GROUP)
    gaps = factors.compute_group_gaps(close, group_ret, GROUP, market_ret,
                                      window=120, recent=5, min_beta_corr=0.2)
    found = {m["ticker"] for m in gaps["ETF:테스트그룹"]["members"]}
    assert found <= set(GROUP["ETF:테스트그룹"]), f"그룹 밖 종목 포함: {found}"


def test_quiet_group_is_skipped(market):
    """그룹 고유 움직임이 없으면 미반영 판정 자체가 의미 없으므로 제외한다."""
    close, _, market_ret = market
    flat = pd.DataFrame({"ETF:조용한그룹": pd.Series(0.0, index=close.index)})
    gaps = factors.compute_group_gaps(close, flat, {"ETF:조용한그룹": list(close.columns)[:3]},
                                      market_ret, window=120, recent=5)
    assert "ETF:조용한그룹" not in gaps


def test_short_history_does_not_crash(market):
    """조회 기간이 베타 구간보다 짧아도 예외 없이 빈 결과를 낸다."""
    close, factor_panel, _ = market
    short = close.iloc[-40:]
    exp = factors.compute_exposures(short, factor_panel.iloc[-40:], window=120)
    assert isinstance(exp, dict)
