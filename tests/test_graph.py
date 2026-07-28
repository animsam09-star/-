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


class TestCrossValidationSurvivesDedup:
    """DART 방식을 정당화하는 것은 '같은 관계가 독립된 두 문서에서 나온다'는 점이다.

    source는 'valuechain'이냐 'dart'냐만 구분한다. 그것만으로 중복 키를 잡으면
    시멘트사와 건설사가 각각 주장한 같은 관계가 하나로 합쳐지고 두 번째 근거가
    사라진다 — 실제로 '건설·EPC ←후방← 시멘트·레미콘'이 그렇게 소실됐다.
    """

    def _edge(self, origin, evidence):
        return G.make_edge(G.industry_node("건설·EPC"), G.industry_node("시멘트·레미콘"),
                           G.REL_UPSTREAM, "dart", origin=origin,
                           evidence=evidence, asof="20260728")

    def test_two_filings_asserting_the_same_relation_both_survive(self):
        merged = G.merge([self._edge("000720", "건설사 원재료: 레미콘"),
                          self._edge("300720", "시멘트사 매출처: 건설")])
        assert len(merged) == 2, "교차 검증 근거가 합쳐져 사라졌다"
        assert {e["origin"] for e in merged} == {"000720", "300720"}

    def test_same_filing_repeating_itself_is_still_deduped(self):
        """같은 공시가 같은 관계를 다시 주장하면 그건 갱신이지 검증이 아니다."""
        merged = G.merge([self._edge("300720", "구"), self._edge("300720", "신")])
        assert len(merged) == 1

    def test_origin_is_optional_and_defaults_to_empty(self):
        """밸류체인 YAML 엣지는 origin이 없다. 기존 동작이 깨지면 안 된다."""
        a = G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                        G.REL_UPSTREAM, "valuechain", asof="20260728")
        assert a["origin"] == ""
        assert len(G.merge([a, dict(a)])) == 1

    def test_different_sources_still_both_survive(self):
        vc = G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                         G.REL_UPSTREAM, "valuechain", asof="20260728")
        dart = G.make_edge(G.industry_node("조선"), G.industry_node("후판·강재"),
                           G.REL_UPSTREAM, "dart", origin="010140", asof="20260728")
        assert len(G.merge([vc, dart])) == 2


class TestAliasTableIsConsistentWithRealNodes:
    """별칭 표는 노드를 접으라고 있는 것이지, 쪼개라고 있는 게 아니다.

    실제로 `방위산업: 방산`이 들어가 있었다. defense.yaml의 노드 이름은
    '방위산업'인데 별칭이 그걸 '방산'으로 바꾸므로, 공시에서 '방위산업'이 나올
    때마다 YAML 노드와 이어지지 않는 **새 노드** '방산'이 생긴다. 방향이 뒤집힌
    별칭은 파편화를 막는 게 아니라 만들어 낸다. 조용히 일어나므로 테스트로 잡는다.
    """

    def _nodes(self):
        from src import dart_extract as dx
        nodes = set()
        for f in graph_build.VALUECHAIN_DIR.glob("*.yaml"):
            if f.name.startswith("_"):
                continue
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if doc.get("industry"):
                nodes.add(str(doc["industry"]))
            for block in (doc.get("upstream"), doc.get("downstream")):
                for seg in block or []:
                    if isinstance(seg, dict) and seg.get("segment"):
                        nodes.add(str(seg["segment"]))
        return nodes, dx.load_aliases()

    def test_every_alias_target_is_a_real_node(self):
        nodes, aliases = self._nodes()
        bad = {k: v for k, v in aliases.items() if v not in nodes}
        assert not bad, f"별칭이 존재하지 않는 노드를 가리킨다(방향이 뒤집혔을 수 있음): {bad}"

    def test_no_alias_source_is_itself_a_node(self):
        """왼쪽이 이미 노드면 그 노드가 통째로 다른 이름으로 넘어간다."""
        nodes, aliases = self._nodes()
        bad = {k: v for k, v in aliases.items() if k in nodes}
        assert not bad, f"실재하는 노드를 다른 이름으로 접고 있다: {bad}"

    def test_aliases_do_not_chain(self):
        """A→B, B→C는 한 번만 적용되므로 A가 C에 닿지 않는다."""
        _, aliases = self._nodes()
        chained = {k: v for k, v in aliases.items() if v in aliases}
        assert not chained, f"별칭이 연쇄한다 — 한 번만 적용되므로 끝까지 접히지 않는다: {chained}"


class TestIndustryGroupsDoNotBecomeDriverNodes:
    """산업 그룹은 갭 회귀용이지 동인이 아니다.

    소속은 이미 T→I 엣지로 있다. 여기서 D:산업:철강을 또 만들면 같은 관계가 두
    네임스페이스로 쪼개지고, 게다가 출처가 'etf_pdf'로 찍혀 ETF 구성종목이라는
    근거가 없는 엣지에 ETF 신뢰도가 붙는다.
    """

    MEMBERSHIP = {"005490": ["산업:철강", "테마:탄소중립"], "004020": ["산업:철강"]}

    def test_industry_groups_make_no_exposure_edges(self):
        edges = graph_build.from_groups(self.MEMBERSHIP, ASOF)
        assert not [e for e in edges if "산업:" in e["dst"]]

    def test_theme_groups_still_do(self):
        edges = graph_build.from_groups(self.MEMBERSHIP, ASOF)
        themes = [e for e in edges if e["dst"] == G.driver_node("테마:탄소중립")]
        assert len(themes) == 1
        assert themes[0]["source"] == "theme_index"
