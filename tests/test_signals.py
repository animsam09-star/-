"""스크리닝 시그널 검증: 합성 가격으로 강세지속·상승전환이 의도대로 잡히는지 확인."""

import numpy as np
import pandas as pd
import pytest

from src import krx_api, prices, screener


class TestDataSourceSelection:
    """어느 원천을 쓰는지가 조용히 결정되면 안 된다.

    둘 다 없는데 진행하면 '후보 0종목'이라는 그럴듯한 빈 결과가 나온다.
    실제로 프로덕션 실행이 pykrx 자격 증명 없이 그렇게 죽었다.
    """

    def _clear(self, monkeypatch):
        for env in (krx_api.ENV_KEY, "KRX_ID", "KRX_PW"):
            monkeypatch.delenv(env, raising=False)

    def test_no_credentials_names_both_options(self, monkeypatch):
        self._clear(monkeypatch)
        with pytest.raises(RuntimeError) as exc:
            prices.require_source()
        assert krx_api.ENV_KEY in str(exc.value)
        assert "KRX_ID" in str(exc.value)

    def test_openapi_key_wins_over_login(self, monkeypatch):
        """인증키가 있으면 로그인 경로를 쓰지 않는다 — 계정이 막혀도 돌아야 한다."""
        self._clear(monkeypatch)
        monkeypatch.setenv(krx_api.ENV_KEY, "k")
        monkeypatch.setenv("KRX_ID", "x")
        monkeypatch.setenv("KRX_PW", "y")
        assert prices.source() == "openapi"

    def test_partial_login_credentials_are_not_a_source(self, monkeypatch):
        """ID만 있고 PW가 없으면 '있는 것'으로 세면 안 된다."""
        self._clear(monkeypatch)
        monkeypatch.setenv("KRX_ID", "x")
        assert prices.source() == ""

    def test_empty_panel_is_not_reported_as_short_lookback(self, monkeypatch):
        """가격이 하나도 안 오는 것과 조회 기간이 짧은 것은 다른 문제다."""
        monkeypatch.setattr(prices, "fetch_panel", lambda *a, **kw: (
            [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
        with pytest.raises(RuntimeError, match="가격 스냅샷이 하나도"):
            screener.screen("20260724", {"lookback_days": 130})

    def test_missing_market_cap_stops_instead_of_skipping_the_filter(self, monkeypatch):
        """시총 컬럼이 없다고 필터를 건너뛰면 유니버스가 통째로 달라진다."""
        dates = pd.date_range("2026-01-01", periods=130, freq="B").strftime("%Y%m%d")
        close = pd.DataFrame(10000.0, index=dates, columns=["000001"])
        snap = pd.DataFrame({"name": ["가나"], "market_cap": [None]}, index=["000001"])
        monkeypatch.setattr(prices, "fetch_panel", lambda *a, **kw: (
            list(dates), close, close, close, snap))
        with pytest.raises(RuntimeError, match="시가총액이 없습니다"):
            screener.screen("20260724", dict(CFG, lookback_days=130))

from src.screener import compute_signals

N_DAYS = 130
CFG = {"golden_cross_window": 7, "momentum_ret20": 0.15,
       "high_proximity": 0.97, "volume_surge_ratio": 2.5}


@pytest.fixture
def panel():
    """모멘텀 / 상승전환 / 횡보 세 가지 성격의 종목을 만든다."""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2026-01-01", periods=N_DAYS, freq="B").strftime("%Y%m%d")

    momentum = 10000 * np.cumprod(1 + rng.normal(0.004, 0.01, N_DAYS))
    down = np.cumprod(1 + rng.normal(-0.003, 0.01, 95))
    up = np.cumprod(1 + rng.normal(0.012, 0.01, 35))
    turnaround = 10000 * np.concatenate([down, down[-1] * up])
    flat = 10000 * np.cumprod(1 + rng.normal(0, 0.008, N_DAYS))

    close = pd.DataFrame(
        {"000001": momentum, "000002": turnaround, "000003": flat}, index=dates)
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=close.columns)
    volume.iloc[-5:, 1] = 5_000_000  # 상승전환 종목만 거래량 급증
    return close, volume, close * volume


def test_momentum_stock_has_positive_returns(panel):
    sig = compute_signals(*panel, CFG)
    assert sig.loc["000001", "ret20"] > 0.05
    assert sig.loc["000001", "high_proximity"] > 0.95


def test_turnaround_stock_flags_volume_surge_and_trend_change(panel):
    sig = compute_signals(*panel, CFG)
    assert sig.loc["000002", "vol_surge"] > CFG["volume_surge_ratio"]
    assert sig.loc["000002", "golden_cross"] or sig.loc["000002", "slope_turn"]


def test_flat_stock_triggers_nothing(panel):
    sig = compute_signals(*panel, CFG)
    row = sig.loc["000003"]
    assert row["vol_surge"] < CFG["volume_surge_ratio"]
    assert not (row["golden_cross"] or row["slope_turn"])


def test_drops_stocks_with_insufficient_history(panel):
    close, volume, value = panel
    close = close.copy()
    close.loc[close.index[:100], "000003"] = np.nan  # 신규 상장 흉내
    sig = compute_signals(close, volume, value, CFG)
    assert "000003" not in sig.index
