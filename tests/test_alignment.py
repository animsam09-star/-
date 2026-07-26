"""정합성 회귀 테스트.

여기 있는 테스트들은 전부 **조용히 틀린 답을 내는** 결함을 막기 위한 것이다.
예외가 나지 않아 오프라인 테스트를 통과해 버리는 종류라, 명시적으로 고정해 둔다.
"""

import numpy as np
import pandas as pd
import pytest

from src import analyze, factors, groups, universe

N_DAYS = 130
DATES = pd.date_range("2026-01-01", periods=N_DAYS, freq="B")
STR_DATES = DATES.strftime("%Y%m%d")


@pytest.fixture
def panels():
    """가격 패널은 문자열 날짜 인덱스, 팩터 패널은 pykrx 원본 그대로 DatetimeIndex."""
    rng = np.random.default_rng(3)
    mkt = rng.normal(0.0004, 0.009, N_DAYS)
    stock_a = mkt * 1.4 + rng.normal(0, 0.003, N_DAYS)   # 시장 민감
    stock_b = mkt * 0.3 + rng.normal(0, 0.003, N_DAYS)   # 시장 둔감

    def px(r):
        return 10000 * np.cumprod(1 + r)

    close = pd.DataFrame({"000001": px(stock_a), "000002": px(stock_b)}, index=STR_DATES)
    # pykrx의 get_index_ohlcv_by_date / get_etf_ohlcv_by_date는 DatetimeIndex를 준다
    factor_panel = pd.DataFrame({"시장": px(mkt)}, index=DATES)
    return close, factor_panel


def test_as_date_index_converts_datetime(panels):
    _, factor_panel = panels
    out = factors.as_date_index(factor_panel)
    assert out.index[0] == STR_DATES[0]
    assert not isinstance(out.index, pd.DatetimeIndex)


def test_as_date_index_is_idempotent_on_strings(panels):
    close, _ = panels
    assert list(factors.as_date_index(close).index) == list(close.index)


def test_market_factor_survives_datetime_index(panels):
    """DatetimeIndex 팩터 패널이 문자열 가격 패널과 정렬돼야 한다.

    정렬에 실패하면 시장 수익률이 전부 NaN → fillna(0) → 시장 요인이 0으로 취급되어
    2팩터 보정이 조용히 무력화된다. 예외는 나지 않는다.
    """
    close, factor_panel = panels
    exp = factors.compute_exposures(close, factor_panel, window=120)
    betas = exp.get("market_beta", {})
    assert betas, "시장 베타가 계산되지 않았다 — 인덱스 정렬 실패"
    assert betas["000001"] > 1.0, "시장 민감 종목의 베타가 1보다 커야 한다"
    assert betas["000002"] < 0.7, "시장 둔감 종목의 베타가 낮아야 한다"


def test_group_gap_survives_datetime_market_series(panels):
    close, factor_panel = panels
    group = {"ETF:테스트": ["000001", "000002"]}
    group_ret = groups.group_returns(close, group, min_members=2)
    market_ret = factor_panel["시장"].pct_change()  # DatetimeIndex 그대로 전달
    # 정렬 실패 시 유니버스 평균으로 대체되며, 어느 쪽이든 예외 없이 결과를 내야 한다
    gaps = factors.compute_group_gaps(close, group_ret, group, market_ret,
                                      window=120, recent=5, min_beta_corr=0.0)
    assert isinstance(gaps, dict)


def test_group_return_ignores_days_with_too_few_members():
    """거래정지로 한 종목만 남은 날의 등락이 그룹 수익률로 잡히면 안 된다."""
    close = pd.DataFrame({
        "000001": [100.0, 101.0, 102.0, 103.0],
        "000002": [100.0, 101.0, np.nan, np.nan],
        "000003": [100.0, 101.0, np.nan, np.nan],
    }, index=STR_DATES[:4])
    ret = groups.group_returns(close, {"G": ["000001", "000002", "000003"]}, min_members=3)
    assert not pd.isna(ret["G"].iloc[1]), "전 종목 유효한 날은 값이 있어야 한다"
    assert pd.isna(ret["G"].iloc[2]), "유효 종목이 부족한 날은 결측이어야 한다"


def test_halted_stock_is_not_reported_as_unmoved():
    """거래정지 종목이 '미반영 1순위'로 올라오면 안 된다.

    pandas는 pct_change에서 결측을 앞값으로 채우고(0% 수익률로 둔갑),
    sum()은 전부 결측인 구간을 0으로 돌려준다. 두 동작이 겹치면 정지 종목이
    '전혀 안 움직인 종목'으로 보여 갭 상위를 차지한다. 예외는 나지 않는다.
    """
    rng = np.random.default_rng(11)
    n = 130
    mkt = rng.normal(0.0005, 0.008, n)
    base = np.cumprod(1 + mkt)

    def series(mult, boost=0.0):
        r = mkt * mult + rng.normal(0, 0.003, n)
        r[-5:] += boost
        return 10000 * np.cumprod(1 + r)

    close = pd.DataFrame({
        "000001": series(1.0, 0.02),   # 그룹 리더 (상승)
        "000002": series(1.0, 0.02),
        "000003": series(1.0, 0.02),
        "000004": series(1.0, 0.02),   # 거래정지 예정 종목
    }, index=STR_DATES)
    close.iloc[-6:, close.columns.get_loc("000004")] = np.nan  # 최근 6일 거래정지

    group = {"ETF:테스트": list(close.columns)}
    group_ret = groups.group_returns(close, group, min_members=3)
    market_ret = pd.Series(10000 * base, index=STR_DATES).pct_change(fill_method=None)

    gaps = factors.compute_group_gaps(close, group_ret, group, market_ret,
                                      window=120, recent=5, min_beta_corr=0.0)
    reported = {m["ticker"] for m in gaps.get("ETF:테스트", {}).get("members", [])}
    assert "000004" not in reported, "거래정지 종목이 갭 판정에 포함되면 안 된다"


def _universe(names: dict[str, str], caps: dict[str, int] | None = None):
    return universe.from_entries("20260101", {
        t: {"name": n, "market": "KOSPI", "market_cap": (caps or {}).get(t, 1)}
        for t, n in names.items()})


class TestNameMatching:
    """LLM이 내놓은 종목명과 KRX 공식 표기의 차이를 흡수한다."""

    NAMES = {"000001": "LS ELECTRIC", "000002": "HD현대일렉트릭", "000003": "㈜한화"}

    def test_normalization_absorbs_spacing_and_entity_marks(self):
        uni = _universe(self.NAMES)
        assert uni.resolve("LS ELECTRIC") == "000001"
        assert uni.resolve("LSELECTRIC") == "000001"
        assert uni.resolve("한화") == "000003"
        assert uni.resolve("(주)한화") == "000003"

    def test_preferred_shares_are_not_merged_into_common(self):
        """'현대차우'를 '현대차'로 흡수하면 엉뚱한 종목에 수혜가 귀속된다."""
        uni = _universe({"005380": "현대차", "005385": "현대차우"})
        assert uni.resolve("현대차") == "005380"
        assert uni.resolve("현대차우") == "005385"

    def test_name_collision_prefers_larger_cap_and_is_recorded(self):
        uni = _universe({"000001": "동성", "000002": "동성"},
                        caps={"000001": 100, "000002": 900})
        assert uni.resolve("동성") == "000002"
        assert uni.collisions, "이름 충돌은 기록돼 드러나야 한다"

    def test_unmatched_names_are_reported_not_swallowed(self):
        """매칭 실패는 조용히 넘어가면 안 된다 — 갭도 채점도 전부 빠지기 때문이다."""
        analyses = [{"ripple_paths": [{"beneficiaries": [
            {"name": "HD현대일렉트릭"}, {"name": "존재하지않는회사"}]}]}]
        stats = analyze.check_priced_in(analyses, {}, _universe(self.NAMES))
        assert stats["matched"] == 1
        assert stats["unmatched"] == ["존재하지않는회사"]


def test_synthesis_beneficiaries_get_tickers():
    """사후 채점(score.py)은 종합 아이디어의 티커를 읽는다.

    여기 주입이 빠지면 채점이 항상 0건이 되는데, 예외도 경고도 나지 않는다.
    """
    synthesis = {"ideas": [{"beneficiaries": [{"name": "테스트전선"}]}]}
    horizontal = {"group_gaps": {"G": {"group_move": 0.05, "members": [
        {"ticker": "000009", "beta": 1.0, "corr": 0.7,
         "expected": 0.05, "actual": 0.01, "gap": 0.04}]}}}
    stats = analyze.check_priced_in(
        None, {"000009": {"ret5": 0.01, "ret20": 0.02}},
        _universe({"000009": "테스트전선"}), horizontal, synthesis)
    b = synthesis["ideas"][0]["beneficiaries"][0]
    assert stats["matched"] == 1
    assert b["ticker"] == "000009"
    assert b["gap"] == pytest.approx(0.04)
