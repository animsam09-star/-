"""주가 상관을 밸류체인에 쓰는 방식의 경계를 고정한다.

여기서 지키는 규칙은 하나다. **엣지의 존재는 절대 가격에서 나오지 않는다.**
상관으로 엣지를 만들면, 같이 움직인 종목만 연결되고 안 움직인 종목은 그룹에서
빠진다 — 미반영 갭이 찾으려던 바로 그 종목이 사라진다. 순환 논리다.
"""

import numpy as np
import pandas as pd
import pytest

from src import corr
from src import graph as G

N = 260
DATES = pd.date_range("2025-01-01", periods=N, freq="B").strftime("%Y%m%d")


@pytest.fixture
def world():
    """시장 + 조선 고유 충격. 기자재는 조선을 5거래일 후행한다."""
    rng = np.random.default_rng(11)
    mkt = rng.normal(0.0002, 0.008, N)
    ship_shock = rng.normal(0, 0.012, N)

    ship = mkt * 1.1 + ship_shock + rng.normal(0, 0.002, N)
    # 5일 후행: 조선의 고유 충격이 5거래일 뒤 기자재에 실린다
    lagged = np.concatenate([np.zeros(5), ship_shock[:-5]])
    parts = mkt * 0.9 + lagged * 0.8 + rng.normal(0, 0.002, N)
    # 무관 산업: 시장에만 노출 — 원시 상관은 높지만 잔차 상관은 낮아야 한다
    unrelated = mkt * 1.0 + rng.normal(0, 0.004, N)

    def px(r):
        return 10000 * np.cumprod(1 + r)

    close = pd.DataFrame({
        "000001": px(ship), "000002": px(ship + rng.normal(0, 0.003, N)),
        "000011": px(parts), "000012": px(parts + rng.normal(0, 0.003, N)),
        "000021": px(unrelated), "000022": px(unrelated + rng.normal(0, 0.003, N)),
    }, index=DATES)
    members = {"조선": ["000001", "000002"],
               "조선 기자재": ["000011", "000012"],
               "무관산업": ["000021", "000022"]}
    caps = pd.Series({t: 1e12 for t in close.columns})
    market = pd.Series(px(mkt), index=DATES).pct_change()
    return close, caps, members, market


def _ind(world):
    close, caps, members, market = world
    return corr.residualize(corr.industry_returns(close, caps, members), market)


class TestPriceNeverCreatesEdges:
    """가격은 채점만 한다. 이게 깨지면 미반영 갭이 구조적으로 죽는다."""

    EDGES = [
        G.make_edge(G.industry_node("조선"), G.industry_node("조선 기자재"),
                    G.REL_UPSTREAM, "dart", origin="009540", asof="20260728"),
        G.make_edge(G.industry_node("조선"), G.industry_node("해운"),
                    G.REL_DOWNSTREAM, "dart", origin="011200", asof="20260728"),
    ]

    def test_edge_count_is_unchanged(self, world):
        scored = corr.score_edges(self.EDGES, _ind(world))
        assert len(scored) == len(self.EDGES)

    def test_unmeasurable_edge_survives_without_a_score(self, world):
        """'해운'은 수익률이 없다. 잴 수 없다고 엣지를 지우면 안 된다."""
        scored = corr.score_edges(self.EDGES, _ind(world))
        haeun = [e for e in scored if e["dst"] == G.industry_node("해운")]
        assert len(haeun) == 1
        assert "price_corr" not in haeun[0], "못 잰 것과 상관 0을 구분해야 한다"

    def test_original_edges_are_not_mutated(self, world):
        before = [dict(e) for e in self.EDGES]
        corr.score_edges(self.EDGES, _ind(world))
        assert self.EDGES == before

    def test_weak_edge_goes_to_review_not_deletion(self, world):
        """가격이 안 받쳐 준다고 지우면, 아직 가격에 안 실린 신생 관계가 사라진다."""
        edges = self.EDGES + [
            G.make_edge(G.industry_node("조선"), G.industry_node("무관산업"),
                        G.REL_UPSTREAM, "dart", origin="000000", asof="20260728")]
        scored = corr.score_edges(edges, _ind(world))
        assert len(scored) == len(edges), "검토 대상이 삭제됐다"
        queue = corr.review_queue(scored)
        assert any("무관산업" in r["dst"] for r in queue)


class TestMarketFactorIsRemoved:
    """시장을 안 빼면 KOSPI 아무 두 산업이나 '연관 있음'이 된다."""

    def test_unrelated_pair_loses_its_correlation(self, world):
        close, caps, members, market = world
        raw = corr.industry_returns(close, caps, members)
        resid = corr.residualize(raw, market)

        raw_c = abs(raw["조선"].corr(raw["무관산업"]))
        res_c = abs(resid["조선"].corr(resid["무관산업"]))
        assert raw_c > 0.4, f"원시 상관이 낮아 검증이 무의미하다({raw_c:.2f})"
        assert res_c < raw_c / 2, f"시장 요인이 제거되지 않았다 {raw_c:.2f}→{res_c:.2f}"

    def test_real_link_survives_residualization(self, world):
        resid = _ind(world)
        lag, c, n = corr.lead_lag(resid["조선"], resid["조선 기자재"])
        assert abs(c) > 0.3, f"실제 관계까지 지워졌다({c:.2f})"


class TestLeadLagIsMeasuredNotAssumed:
    """밸류체인 YAML에 '6~12개월 후행'이라고 손으로 적어 둔 값을 실측으로 바꾼다."""

    def test_recovers_the_planted_lag(self, world):
        resid = _ind(world)
        lag, c, n = corr.lead_lag(resid["조선"], resid["조선 기자재"])
        assert lag == 5, f"심어 둔 5일 시차를 찾지 못했다(lag={lag}, corr={c:.2f})"

    def test_sign_of_lag_says_who_leads(self, world):
        resid = _ind(world)
        fwd, _, _ = corr.lead_lag(resid["조선"], resid["조선 기자재"])
        rev, _, _ = corr.lead_lag(resid["조선 기자재"], resid["조선"])
        assert fwd > 0 > rev, f"선행·후행 방향이 뒤집혔다({fwd}, {rev})"

    def test_score_edges_records_the_lag(self, world):
        e = G.make_edge(G.industry_node("조선"), G.industry_node("조선 기자재"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260728")
        scored = corr.score_edges([e], _ind(world))
        assert scored[0]["price_lag"] == 5


class TestMultipleComparisons:
    """380만 쌍을 p<0.01로 걸러도 우연히 3.8만 쌍이 남는다."""

    def test_threshold_rises_with_pair_count(self):
        few = corr.corr_threshold(250, 10)
        many = corr.corr_threshold(250, 3_800_000)
        assert many > few, "쌍이 많아져도 임계가 그대로면 우연이 발굴로 둔갑한다"

    def test_threshold_falls_with_more_observations(self):
        assert corr.corr_threshold(1000, 1000) < corr.corr_threshold(100, 1000)

    def test_candidates_do_not_become_edges(self, world):
        """발굴 후보는 목록일 뿐이다. 공시로 확인한 뒤에만 엣지가 된다."""
        edges = []
        out = corr.candidates(_ind(world), edges)
        assert isinstance(out, list)
        for row in out:
            assert "공시로 확인" in row["note"]
        assert not edges, "후보 계산이 엣지 목록을 건드렸다"

    def test_existing_edges_are_excluded_from_candidates(self, world):
        edges = [G.make_edge(G.industry_node("조선"), G.industry_node("조선 기자재"),
                             G.REL_UPSTREAM, "dart", origin="009540", asof="20260728")]
        out = corr.candidates(_ind(world), edges)
        pairs = {(r["a"], r["b"]) for r in out}
        assert ("조선", "조선 기자재") not in pairs
        assert ("조선 기자재", "조선") not in pairs


class TestIndustryReturnsAreCapWeighted:
    def test_thin_industry_is_dropped(self, world):
        close, caps, members, _ = world
        out = corr.industry_returns(close, caps, {"외톨이": ["000001"]})
        assert "외톨이" not in out.columns, "한 종목짜리 산업은 그 종목 자체다"

    def test_day_with_too_few_live_names_is_masked(self, world):
        close, caps, members, _ = world
        close = close.copy()
        close.loc[DATES[100], "000002"] = np.nan
        out = corr.industry_returns(close, caps, {"조선": ["000001", "000002"]})
        assert pd.isna(out.loc[DATES[100], "조선"]), \
            "남은 한 종목의 등락이 산업 수익률로 둔갑했다"
