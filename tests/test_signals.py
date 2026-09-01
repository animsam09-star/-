"""스크리닝 시그널 검증: 합성 가격으로 강세지속·상승전환이 의도대로 잡히는지 확인."""

import numpy as np
import pandas as pd
import pytest

from src import krx_api, prices, screener, universe


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


class TestScreenerEndToEnd:
    """원천 응답 형태 그대로 스크리너→종목 마스터를 통과시킨다.

    단위 테스트는 픽스처를 이미 깨끗한 DataFrame으로 만들기 때문에, 실제 응답의
    '쉼표 낀 문자열'과 '표준코드'가 어디서 깨지는지 잡지 못한다. 라이브 실행은
    20분이 걸려서 이런 버그를 거기서 발견하면 비싸다.
    """

    @pytest.fixture
    def fake_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr(screener, "DATA_DIR", tmp_path)
        monkeypatch.setattr(universe, "DATA_DIR", tmp_path)

        tickers = [f"{i:06d}" for i in range(1, 41)]
        dates = pd.date_range("2026-01-01", periods=70,
                              freq="B").strftime("%Y%m%d").tolist()

        def snapshot_for(i):
            recs = []
            for j, t in enumerate(tickers):
                px = 10000 * (1 + 0.004 * i + 0.001 * j)
                recs.append({
                    "BAS_DD": dates[i],
                    # 표준코드와 단축코드가 섞여 오는 상황을 재현한다
                    "ISU_CD": f"KR7{t}003" if j % 3 == 0 else t,
                    "ISU_NM": f"종목{j}",
                    "TDD_CLSPRC": f"{px:,.0f}",          # 쉼표 낀 문자열
                    "ACC_TRDVOL": "1,000,000",
                    "ACC_TRDVAL": "5,000,000,000",
                    "MKTCAP": f"{500_000_000_000 + j:,.0f}",
                    "_MARKET": "KOSPI",
                })
            return krx_api._snapshot_frame(recs)

        snaps = {d: snapshot_for(i) for i, d in enumerate(dates)}

        def fake_panel(end_date, n_days, use_cache=True):
            days = dates[-n_days:]
            frame = lambda col: pd.DataFrame(
                {d: snaps[d][col] for d in days}).T.sort_index()
            return (days, frame("close"), frame("volume"), frame("value"),
                    snaps[days[-1]])

        monkeypatch.setattr(prices, "fetch_panel", fake_panel)
        return dates

    CFG = dict(lookback_days=65, min_market_cap=1, min_avg_turnover=1,
               momentum_ret20=0.15, high_proximity=0.97, volume_surge_ratio=2.5,
               golden_cross_window=7, top_n=5)

    def test_string_prices_do_not_survive_into_signals(self, fake_source):
        result, close = screener.screen(fake_source[-1], self.CFG)
        assert close.dtypes.iloc[0].kind in "if", (
            f"종가가 {close.dtypes.iloc[0]} — 문자열이면 비교가 사전순이 된다")
        assert result["candidates"], "후보가 하나도 안 나왔다"

    def test_standard_codes_are_normalized_before_they_reach_the_master(self, fake_source):
        """표준코드가 남으면 DART·밸류체인과의 조인이 예외 없이 전부 빗나간다."""
        result, _ = screener.screen(fake_source[-1], self.CFG)
        uni = universe.load(result["base_date"])
        assert uni is not None, "종목 마스터가 저장되지 않았다"
        assert not [t for t in uni.entries if t.startswith("KR")]
        assert "000001" in uni

    def test_master_is_not_narrowed_by_the_screening_filter(self, fake_source):
        """시총 하한에 걸린 소형주도 이름 해석이 돼야 한다 — 이 모듈의 존재 이유다."""
        cfg = dict(self.CFG, min_market_cap=500_000_000_020)   # 대부분 탈락시킨다
        result, _ = screener.screen(fake_source[-1], cfg)
        uni = universe.load(result["base_date"])
        assert len(uni) == 40, f"마스터가 {len(uni)}종목으로 좁혀졌다"
        assert result["universe_size"] < 40, "필터가 실제로 걸리지 않아 검증이 무의미하다"

    def test_names_resolve_back_to_tickers(self, fake_source):
        result, _ = screener.screen(fake_source[-1], self.CFG)
        uni = universe.load(result["base_date"])
        assert uni.resolve("종목0") == "000001"
        assert uni.resolve("종목 0") == "000001", "공백 정규화가 안 된다"
