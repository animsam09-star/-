"""리포트·프롬프트 렌더링 검증: 부호·갭·축 표기가 출력에 실제로 반영되는지 확인."""

import pytest

from src import analyze, report, universe

NAMES = {"000001": "테스트제련", "000002": "테스트전선", "000003": "테스트리더",
         "000004": "테스트라거드"}

HORIZONTAL = {
    "exposures": {"000003": {"구리": {"beta": 0.85, "corr": 0.41}}},
    "factor_moves": {"구리": {"ret5": 0.107, "ret20": 0.091}},
    "group_gaps": {
        "ETF:테스트그룹": {
            "group_move": 0.045,
            "members": [
                {"ticker": "000004", "beta": 0.98, "corr": 0.8,
                 "expected": 0.069, "actual": 0.029, "gap": 0.040},
                {"ticker": "000003", "beta": 1.38, "corr": 0.9,
                 "expected": 0.090, "actual": 0.159, "gap": -0.069},
            ],
        }
    },
    "membership": {"000003": ["ETF:테스트그룹"]},
    "groups": {"ETF:테스트그룹": ["000003", "000004"]},
}

ANALYSES = [{
    "ticker": "000003", "name": "테스트리더", "trigger": "강세지속", "ret20": 0.30,
    "cause_summary": "구리 가격 급등으로 판가 상승.", "cause_type": "업황개선(P/Q/C)",
    "cause_scope": "산업공통", "industry": "비철금속", "evidence": "7/22 업황 뉴스",
    "confidence": "높음", "ripple_axis": "수평", "groups": ["ETF:테스트그룹"],
    "ripple_paths": [{
        "direction": "공통동인", "target_industry": "전선",
        "logic": "구리는 전선의 주원재료라 원가로 작용한다.",
        "beneficiaries": [
            {"name": "테스트라거드", "impact": "수혜", "reason": "같은 그룹인데 미반응"},
            {"name": "테스트전선", "impact": "피해", "reason": "구리 원가 상승"},
        ],
    }],
}]

SYNTHESIS = {"market_summary": "구리 급등이 공통 동인.", "ideas": [{
    "title": "구리 급등 파급", "driver": "구리 가격", "axis": "수평",
    "path": "구리 상승 → 제련 수혜 / 전선 원가 부담",
    "beneficiaries": [
        {"name": "테스트라거드", "impact": "수혜", "priced_in": "미반영",
         "comment": "그룹 대비 뒤처짐", "gap": 0.040},
        {"name": "테스트전선", "impact": "피해", "priced_in": "확인불가",
         "comment": "원가 부담"},
    ],
    "watch_points": "구리 반락 시 역방향",
}]}

CANDIDATES = [{"ticker": "000003", "name": "테스트리더", "trigger": "강세지속",
               "close": 13000, "ret5": 0.13, "ret20": 0.30, "ret60": 0.40,
               "vol_surge": 3.1, "high_proximity": 1.0, "market_cap": 10 ** 12}]


@pytest.fixture
def analysis():
    return {"analyses": ANALYSES, "synthesis": SYNTHESIS}


def test_prompt_context_surfaces_unmoved_peer():
    ctx = analyze.build_horizontal_context("000003", HORIZONTAL, NAMES)
    assert "테스트라거드" in ctx
    assert "미반영" in ctx
    assert "구리" in ctx


def test_prompt_context_handles_stock_with_no_group():
    ctx = analyze.build_horizontal_context("999999", HORIZONTAL, NAMES)
    assert "없음" in ctx


def test_gap_is_injected_into_beneficiaries():
    analyses = [dict(a, ripple_paths=[dict(p, beneficiaries=[dict(b) for b in p["beneficiaries"]])
                                      for p in a["ripple_paths"]]) for a in ANALYSES]
    returns = {t: {"name": n, "ret5": 0.01, "ret20": 0.03} for t, n in NAMES.items()}
    uni = universe.from_entries("20260724", {
        t: {"name": n, "market": "KOSPI", "market_cap": 1} for t, n in NAMES.items()})
    analyze.check_priced_in(analyses, returns, uni, HORIZONTAL)
    laggard = analyses[0]["ripple_paths"][0]["beneficiaries"][0]
    assert laggard["gap"] == pytest.approx(0.040)
    assert laggard["gap_group"] == "ETF:테스트그룹"


def test_html_shows_axis_impact_and_gap(analysis):
    html = report.render_html("20260724", CANDIDATES, analysis, "테스트", HORIZONTAL, NAMES)
    assert "수평 파급" in html      # 수평 섹션
    assert "[피해]" in html         # 부호 구분
    assert "수평축" in html         # 축 표기
    assert "테스트라거드" in html


def test_html_survives_missing_horizontal(analysis):
    """수평 그래프 생성이 실패해도 리포트는 정상 렌더링돼야 한다."""
    html = report.render_html("20260724", CANDIDATES, analysis, "테스트", None, None)
    assert "테스트리더" in html
    assert "수평 파급" not in html


def test_html_shows_graph_provenance(analysis):
    """그래프 기반 후보와 그래프 밖 후보가 눈으로 구분돼야 한다."""
    enriched = {"analyses": ANALYSES, "synthesis": {**SYNTHESIS, "ideas": [
        {**SYNTHESIS["ideas"][0], "beneficiaries": [
            {**SYNTHESIS["ideas"][0]["beneficiaries"][0],
             "graph_backed": True, "via": "비철금속→전방→전선"},
            {**SYNTHESIS["ideas"][0]["beneficiaries"][1], "graph_backed": False},
        ]}]}}
    html = report.render_html("20260724", CANDIDATES, enriched, "테스트", HORIZONTAL, NAMES)
    assert "비철금속→전방→전선" in html
    assert "그래프 밖" in html


def test_coverage_section_surfaces_map_holes():
    """맵에 적어 놓고 티커로 해석되지 않은 회사는 리포트에 드러나야 한다."""
    out = report.render_coverage({
        "valuechain_coverage": {"industries": ["건설·EPC"], "edges": 12,
                                "unresolved": {"construction.yaml": ["없어진회사"]}},
        "provenance": {"graph_backed": 3, "total": 5},
        "name_match": {"matched": 4, "total": 5, "unmatched": ["오타난이름"]},
    })
    assert "없어진회사" in out
    assert "그래프 기반 3건" in out
    assert "오타난이름" in out


def test_coverage_section_is_empty_when_nothing_to_report():
    assert report.render_coverage({}) == ""


def test_telegram_marks_negative_impact(analysis):
    tg = report.render_telegram("20260724", CANDIDATES, analysis, None)
    assert "🔻" in tg          # 피해 종목
    assert "🟢" in tg          # 미반영 수혜 종목
    assert "수평축" in tg


def test_telegram_falls_back_without_analysis():
    tg = report.render_telegram("20260724", CANDIDATES,
                                {"analyses": [], "synthesis": None}, None)
    assert "테스트리더" in tg


# ---- 밸류체인 탭 ---------------------------------------------------------

class TestValuechainTab:
    """구축한 밸류체인을 눈으로 검증할 수 있어야 한다.

    이 그래프의 값어치는 관계 개수가 아니라 근거다. 인용문이 화면에 없으면
    사람은 그 관계를 믿을 근거가 없고, 그러면 그래프 자체가 무의미해진다.
    """

    def _edges(self):
        from src import graph as G
        return [
            G.make_edge(G.ticker_node("009540"), G.industry_node("조선"),
                        G.REL_MEMBER, "dart", origin="009540", asof="20260728",
                        evidence="조 선 제품 선 박 外 25,036,454(83.6%)"),
            G.make_edge(G.ticker_node("010140"), G.industry_node("조선"),
                        G.REL_MEMBER, "valuechain", asof="20260728"),
            G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260728",
                        evidence="〃 강 재 〃 2,156,875(19.2%)"),
            G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                        G.REL_UPSTREAM, "dart", origin="010620", asof="20260728",
                        evidence="철판, 형강 등 철강 제품은 POSCO, 현대제철 및 일본, 중국"),
            G.make_edge(G.industry_node("조선"), G.industry_node("해운"),
                        G.REL_DOWNSTREAM, "dart", origin="011200", asof="20260728",
                        evidence="메탄올 연료 추진 9,000TEU 급 컨테이너선 9척을 발주"),
        ]

    NAMES = {"009540": "HD한국조선해양", "010140": "삼성중공업",
             "010620": "HD현대미포", "011200": "HMM"}

    def _html(self):
        return report.render_valuechain(self._edges(), self.NAMES)

    def test_quotes_are_visible(self):
        """근거 문장이 화면에 없으면 관계를 믿을 방법이 없다."""
        h = self._html()
        assert "2,156,875(19.2%)" in h
        assert "컨테이너선 9척을 발주" in h

    def test_cross_validation_is_marked(self):
        """두 회사가 독립적으로 같은 관계를 말한 것이 드러나야 한다."""
        h = self._html()
        assert "×2" in h
        assert "HD한국조선해양" in h and "HD현대미포" in h

    def test_membership_source_is_distinguishable(self):
        """공시 근거가 있는 소속과 사람이 쓴 맵을 섞으면 신뢰도가 뭉개진다."""
        h = self._html()
        assert 'class="mchip m-dart" title="사업보고서 인용 근거 있음"' in h
        assert 'class="mchip m-map"' in h

    def test_upstream_and_downstream_are_separated(self):
        h = self._html()
        assert "후방(공급)" in h and "전방(수요)" in h

    def test_no_external_resources(self):
        """CDN을 부르면 그 호스트가 죽는 날 화면이 조용히 빈다."""
        h = self._html()
        for bad in ("http://", "https://", "src=", "@import"):
            assert bad not in h, f"외부 리소스 참조: {bad}"

    def test_empty_graph_does_not_crash(self):
        h = report.render_valuechain([], {})
        assert "밸류체인" in h
