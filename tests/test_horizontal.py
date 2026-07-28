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


# ---- KRX OpenAPI 경로의 팩터 대용치 ------------------------------------

class TestEtfPanelUsesGivenTradingDays:
    """ETF 패널은 거래일을 **다시 찾지 않는다.**

    종목 패널이 확정한 날짜를 그대로 받아야 두 패널의 인덱스가 문자 그대로
    같아진다. 어긋나면 pandas는 예외 없이 전부 NaN을 만들고, 팩터 회귀가
    조용히 0이 된다 — 리포트는 멀쩡해 보이는데 수평축만 죽는다.
    """

    RECORD = {
        "BAS_DD": "20260727", "ISU_CD": "KR7132030001", "ISU_NM": "KODEX 골드선물(H)",
        "IDX_IND_NM": "S&P GSCI Gold Index(TR)", "NAV": "13,455.21",
        "TDD_CLSPRC": "13,450", "TDD_OPNPRC": "13,400", "TDD_HGPRC": "13,500",
        "TDD_LWPRC": "13,380", "CMPPREVDD_PRC": "50", "FLUC_RT": "0.37",
        "ACC_TRDVOL": "1,234,567", "ACC_TRDVAL": "16,600,000,000",
        "MKTCAP": "500,000,000,000", "LIST_SHRS": "37,000,000",
        "OBJ_STKPRC_IDX": "2,101.55", "CMPPREVDD_IDX": "7.8", "FLUC_RT_IDX": "0.37",
        "INVSTASST_NETASST_TOTAMT": "497,843,770,000",
    }

    def _rows(self, day, price):
        a = dict(self.RECORD, BAS_DD=day, TDD_CLSPRC=f"{price:,}")
        b = dict(self.RECORD, BAS_DD=day, ISU_CD="KR7069500007",
                 ISU_NM="KODEX 200", TDD_CLSPRC=f"{price + 100:,}")
        return [a, b]

    def _patch(self, monkeypatch, seen):
        from src import krx_api

        def fake(category, endpoint, bas_dd, **kw):
            seen.append(bas_dd)
            return self._rows(bas_dd, 13000 + int(bas_dd[-2:]))

        monkeypatch.setattr(krx_api, "fetch_raw", fake)
        monkeypatch.setattr(krx_api, "CACHE_DIR", krx_api.CACHE_DIR)  # 경로 고정
        return krx_api

    def test_only_the_given_dates_are_requested(self, monkeypatch, tmp_path):
        from src import krx_api
        seen = []
        monkeypatch.setattr(krx_api, "CACHE_DIR", tmp_path)
        self._patch(monkeypatch, seen)

        dates = ["20260721", "20260722", "20260723"]
        close, catalog = krx_api.fetch_etf_panel(dates, use_cache=False)

        assert sorted(seen) == sorted(dates), "요청하지 않은 날짜를 조회했다"
        assert list(close.index) == dates, "패널이 오름차순이 아니다"

    def test_ticker_and_numerics_are_normalized(self, monkeypatch, tmp_path):
        from src import krx_api
        monkeypatch.setattr(krx_api, "CACHE_DIR", tmp_path)
        self._patch(monkeypatch, [])

        close, catalog = krx_api.fetch_etf_panel(["20260727"], use_cache=False)
        # 표준코드 12자리(KR7132030001) → 단축코드 6자리
        assert "132030" in close.columns, list(close.columns)
        # '13,450' 문자열이 그대로 남으면 종가 비교가 사전순이 된다
        assert close.loc["20260727", "132030"] == 13027
        assert catalog.loc["132030", "index_name"] == "S&P GSCI Gold Index(TR)"

    def test_index_name_is_not_folded_into_sector(self, monkeypatch, tmp_path):
        """ETF의 IDX_IND_NM은 기초지수명이지 업종이 아니다."""
        from src import krx_api
        monkeypatch.setattr(krx_api, "CACHE_DIR", tmp_path)
        self._patch(monkeypatch, [])
        _, catalog = krx_api.fetch_etf_panel(["20260727"], use_cache=False)
        assert "sector" not in catalog.columns


class TestFactorProxyResolution:
    """티커를 박지 않고 상품명으로 해석한다 — ETF는 상장·폐지가 잦다."""

    SPECS = [
        {"name": "금", "keywords": ["골드", "금현물"], "exclude": ["인버스", "레버리지"]},
        {"name": "구리", "keywords": ["구리"], "exclude": ["인버스", "레버리지"]},
    ]

    def _fake_panel(self, monkeypatch):
        from src import krx_api, prices
        dates = ["20260723", "20260724", "20260727"]
        cols = ["132030", "132031", "999999"]
        close = pd.DataFrame(
            [[13000, 26000, 9000], [13100, 26200, 9050], [13050, 26100, 9100]],
            index=dates, columns=cols)
        catalog = pd.DataFrame(
            {"name": ["KODEX 골드선물(H)", "KODEX 골드선물 레버리지", "TIGER 원유선물"]},
            index=cols)
        monkeypatch.setattr(krx_api, "fetch_etf_panel",
                            lambda d, use_cache=True: (close, catalog))
        monkeypatch.setattr(prices, "require_source", lambda: "openapi")
        return prices, dates

    def test_excluded_products_lose_and_shortest_name_wins(self, monkeypatch):
        prices, dates = self._fake_panel(monkeypatch)
        resolved, panel = prices.resolve_factor_proxies(self.SPECS, dates)
        assert resolved["금"]["ticker"] == "132030"
        assert "레버리지" not in resolved["금"]["proxy_name"]

    def test_unmatched_factor_is_dropped_loudly(self, monkeypatch, capsys):
        """조용히 빠지면 노출도 '0'과 '측정 안 됨'이 리포트에서 같아 보인다."""
        prices, dates = self._fake_panel(monkeypatch)
        resolved, panel = prices.resolve_factor_proxies(self.SPECS, dates)
        assert "구리" not in resolved
        assert "구리" in capsys.readouterr().out

    def test_panel_columns_are_factor_names_not_tickers(self, monkeypatch):
        prices, dates = self._fake_panel(monkeypatch)
        _, panel = prices.resolve_factor_proxies(self.SPECS, dates)
        assert list(panel.columns) == ["금"]
        assert list(panel.index) == dates


class TestMarketSeriesFromPanel:
    """지수 엔드포인트에 기대지 않고 이미 받아 둔 패널로 시장 요인을 만든다."""

    def _close(self):
        return pd.DataFrame(
            {"A": [100.0, 110.0, 121.0], "B": [100.0, 100.0, 100.0]},
            index=["20260723", "20260724", "20260727"])

    def test_cap_weighting_follows_the_large_cap(self):
        from src import prices
        caps = pd.Series({"A": 900.0, "B": 100.0})
        s = prices.market_series(self._close(), caps)
        assert s.loc["20260724"] == pytest.approx(0.09)   # 0.9*0.10 + 0.1*0.0

    def test_equal_weight_fallback_is_announced(self, capsys):
        from src import prices
        s = prices.market_series(self._close(), None)
        assert s.loc["20260724"] == pytest.approx(0.05)
        assert "동일가중" in capsys.readouterr().out

    def test_missing_names_renormalize_instead_of_shrinking(self):
        """상장 전 구간에서 결측이 0으로 취급되면 시장 수익률이 통째로 줄어든다."""
        from src import prices
        close = self._close()
        close.loc["20260723":"20260724", "B"] = float("nan")
        caps = pd.Series({"A": 900.0, "B": 100.0})
        s = prices.market_series(close, caps)
        # B가 없는 날은 A 혼자 100%가 되어야 한다 — 0.9로 축소되면 안 된다
        assert s.loc["20260724"] == pytest.approx(0.10)
