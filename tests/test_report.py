"""리포트·프롬프트 렌더링 검증: 부호·갭·축 표기가 출력에 실제로 반영되는지 확인."""

import re

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

class TestValuechainTabIsADiagram:
    """밸류체인은 방향이 있는 흐름이라 글보다 그림이 빠르게 읽힌다.

    이전 판은 표와 인용문이었다. 정보는 다 있었지만 '무엇이 무엇으로 흐르는가'가
    한눈에 안 들어왔다. 인용문은 화면에서 뺐다 — 근거는 그래프 파일에 그대로
    남아 있고, 매일 보는 화면에서는 오히려 시야를 가린다.
    """

    def _edges(self):
        from src import graph as G
        return [
            G.make_edge(G.ticker_node("009540"), G.industry_node("조선"),
                        G.REL_MEMBER, "dart", origin="009540", asof="20260728"),
            G.make_edge(G.ticker_node("010140"), G.industry_node("조선"),
                        G.REL_MEMBER, "valuechain", asof="20260728"),
            G.make_edge(G.ticker_node("005490"), G.industry_node("후판·강재"),
                        G.REL_MEMBER, "valuechain", asof="20260728"),
            G.make_edge(G.ticker_node("011200"), G.industry_node("해운"),
                        G.REL_MEMBER, "dart", origin="011200", asof="20260728"),
            G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260728",
                        evidence="〃 강 재 〃 2,156,875(19.2%)"),
            G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                        G.REL_UPSTREAM, "dart", origin="010620", asof="20260728",
                        evidence="철판, 형강 등 철강 제품은 POSCO, 현대제철"),
            G.make_edge(G.industry_node("조선"), G.industry_node("해운"),
                        G.REL_DOWNSTREAM, "dart", origin="011200", asof="20260728",
                        evidence="컨테이너선 9척을 발주"),
        ]

    NAMES = {"009540": "HD한국조선해양", "010140": "삼성중공업",
             "005490": "POSCO홀딩스", "011200": "HMM"}

    def _html(self):
        return report.render_valuechain(self._edges(), self.NAMES)

    def test_renders_svg_not_tables(self):
        h = self._html()
        assert "<svg" in h
        assert "<details>" not in h, "펼쳐 읽는 방식은 그림 우선과 어긋난다"

    def test_flow_direction_is_labelled(self):
        """방향이 없으면 후방과 전방이 뒤집혀도 알 수 없다."""
        h = self._html()
        assert "공급(후방)" in h and "수요(전방)" in h

    def test_member_stocks_are_on_the_diagram(self):
        """산업만 보이면 '그래서 뭘 사야 하나'에 답이 안 된다."""
        h = self._html()
        assert "HD한국조선해양" in h
        assert "POSCO홀딩스" in h
        assert "HMM" in h

    def test_cross_validation_shows_as_line_weight(self):
        """숫자를 읽지 않아도 신뢰도가 눈에 들어와야 한다."""
        from src import report as R
        thin = R._vc_link(0, 0, 10, 10, 1)
        thick = R._vc_link(0, 0, 10, 10, 5)
        def w(s): return float(re.search(r'stroke-width="([\d.]+)"', s).group(1))
        assert w(thick) > w(thin)

    def test_quotes_are_not_rendered(self):
        """근거는 그래프에 남기고 화면에서는 뺀다."""
        h = self._html()
        assert "2,156,875" not in h
        assert "컨테이너선 9척" not in h

    def test_no_external_resources(self):
        """CDN을 부르면 그 호스트가 죽는 날 화면이 조용히 빈다."""
        h = self._html()
        for bad in ("http://", "https://", "src=", "@import"):
            assert bad not in h, f"외부 리소스 참조: {bad}"

    def test_empty_graph_does_not_crash(self):
        assert "밸류체인" in report.render_valuechain([], {})


class TestValuechainIsActuallyConnected:
    """산업별 카드만으로는 사슬을 따라갈 수 없다.

    이전 판은 산업마다 '이웃 한 칸'짜리 카드를 따로 그렸다. 정보는 다 있었지만
    철광석 → 철강 → 후판 → 조선 → 해운처럼 **이어서** 보는 게 화면에서
    불가능했다 — 카드 61장이 서로 안 이어진 채 흩어져 있었던 셈이다.
    """

    def _flow(self, pairs):
        return {p: {"a"} for p in pairs}

    def test_chain_is_laid_out_left_to_right(self):
        """공급이 왼쪽, 수요가 오른쪽. 순서가 뒤집히면 그림이 거짓말을 한다."""
        flow = self._flow([("광산", "철강"), ("철강", "후판"),
                           ("후판", "조선"), ("조선", "해운")])
        pos, ncols, _, _ = report._vc_layout(flow)
        cols = {n: c for n, (c, _) in pos.items()}
        assert cols["광산"] < cols["철강"] < cols["후판"] < cols["조선"] < cols["해운"]
        assert ncols == 5

    def test_cycles_do_not_collapse_the_layout(self):
        """산업 연관은 실제로 순환한다 — 철강↔건설기계↔광산.

        위상정렬만 쓰면 62개 중 41개가 사이클에 걸려 층이 안 나왔다.
        """
        flow = self._flow([("철강", "건설기계"), ("건설기계", "광산"),
                           ("광산", "철강"), ("철강", "조선")])
        pos, ncols, _, dropped = report._vc_layout(flow)
        assert len(pos) == 4, "사이클에 걸린 산업이 지도에서 사라지면 안 된다"
        assert ncols >= 2
        assert len(dropped) == 1, "사이클을 끊는 간선은 최소여야 한다"

    def test_reachability_uses_the_acyclic_edges(self):
        """사이클이 남은 채 도달성을 재면 모두가 모두에 닿아 구분이 사라진다.

        실제로 원본 간선으로 계산했더니 철강·시멘트·조선이 전부 '후방 38 /
        전방 41'로 똑같이 나왔다. 62개 중 41개가 한 덩어리로 순환해서였다.
        """
        flow = self._flow([("철강", "건설기계"), ("건설기계", "광산"),
                           ("광산", "철강"), ("철강", "조선")])
        _, _, _, dropped = report._vc_layout(flow)
        fwd = {k: v for k, v in flow.items() if k not in dropped}
        up, down = report._vc_reach(fwd)
        sets = [(len(up.get(n, ())), len(down.get(n, ())))
                for n in ("철강", "건설기계", "광산", "조선")]
        assert len(set(sets)) > 1, "모든 산업의 사슬이 같으면 추적이 의미가 없다"
        assert "조선" not in up.get("조선", set())

    def test_map_draws_every_relation_once(self):
        """그래프에는 같은 관계가 후방·전방 두 방향으로 들어 있다.

        둘 다 읽으면 모든 선이 두 번 그려져 굵기(교차 검증 횟수)가 거짓이 된다.
        """
        from src import graph as G
        edges = [
            G.make_edge(G.industry_node("조선"), G.industry_node("후판"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260728"),
            G.make_edge(G.industry_node("후판"), G.industry_node("조선"),
                        G.REL_DOWNSTREAM, "dart", origin="009540", asof="20260728"),
        ]
        flow, _ = report._vc_flow(edges)
        assert flow == {("후판", "조선"): {"009540"}}

    def test_map_appears_in_the_page(self):
        h = report.render_valuechain(
            TestValuechainTabIsADiagram()._edges(),
            TestValuechainTabIsADiagram.NAMES)
        assert 'class="map"' in h
        assert "전체 흐름도" in h

    def test_member_names_come_from_the_full_master(self, tmp_path, monkeypatch):
        """이름을 스크리닝 결과에서 가져오면 소형 후방주가 티커로 찍힌다.

        returns_*.json은 시총·거래대금 필터를 통과한 종목만 담는다. 밸류체인이
        잡아내야 하는 대상이 바로 그 필터 밖 소재주라, 실제로 20종목이 '104700'
        같은 숫자로 화면에 나가고 있었다.
        """
        from src import graph as G, universe as U
        monkeypatch.setattr(report, "REPORTS_DIR", tmp_path)
        # load()의 기본 인자는 정의 시점에 묶이므로 EDGES_FILE만 갈아 끼우면
        # 실제 저장소 그래프를 읽는다. 그러면 이 테스트는 통과하되 아무것도
        # 검증하지 않게 된다 — 함수 자체를 바꿔야 격리된다.
        edges = self._member_edges()
        monkeypatch.setattr(G, "load", lambda *a, **k: edges)
        uni = U.Universe("20260728", {"104700": {"name": "한국철강",
                                                 "market_cap": 1}})
        report.build("20260728", [], {"analyses": []},
                     {"site_title": "테스트"}, None, uni)
        h = (tmp_path / "valuechain.html").read_text(encoding="utf-8")
        assert "한국철강" in h
        assert "104700" not in h

    def _member_edges(self):
        from src import graph as G
        return [G.make_edge(G.ticker_node("104700"), G.industry_node("철근·형강"),
                            G.REL_MEMBER, "valuechain", asof="20260728"),
                G.make_edge(G.industry_node("철근·형강"), G.industry_node("철강"),
                            G.REL_UPSTREAM, "dart", origin="104700",
                            asof="20260728")]

    def test_companies_in_one_industry_show_different_products(self):
        """같은 산업에 있다고 같은 걸 만드는 게 아니다.

        이름만 나열하면 '자동차 부품·모듈'의 종목들이 서로 대체재처럼 보인다.
        실제로는 제동장치·변속기·자동차 전선이고 수요가 움직이는 이유가 다르다.
        """
        from src import graph as G
        edges = [
            G.make_edge(G.ticker_node("204320"), G.industry_node("자동차 부품·모듈"),
                        G.REL_MEMBER, "dart", origin="204320", asof="20260728",
                        product="제동·조향·현가 장치"),
            G.make_edge(G.ticker_node("003570"), G.industry_node("자동차 부품·모듈"),
                        G.REL_MEMBER, "dart", origin="003570", asof="20260728",
                        product="차축·변속기"),
            G.make_edge(G.ticker_node("000500"), G.industry_node("자동차 부품·모듈"),
                        G.REL_MEMBER, "valuechain", asof="20260728"),
        ]
        h = report.render_valuechain(edges, {"204320": "HL만도",
                                             "003570": "SNT다이내믹스",
                                             "000500": "가온전선"})
        assert "제동·조향·현가 장치" in h
        assert "차축·변속기" in h
        # 품목을 못 뽑은 종목은 빈칸이 아니라 그렇다고 말해야 한다 — 빈칸이면
        # '안 만든다'인지 '아직 못 뽑았다'인지 구분이 안 된다.
        assert "공시에서 품목 미추출" in h

    def test_unconnected_industry_is_named_not_dropped(self):
        """흐름에 못 붙은 산업이 조용히 사라지면 '없는' 건지 '안 이어진' 건지 모른다."""
        from src import graph as G
        edges = TestValuechainTabIsADiagram()._edges() + [
            G.make_edge(G.ticker_node("000660"), G.industry_node("외톨이산업"),
                        G.REL_MEMBER, "valuechain", asof="20260728")]
        h = report.render_valuechain(edges, TestValuechainTabIsADiagram.NAMES)
        assert "외톨이산업" in h
        assert "아직 어느 사슬에도 안 붙은" in h


class TestPriceReviewSurvivesSkippedPairs:
    """구성 종목이 겹쳐 측정을 포기한 쌍은 corr 키가 아예 없다.

    corr.py에는 measured/skipped 구분을 넣어 두고 report.py에서만 빠뜨렸다.
    라이브에서 6쌍이 그 모양으로 들어오자 정렬 중 KeyError로 파이프라인 전체가
    죽었다 — 리포트 한 칸이 아니라 실행이 통째로 멈춘다.
    """

    LAGS = {
        "ESS·전력→분리막·전해액": {"lag_months": 0, "corr": 0.506,
                              "obs": 43, "overlap": 0.0},
        "ESS·전력→신재생": {"lag_months": -12, "corr": -0.509,
                         "obs": 31, "overlap": 0.0},
        "동제련→비철·소재": {"skipped": "구성 중복", "overlap": 0.5},
    }

    def _html(self, lags):
        return report.render_price_review({"price_review": {"cycle_lags": lags}})

    def test_skipped_pair_does_not_crash_the_report(self):
        h = self._html(self.LAGS)
        assert "ESS·전력 → 신재생" in h
        assert "-12개월" in h

    def test_skipped_pairs_are_counted_not_hidden(self):
        """조용히 지우면 '관계가 없다'로 읽힌다 — 뜻이 정반대다."""
        h = self._html(self.LAGS)
        assert "측정 불가 1쌍" in h

    def test_all_skipped_renders_nothing_rather_than_an_empty_section(self):
        assert self._html({"동제련→비철·소재": {"skipped": "구성 중복",
                                            "overlap": 0.5}}) == ""

    def test_real_lead_lag_file_renders(self):
        """실측 파일 그대로 통과해야 한다. 합성 데이터만 보면 같은 실수를 반복한다."""
        import json
        from pathlib import Path
        f = Path(__file__).resolve().parent.parent / "data" / "lead_lag.json"
        if not f.exists():
            pytest.skip("lead_lag.json 없음")
        table = json.loads(f.read_text(encoding="utf-8"))["lags"]
        assert report.render_price_review({"price_review": {"cycle_lags": table}})
