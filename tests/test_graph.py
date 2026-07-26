"""관계 그래프 구조 테스트.

여기서 고정하는 것은 개별 계산이 아니라 **구조적 성질**이다.
- 서브그래프가 무관한 산업으로 새지 않는가 (프롬프트 폭발의 원인)
- 소형 소재주에서 출발해도 전방 수요처를 찾는가 (수직축의 존재 이유)
- 이름이 티커로 해석되는가, 실패가 드러나는가 (커버리지 구멍)
- 부호가 보존되는가 (구리 상승 = 제련 수혜 / 전선 피해)
"""

import yaml

from src import graph as G
from src import graph_build, universe

ASOF = "20260101"

NAMES = {
    "000001": "가건설", "000002": "나건설",
    "000003": "다시멘트", "000004": "라시멘트",
    "000005": "마리츠",
    "000010": "바조선", "000011": "사기자재",
    "010130": "고려아연", "006260": "엘에스전선",
}


def _universe():
    return universe.from_entries(ASOF, {
        t: {"name": n, "market": "KOSPI", "market_cap": 1000 - i}
        for i, (t, n) in enumerate(NAMES.items())})


def _write_maps(tmp_path):
    (tmp_path / "construction.yaml").write_text(yaml.safe_dump({
        "industry": "건설·EPC",
        "peers": ["가건설", "나건설"],
        "upstream": [{"segment": "시멘트", "companies": ["다시멘트", "라시멘트", "없는회사"]}],
        "downstream": [{"segment": "리츠", "companies": ["마리츠"]}],
    }, allow_unicode=True), encoding="utf-8")
    (tmp_path / "ship.yaml").write_text(yaml.safe_dump({
        "industry": "조선",
        "peers": ["바조선"],
        "upstream": [{"segment": "조선기자재", "companies": ["사기자재"]}],
    }, allow_unicode=True), encoding="utf-8")
    return tmp_path


def _graph(tmp_path):
    edges, report = graph_build.from_valuechain(_universe(), ASOF, _write_maps(tmp_path))
    return G.Graph(edges), report


class TestValueChainEdges:
    def test_names_are_resolved_to_tickers(self, tmp_path):
        g, _ = _graph(tmp_path)
        assert set(g.members_of("건설·EPC")) == {"000001", "000002"}
        assert set(g.members_of("시멘트")) == {"000003", "000004"}

    def test_unresolved_names_are_reported(self, tmp_path):
        """해석 실패를 조용히 버리면 커버리지 구멍이 보이지 않는다."""
        _, report = _graph(tmp_path)
        assert report["unresolved_count"] == 1
        assert "없는회사" in report["unresolved"]["construction.yaml"]

    def test_upstream_lookup(self, tmp_path):
        g, _ = _graph(tmp_path)
        ups = g.upstream("000001")
        assert [u["industry"] for u in ups] == ["시멘트"]
        assert set(ups[0]["members"]) == {"000003", "000004"}

    def test_reverse_lookup_from_small_cap_supplier(self, tmp_path):
        """소재주에서 출발해도 전방(건설)을 찾아야 한다.

        한 방향만 넣으면 소형 후방주에서 출발할 때 아무것도 안 나오는데,
        수직축이 실제로 잡아내야 하는 방향이 바로 그쪽이다.
        """
        g, _ = _graph(tmp_path)
        downs = g.downstream("000003")
        assert [d["industry"] for d in downs] == ["건설·EPC"]
        assert set(downs[0]["members"]) == {"000001", "000002"}

    def test_confidence_compounds_over_two_hops(self, tmp_path):
        g, _ = _graph(tmp_path)
        base = graph_build.VALUECHAIN_CONFIDENCE
        assert g.upstream("000001")[0]["confidence"] == round(base * base, 3)


class TestSubgraphIsolation:
    def test_subgraph_excludes_unrelated_industries(self, tmp_path):
        """건설 종목을 볼 때 조선 맵이 딸려 오면 안 된다.

        맵 전체를 덤프하던 이전 구조의 결함이다. 산업을 늘리면 프롬프트가
        선형으로 커져 확장이 불가능해진다. 예외는 나지 않는다.
        """
        g, _ = _graph(tmp_path)
        rendered = G.render_subgraph(g, "000001", _universe())
        assert "시멘트" in rendered
        assert "조선" not in rendered
        assert "사기자재" not in rendered

    def test_render_is_flat_in_number_of_industries(self, tmp_path):
        """산업 파일이 늘어도 한 종목의 렌더 크기는 변하지 않아야 한다."""
        g1, _ = _graph(tmp_path)
        before = len(G.render_subgraph(g1, "000001", _universe()))

        extra = tmp_path / "battery.yaml"
        extra.write_text(yaml.safe_dump({
            "industry": "이차전지", "peers": ["고려아연"],
            "upstream": [{"segment": "양극재", "companies": ["엘에스전선"]}],
        }, allow_unicode=True), encoding="utf-8")
        edges, _ = graph_build.from_valuechain(_universe(), ASOF, tmp_path)
        after = len(G.render_subgraph(G.Graph(edges), "000001", _universe()))
        assert before == after

    def test_unmapped_stock_renders_without_crashing(self, tmp_path):
        g, _ = _graph(tmp_path)
        out = G.render_subgraph(g, "999999", _universe())
        assert "미분류" in out


class TestExposureEdges:
    EXPOSURES = {
        "010130": {"구리": {"beta": 0.90, "corr": 0.62}},   # 제련 — 구리 상승 수혜
        "006260": {"구리": {"beta": -0.71, "corr": -0.55}},  # 전선 — 원가 부담
    }

    def test_factor_sign_is_preserved(self):
        edges = graph_build.from_factor_exposures(self.EXPOSURES, ASOF)
        sign = {G.split_node(e["src"])[1]: e["sign"] for e in edges}
        assert sign["010130"] == 1
        assert sign["006260"] == -1

    def test_co_exposed_separates_opposite_signs(self):
        g = G.Graph(graph_build.from_factor_exposures(self.EXPOSURES, ASOF))
        co = g.co_exposed("010130")
        assert len(co) == 1
        assert co[0]["driver"] == "구리"
        assert co[0]["my_sign"] == 1
        assert [(o["ticker"], o["sign"]) for o in co[0]["others"]] == [("006260", -1)]

    def test_opposite_sign_shows_up_in_render(self):
        g = G.Graph(graph_build.from_factor_exposures(self.EXPOSURES, ASOF))
        out = G.render_subgraph(g, "010130", _universe())
        assert "반대 방향" in out
        assert "엘에스전선" in out

    def test_group_membership_stays_bipartite(self):
        """구성종목 N개 그룹이 N개 엣지여야 한다.

        회사 쌍으로 저장하면 N(N-1)/2로 폭발한다. 80종목 ETF 하나가 3,160개다.
        """
        members = [f"{i:06d}" for i in range(80)]
        edges = graph_build.from_groups({t: ["ETF:테스트"] for t in members}, ASOF)
        assert len(edges) == 80

    def test_group_beta_is_attached_as_weight(self):
        gaps = {"ETF:테스트": {"group_move": 0.03, "members": [
            {"ticker": "000001", "beta": 1.4, "gap": 0.02}]}}
        edges = graph_build.from_groups({"000001": ["ETF:테스트"]}, ASOF, gaps)
        assert edges[0]["weight"] == 1.4


class TestMerge:
    def _edge(self, source, asof, conf):
        return G.make_edge("I:A", "I:B", G.REL_UPSTREAM, source,
                           confidence=conf, asof=asof)

    def test_same_source_newer_wins(self):
        merged = G.merge([self._edge("valuechain", "20260101", 0.5)],
                         [self._edge("valuechain", "20260201", 0.8)])
        assert len(merged) == 1
        assert merged[0]["confidence"] == 0.8

    def test_different_sources_both_survive(self):
        """밸류체인과 DART가 같은 관계를 주장하는 건 교차 검증이다 — 합치면 근거가 사라진다."""
        merged = G.merge([self._edge("valuechain", "20260101", 0.5)],
                         [self._edge("dart", "20260101", 0.9)])
        assert {e["source"] for e in merged} == {"valuechain", "dart"}

    def test_roundtrip_through_jsonl(self, tmp_path):
        edges = [self._edge("valuechain", "20260101", 0.5),
                 self._edge("dart", "20260101", 0.9)]
        path = G.save(edges, tmp_path / "edges.jsonl")
        assert G.load(path) == edges

    def test_load_skips_corrupt_lines(self, tmp_path):
        path = tmp_path / "edges.jsonl"
        path.write_text('{"src":"I:A"}\n{broken\n', encoding="utf-8")
        assert len(G.load(path)) == 1


class TestProvenance:
    """후보가 그래프에서 나왔는지 LLM이 만들었는지 남겨야 사후에 비교할 수 있다."""

    def test_reachability_labels_the_path(self, tmp_path):
        from src import analyze
        g, _ = _graph(tmp_path)
        reach = analyze.reachability(g, "000001")
        assert reach["000003"] == "건설·EPC→후방→시멘트"
        assert reach["000005"] == "건설·EPC→전방→리츠"
        assert reach["000002"] == "건설·EPC→동종"
        assert "000011" not in reach, "조선 기자재까지 닿으면 안 된다"

    def test_beneficiaries_are_marked_backed_or_invented(self, tmp_path):
        from src import analyze
        g, _ = _graph(tmp_path)
        reach = analyze.reachability(g, "000001")
        bens = [{"ticker": "000003", "name": "다시멘트"},
                {"ticker": "000011", "name": "사기자재"},
                {"name": "티커없음"}]
        stats = analyze.annotate_provenance(iter(bens), reach)
        assert bens[0]["graph_backed"] is True
        assert bens[0]["via"] == "건설·EPC→후방→시멘트"
        assert bens[1]["graph_backed"] is False
        assert "via" not in bens[1]
        assert stats == {"graph_backed": 1, "total": 2}


def test_unknown_relation_is_rejected():
    """관계 유형 오타가 조용히 통과하면 조회에서 영원히 안 잡힌다."""
    import pytest
    with pytest.raises(ValueError):
        G.make_edge("T:000001", "I:건설", "후방산업", "valuechain")
