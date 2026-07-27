"""스크리닝 시그널 검증: 합성 가격으로 강세지속·상승전환이 의도대로 잡히는지 확인."""

import numpy as np
import pandas as pd
import pytest

from src import screener


class TestCredentialDiagnostics:
    """pykrx는 로그인 실패 시 예외 없이 빈 목록을 준다.

    그대로 두면 호출부에서 IndexError가 나고 진짜 원인(자격 증명 누락)이
    스택트레이스 어디에도 남지 않는다. 실제로 프로덕션 실행이 그렇게 죽었다.
    """

    def test_empty_dates_names_the_missing_credentials(self, monkeypatch):
        monkeypatch.delenv("KRX_ID", raising=False)
        monkeypatch.delenv("KRX_PW", raising=False)
        monkeypatch.setattr(screener.stock, "get_previous_business_days",
                            lambda **kw: [])
        with pytest.raises(RuntimeError, match="KRX_ID"):
            screener.get_trading_dates("20260724", 130)

    def test_empty_dates_with_credentials_blames_the_response(self, monkeypatch):
        monkeypatch.setenv("KRX_ID", "x")
        monkeypatch.setenv("KRX_PW", "y")
        monkeypatch.setattr(screener.stock, "get_previous_business_days",
                            lambda **kw: [])
        with pytest.raises(RuntimeError, match="KRX 응답이 비어"):
            screener.get_trading_dates("20260724", 130)

    def test_empty_panel_is_not_reported_as_short_lookback(self, monkeypatch):
        """가격이 하나도 안 오는 것과 조회 기간이 짧은 것은 다른 문제다."""
        monkeypatch.setattr(screener, "get_trading_dates", lambda *a: ["20260724"])
        monkeypatch.setattr(screener, "fetch_panel",
                            lambda dates: (pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
        with pytest.raises(RuntimeError, match="가격 스냅샷이 하나도"):
            screener.screen("20260724", {"lookback_days": 130})

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
