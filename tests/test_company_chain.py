"""기업 대 기업 거래 추출.

산업 노드로는 수혜주를 못 고른다. '전공정 장비' 한 상자에 원익IPS·유진테크·
주성엔지니어링이 같이 들어가는데 셋은 만드는 장비가 다르다. 거래는 공시에
이름으로 적혀 있으므로, 그 이름을 티커로 바꿔 회사→회사로 잇는다.
"""

import pytest

from src import company_chain as cc, graph as G, universe as U

UNI = U.Universe("20260728", {t: {"name": n, "market_cap": 1} for t, n in {
    "018880": "한온시스템", "005380": "현대자동차", "012330": "현대모비스",
    "005490": "POSCO홀딩스", "010140": "삼성중공업", "082740": "한화엔진",
    "017960": "한국카본", "009830": "한화솔루션",
}.items()})


def _one(ticker, key, item):
    edges, unlisted = cc.from_extractions(
        {ticker: {"products": [], "upstream": [], "downstream": [], key: [item]}},
        UNI, "20260728")
    return edges, unlisted


class TestNamesBecomeTickers:
    def test_customer_names_become_company_edges(self):
        edges, _ = _one("018880", "downstream", {
            "customer": "현대자동차 21.4%·현대모비스 19.3%·Ford 12.5%",
            "industry": "완성차", "tier": "A", "quote": "q"})
        pairs = {(e["src"], e["dst"]) for e in edges}
        assert (G.ticker_node("018880"), G.ticker_node("005380")) in pairs
        assert (G.ticker_node("018880"), G.ticker_node("012330")) in pairs

    def test_revenue_share_is_attached_to_the_right_customer(self):
        """비중은 파급 크기를 가늠하는 유일한 정량 근거다. 옆 회사 것을 끌어오면 안 된다."""
        edges, _ = _one("018880", "downstream", {
            "customer": "현대자동차 21.4%·현대모비스 19.3%·Ford 12.5%",
            "industry": "완성차", "tier": "A", "quote": "q"})
        by_dst = {e["dst"]: e["weight"] for e in edges}
        assert by_dst[G.ticker_node("005380")] == pytest.approx(0.214)
        assert by_dst[G.ticker_node("012330")] == pytest.approx(0.193)

    def test_upstream_points_from_supplier_to_buyer(self):
        """방향은 물건이 흐르는 쪽으로 통일한다 — 산업 축과 같은 규약이라야 겹쳐 본다."""
        edges, _ = _one("010140", "upstream", {
            "material": "강재·형강(POSCO)", "industry": "후판·강재",
            "tier": "A", "quote": "q"})
        assert (edges[0]["src"], edges[0]["dst"]) == (
            G.ticker_node("005490"), G.ticker_node("010140"))


class TestFalsePositivesAreTheRealRisk:
    def test_quote_is_not_scanned_for_names(self):
        """인용문은 검증용이라 길고 무관한 회사가 들어 있다.

        실제로 삼성중공업의 강재 매입 인용문에서 한화엔진·한국카본이 잡혀
        '한화엔진이 삼성중공업에 강재를 판다'는 없는 거래가 만들어졌다.
        """
        edges, _ = _one("010140", "upstream", {
            "material": "강재·형강(POSCO)", "industry": "후판·강재", "tier": "A",
            "quote": "메인엔진은 한화엔진, 보냉재는 한국카본에서 조달한다"})
        suppliers = {e["src"] for e in edges}
        assert G.ticker_node("082740") not in suppliers
        assert G.ticker_node("017960") not in suppliers
        assert suppliers == {G.ticker_node("005490")}

    def test_generic_words_do_not_become_companies(self):
        """'전방산업'·'재료산업'을 회사로 잡으면 없는 회사를 만들어 낸다."""
        edges, unlisted = _one("018880", "downstream", {
            "customer": "전방산업·고객사·완성차 제조사", "industry": "완성차",
            "tier": "A", "quote": "q"})
        assert edges == []
        assert not any("산업" in u for u in unlisted.get("018880", []))

    def test_short_ambiguous_names_are_skipped(self):
        """'한화'가 한화솔루션인지 한화오션인지 문장만으로는 못 가른다."""
        edges, _ = _one("010140", "upstream", {
            "material": "한화", "industry": "석유화학", "tier": "A", "quote": "q"})
        assert edges == []

    def test_self_reference_is_not_a_trade(self):
        edges, _ = _one("005490", "downstream", {
            "customer": "POSCO홀딩스", "industry": "철강", "tier": "A", "quote": "q"})
        assert edges == []


def test_unlisted_counterparties_are_kept_not_dropped():
    """Ford·Caterpillar는 파급 대상이 아니지만 '누구에게 파는가'의 절반이다."""
    _, unlisted = _one("018880", "downstream", {
        "customer": "현대자동차 21.4%·Ford 12.5%", "industry": "완성차",
        "tier": "A", "quote": "q"})
    assert "Ford" in unlisted.get("018880", [])
