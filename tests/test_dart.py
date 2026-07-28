"""DART 추출 검증.

라이브 DART는 이 환경에서 막혀 있어 네트워크 경로는 스텁으로 대체한다.
여기서 고정하는 것은 **조용히 틀린 답을 내는** 지점들이다.
- 인용 검증이 실제로 환각을 걷어내는가 (이게 없으면 그럴듯한 거짓이 그대로 엣지가 된다)
- 목차를 본문으로 착각하지 않는가 (예외 없이 30자짜리 절이 나온다)
- 산업 어휘가 과·소 병합되지 않는가 (그래프 파편화 / 엉뚱한 병합)
- 표 셀 경계가 살아남는가 (원재료 비중이 뭉개지면 등급 A가 사라진다)
"""

import io
import json
import zipfile

import pytest

from src import dart, dart_extract as dx
from src import graph as G

ASOF = "20260726"

SECTION = """II. 사업의 내용

1. 사업의 개요
당사는 시멘트 및 레미콘을 제조·판매하고 있습니다.

2. 주요 제품 및 서비스
제품 | 매출액 | 비중
시멘트 | 8,000 | 62.0%
레미콘 | 4,900 | 38.0%

3. 원재료 및 생산설비
당사의 주요 원재료는 유연탄이며 전량 수입에 의존하고 있습니다.
원재료 | 매입액 | 비중
유연탄 | 3,200 | 71.0%
석회석 | 1,300 | 29.0%

4. 매출 및 수주상황
당사 제품은 국내 대형 건설사에 주로 납품되고 있습니다.
"""


def _doc(section: str = SECTION) -> dict:
    return {"ticker": "003410", "corp_code": "00126380", "corp_name": "테스트시멘트",
            "report_nm": "사업보고서", "rcept_dt": "20260315", "section": section,
            "truncated": False}


# ---- 절 추출 --------------------------------------------------------------

class TestSectionExtraction:
    def test_slices_business_section(self):
        text = ("I. 회사의 개요\n연혁입니다.\n" + SECTION
                + "\nIII. 재무에 관한 사항\n재무제표입니다.")
        out = dart.extract_business_section(text)
        assert "주요 제품 및 서비스" in out
        assert "재무제표입니다" not in out
        assert "연혁입니다" not in out

    def test_table_of_contents_is_not_mistaken_for_body(self):
        """목차에도 같은 제목이 있다. 첫 매치를 쓰면 30자짜리 절이 조용히 나온다."""
        text = ("- 목  차 -\nI. 회사의 개요\nII. 사업의 내용\nIII. 재무에 관한 사항\n\n"
                "I. 회사의 개요\n연혁\n\n" + SECTION + "\nIII. 재무에 관한 사항\n표\n")
        out = dart.extract_business_section(text)
        assert "유연탄" in out, "목차를 본문으로 착각했다"

    def test_full_width_roman_numerals(self):
        text = "Ⅰ. 회사의 개요\n\nⅡ. 사업의 내용\n" + "가" * 600 + "\nⅢ. 재무에 관한 사항\n"
        assert len(dart.extract_business_section(text)) >= 500

    def test_returns_empty_when_section_absent(self):
        """못 찾으면 통짜 문서를 넘기지 말고 비워야 한다 — 비용과 오독을 막는다."""
        assert dart.extract_business_section("I. 회사의 개요\n" + "가" * 5000) == ""

    def test_rejects_too_short_section(self):
        text = "II. 사업의 내용\n짧음\nIII. 재무에 관한 사항\n"
        assert dart.extract_business_section(text) == ""


# ---- 마크업 → 텍스트 ------------------------------------------------------

class TestXmlToText:
    def test_table_cells_keep_boundaries(self):
        """셀 경계가 뭉개지면 '어느 원재료가 몇 %'가 사라져 등급 A가 전멸한다."""
        xml = ("<TABLE><TR><TD>유연탄</TD><TD>3,200</TD><TD>71.0%</TD></TR>"
               "<TR><TD>석회석</TD><TD>1,300</TD><TD>29.0%</TD></TR></TABLE>")
        out = dart.xml_to_text(xml)
        assert "유연탄 | 3,200 | 71.0%" in out
        assert "석회석" in out.split("\n")[1]

    def test_entities_and_comments(self):
        out = dart.xml_to_text("<P>A&amp;B<!-- 주석 -->&nbsp;C</P>")
        assert "A&B" in out and "주석" not in out

    def test_numeric_character_references_are_decoded(self):
        """&#183;를 남기면 LLM은 그걸 보고 인용엔 '·'로 적어, 정상 관계가
        인용 검증에서 환각으로 오판돼 폐기된다. 예외는 나지 않는다."""
        out = dart.xml_to_text("<P>제조&#183;판매 &#xB7; 유통</P>")
        assert "제조·판매" in out
        assert "&#" not in out

    def test_te_cells_are_handled(self):
        # DART는 헤더 셀에 TE 태그를 쓴다
        assert "제품 | 비중" in dart.xml_to_text("<TR><TE>제품</TE><TE>비중</TE></TR>")


class TestDecoding:
    def test_uses_declared_encoding(self):
        raw = '<?xml version="1.0" encoding="euc-kr"?><P>시멘트</P>'.encode("euc-kr")
        assert "시멘트" in dart._decode(raw)

    def test_falls_back_when_declaration_missing(self):
        assert "시멘트" in dart._decode("<P>시멘트</P>".encode("cp949"))

    def test_utf8_document(self):
        assert "시멘트" in dart._decode("<P>시멘트</P>".encode("utf-8"))


def test_fetch_document_reports_error_body(monkeypatch, tmp_path):
    """오류 응답은 ZIP이 아니라 XML로 온다. 조용히 빈 문서가 되면 안 된다."""
    monkeypatch.setattr(dart, "DOC_CACHE", tmp_path)

    class Resp:
        content = '<result><status>013</status><message>없음</message></result>'.encode("utf-8")

    monkeypatch.setattr(dart, "_get", lambda *a, **k: Resp())
    with pytest.raises(dart.DartError, match="문서 조회 실패"):
        dart.fetch_document("20260315000001")


def test_fetch_document_picks_largest_xml(monkeypatch, tmp_path):
    monkeypatch.setattr(dart, "DOC_CACHE", tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cover.xml", "<P>표지</P>".encode("cp949"))
        zf.writestr("body.xml", ("<P>" + "본문" * 500 + "</P>").encode("cp949"))

    class Resp:
        content = buf.getvalue()

    monkeypatch.setattr(dart, "_get", lambda *a, **k: Resp())
    out = dart.fetch_document("20260315000002")
    assert "본문" in out and "표지" not in out
    assert (tmp_path / "20260315000002.txt").exists(), "캐시가 기록돼야 한다"


# ---- 인용 검증 ------------------------------------------------------------

class TestQuoteVerification:
    def test_accepts_verbatim_quote(self):
        assert dx.verify_quote("주요 원재료는 유연탄이며 전량 수입에 의존", SECTION)

    def test_accepts_reflowed_whitespace(self):
        """표를 읽으면서 줄바꿈이 달라지는 건 환각이 아니다."""
        assert dx.verify_quote("주요 원재료는\n유연탄이며  전량 수입에 의존", SECTION)

    def test_rejects_fabricated_quote(self):
        assert not dx.verify_quote("당사는 반도체 웨이퍼를 주요 원재료로 매입합니다", SECTION)

    def test_rejects_too_short_quote(self):
        """짧은 인용은 우연히 일치한다 — 근거로 인정하면 검증이 무력해진다."""
        assert not dx.verify_quote("시멘트", SECTION)

    def test_accepts_short_table_row_with_figures(self):
        """표 행은 짧지만 가장 강한 근거다. 산문 하한을 그대로 적용하면
        등급 A 근거가 통째로 폐기된다 — 예외 없이 조용히."""
        assert dx.verify_quote("레미콘 | 4,900 | 38.0%", SECTION)
        assert dx.verify_quote("유연탄 | 3,200", SECTION)

    def test_still_rejects_bare_figure(self):
        """수치만 떼어 오면 어느 항목의 것인지 알 수 없어 근거가 아니다."""
        assert not dx.verify_quote("38.0%", SECTION)

    def test_hallucinated_relation_is_dropped(self):
        """이 테스트가 이 모듈의 존재 이유다. 예외 없이 그럴듯한 거짓이 통과하면 안 된다."""
        extraction = {
            "products": [{"industry": "시멘트·레미콘", "product": "시멘트",
                          "revenue_share": 0.62, "tier": "A",
                          "quote": "시멘트 | 8,000 | 62.0%"}],
            "upstream": [
                {"industry": "석탄", "material": "유연탄", "cost_share": 0.71, "tier": "A",
                 "quote": "주요 원재료는 유연탄이며 전량 수입에 의존"},
                {"industry": "반도체", "material": "웨이퍼", "cost_share": 0.4, "tier": "B",
                 "quote": "당사는 반도체 웨이퍼를 주요 원재료로 매입합니다"},
            ],
            "downstream": [], "unmapped": "",
        }
        clean, stats = dx.filter_by_quote(extraction, SECTION)
        assert [u["industry"] for u in clean["upstream"]] == ["석탄"]
        assert stats["dropped"] == 1 and stats["kept"] == 2
        assert stats["dropped_samples"], "폐기 내역이 드러나야 한다"


# ---- 어휘 통제 ------------------------------------------------------------

class TestIndustryVocab:
    def test_normalization_folds_suffix_and_spacing(self):
        assert dx.normalize_industry("시멘트 제조업") == dx.normalize_industry("시멘트")
        assert dx.normalize_industry("건설 산업") == dx.normalize_industry("건설")

    def test_normalization_does_not_gut_short_names(self):
        """'농업'을 '농'으로 깎으면 엉뚱한 산업과 붙는다."""
        assert dx.normalize_industry("농업") == "농업"

    def test_distinct_industries_stay_distinct(self):
        assert dx.normalize_industry("시멘트") != dx.normalize_industry("시멘트·레미콘")

    def test_resolves_to_existing_vocabulary(self):
        vocab = dx.IndustryVocab(known=["건설·EPC", "시멘트·레미콘"])
        assert vocab.resolve("건설·EPC 산업") == "건설·EPC"
        assert not vocab.new

    def test_alias_folds_what_normalization_cannot(self):
        vocab = dx.IndustryVocab(known=["시멘트·레미콘"], aliases={"레미콘": "시멘트·레미콘"})
        assert vocab.resolve("레미콘") == "시멘트·레미콘"

    def test_new_industry_is_recorded_not_swallowed(self):
        vocab = dx.IndustryVocab(known=["건설·EPC"])
        assert vocab.resolve("우주항공") == "우주항공"
        assert vocab.new == {"우주항공": 1}

    def test_same_new_industry_is_reused_within_a_run(self):
        vocab = dx.IndustryVocab(known=[])
        vocab.resolve("우주항공")
        assert vocab.resolve("우주항공 산업") == "우주항공", "같은 실행 안에서 갈라지면 안 된다"

    def test_prefix_overlap_is_suggested_not_auto_merged(self):
        """'건설'과 '건설·EPC'는 후보로 제안하되 자동 병합하면 안 된다 —
        같은 규칙이 '반도체'와 '반도체 장비'도 잘못 붙여 버린다."""
        vocab = dx.IndustryVocab(known=["건설·EPC", "반도체 장비"])
        assert vocab.resolve("건설") == "건설", "자동 병합됐다"
        vocab.resolve("반도체")
        sug = vocab.alias_suggestions()
        assert sug["건설"] == ["건설·EPC"]
        assert sug["반도체"] == ["반도체 장비"]

    def test_no_suggestion_when_nothing_overlaps(self):
        vocab = dx.IndustryVocab(known=["건설·EPC"])
        vocab.resolve("해운")
        assert vocab.alias_suggestions() == {}


# ---- 엣지 변환 ------------------------------------------------------------

class TestToEdges:
    EXTRACTION = {
        "products": [
            {"industry": "레미콘", "product": "레미콘", "revenue_share": 0.38,
             "tier": "A", "quote": "q"},
            {"industry": "시멘트·레미콘", "product": "시멘트", "revenue_share": 0.62,
             "tier": "A", "quote": "q"},
        ],
        "upstream": [{"industry": "석탄", "material": "유연탄", "cost_share": 0.71,
                      "tier": "A", "quote": "q"}],
        "downstream": [{"industry": "건설·EPC", "customer": "국내 건설사",
                        "tier": "B", "quote": "q"}],
        "unmapped": "",
    }

    def _graph(self):
        vocab = dx.IndustryVocab(known=["시멘트·레미콘", "건설·EPC", "석탄"],
                                 aliases={"레미콘": "시멘트·레미콘"})
        return G.Graph(dx.to_edges("003410", self.EXTRACTION, vocab, ASOF)), vocab

    def test_membership_needs_no_name_resolution(self):
        """공시는 그 회사 자신의 것이라 티커를 이미 안다 — 이름 매칭 취약점이 없다."""
        g, _ = self._graph()
        assert g.industries_of("003410"), "소속 엣지가 있어야 한다"
        assert set(g.members_of("시멘트·레미콘")) == {"003410"}

    def test_primary_industry_is_largest_revenue_share(self):
        g, _ = self._graph()
        ups = g.upstream("003410")
        assert {u["from_industry"] for u in ups} == {"시멘트·레미콘"}, \
            "비중 62%인 시멘트가 기준 산업이어야 한다"

    def test_reciprocal_edges_allow_reverse_lookup(self):
        g, _ = self._graph()
        assert [d["industry"] for d in g.downstream("003410")] == ["건설·EPC"]
        # 석탄 산업에서 출발해도 시멘트를 전방으로 찾을 수 있어야 한다
        assert any(e["dst"] == G.industry_node("시멘트·레미콘")
                   for e in g.out(G.industry_node("석탄"), G.REL_DOWNSTREAM))

    def test_aliased_products_are_folded_into_one_edge(self):
        """별칭으로 같은 산업이 되면 엣지가 하나여야 한다.

        접지 않으면 같은 엣지가 두 번 생기고, 매출 비중은 둘 중 하나만 임의로
        남는다(38%가 62%를 덮을 수도 있다). 예외는 나지 않는다.
        """
        g, _ = self._graph()
        member_edges = g.industries_of("003410")
        assert len(member_edges) == 1, f"중복 소속 엣지: {member_edges}"
        assert member_edges[0]["weight"] == pytest.approx(1.0), \
            "같은 산업 안의 두 제품 비중은 합산돼야 한다"

    def test_dart_confidence_beats_handwritten_valuechain(self):
        from src import graph_build
        g, _ = self._graph()
        tier_a = [e for e in g.edges if e["confidence"] == dx.TIER_CONFIDENCE["A"]]
        assert tier_a
        assert dx.TIER_CONFIDENCE["C"] > graph_build.VALUECHAIN_CONFIDENCE

    def test_self_referential_relation_is_skipped(self):
        vocab = dx.IndustryVocab(known=["시멘트·레미콘"])
        edges = dx.to_edges("003410", {
            "products": [{"industry": "시멘트·레미콘", "product": "시멘트",
                          "revenue_share": 1.0, "tier": "A", "quote": "q"}],
            "upstream": [{"industry": "시멘트·레미콘", "material": "클링커",
                          "cost_share": 0.5, "tier": "B", "quote": "q"}],
            "downstream": [], "unmapped": "",
        }, vocab, ASOF)
        assert all(e["rel"] == G.REL_MEMBER for e in edges), "자기 자신으로 가는 관계는 무의미하다"

    def test_products_without_share_still_yield_primary(self):
        vocab = dx.IndustryVocab(known=[])
        edges = dx.to_edges("000001", {
            "products": [{"industry": "조선", "product": "선박", "revenue_share": None,
                          "tier": "C", "quote": "q"}],
            "upstream": [{"industry": "철강", "material": "후판", "cost_share": None,
                          "tier": "B", "quote": "q"}],
            "downstream": [], "unmapped": "",
        }, vocab, ASOF)
        assert any(e["rel"] == G.REL_UPSTREAM for e in edges)


def test_dart_edges_merge_alongside_valuechain_not_over_it():
    """같은 관계를 둘이 주장하면 교차 검증이다 — 합치면 근거가 사라진다."""
    vc = G.make_edge("I:건설·EPC", "I:시멘트·레미콘", G.REL_UPSTREAM,
                     "valuechain", confidence=0.5, asof=ASOF)
    dart_edge = G.make_edge("I:건설·EPC", "I:시멘트·레미콘", G.REL_UPSTREAM,
                            "dart", confidence=0.85, asof=ASOF)
    merged = G.merge([vc], [dart_edge])
    assert {e["source"] for e in merged} == {"valuechain", "dart"}


def test_corroborated_relation_appears_once_in_lookup():
    """엣지는 출처별로 남지만, 조회 결과가 둘로 나오면 프롬프트에 같은 산업이
    두 줄로 찍혀 서로 다른 관계인 것처럼 읽힌다."""
    edges = [
        G.make_edge("T:003410", "I:시멘트·레미콘", G.REL_MEMBER, "dart",
                    confidence=1.0, asof=ASOF),
        G.make_edge("I:시멘트·레미콘", "I:건설·EPC", G.REL_DOWNSTREAM,
                    "valuechain", confidence=0.5, asof=ASOF),
        G.make_edge("I:시멘트·레미콘", "I:건설·EPC", G.REL_DOWNSTREAM,
                    "dart", confidence=0.85, asof=ASOF),
    ]
    edges.append(G.make_edge("T:003410", "I:시멘트·레미콘", G.REL_MEMBER,
                             "valuechain", confidence=0.5, asof=ASOF))
    assert G.Graph(edges).industry_names("003410") == ["시멘트·레미콘"], \
        "출처가 둘이면 산업명이 중복 표기된다"

    rows = G.Graph(edges).downstream("003410")
    assert len(rows) == 1
    assert set(rows[0]["sources"]) == {"valuechain", "dart"}
    assert rows[0]["confidence"] == pytest.approx(0.85), "가장 높은 신뢰도를 쓴다"

    from src import universe
    uni = universe.from_entries(ASOF, {"003410": {"name": "테스트시멘트",
                                                  "market": "KOSPI", "market_cap": 1}})
    out = G.render_subgraph(G.Graph(edges), "003410", uni)
    assert out.count("건설·EPC") == 1
    assert "valuechain+dart" in out


def test_batch_results_are_keyed_by_custom_id_not_order():
    """Batch API 결과는 순서가 보장되지 않는다. 위치로 대조하면 종목이 뒤섞인다."""
    docs = [_doc(), {**_doc(), "ticker": "000660"}]
    by_ticker = {d["ticker"]: d for d in docs}
    returned = [("000660", {"products": []}), ("003410", {"products": []})]  # 역순
    for custom_id, _ in returned:
        assert by_ticker[custom_id]["ticker"] == custom_id


def test_prompt_includes_vocabulary_and_source():
    vocab = dx.IndustryVocab(known=["건설·EPC", "시멘트·레미콘"])
    prompt = dx.build_prompt(_doc(), vocab)
    assert "시멘트·레미콘" in prompt
    assert "사업보고서" in prompt and "20260315" in prompt
    assert "유연탄" in prompt


def test_schema_is_valid_for_structured_output():
    """구조화 출력은 모든 객체에 additionalProperties:false와 required를 요구한다."""
    def check(node):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(node.get("properties", {}))
            for child in node["properties"].values():
                check(child)
        elif node.get("type") == "array":
            check(node["items"])

    check(dx.EXTRACT_SCHEMA)
    assert json.dumps(dx.EXTRACT_SCHEMA)   # 직렬화 가능해야 한다


class TestRateLimitDiagnostics:
    """429 본문은 {'type':'rate_limit_error','message':'Error'}뿐이다.

    어느 한도인지 모르면 조치가 정반대로 갈린다 — 요청 간격을 늘릴지, 요청
    크기를 줄일지. 실제로 5종목이 전부 429로 죽었을 때 본문만으로는 구분할 수
    없었다. 헤더에서 읽어 로그에 남긴다.
    """

    class _Exc(Exception):
        def __init__(self, headers):
            self.status_code = 429
            self.response = type("R", (), {"headers": headers})()

    def test_reads_which_limit_was_hit(self):
        e = self._Exc({"anthropic-ratelimit-input-tokens-limit": "20000",
                       "anthropic-ratelimit-input-tokens-remaining": "0",
                       "retry-after": "38"})
        detail = dx.rate_limit_detail(e)
        assert "input-tokens=0/20000" in detail
        assert "reset=38" in detail

    def test_missing_headers_say_so_instead_of_pretending(self):
        assert dx.rate_limit_detail(self._Exc({})) == "한도 헤더 없음"

    def test_server_retry_after_beats_our_guess(self):
        e = self._Exc({"retry-after": "45"})
        assert dx._retry_after(e) == 45.0

    def test_no_retry_after_falls_back_to_none(self):
        assert dx._retry_after(self._Exc({})) is None


class TestSectionSlimming:
    """'사업의 내용'은 13만 자에 달한다. 통째로 넣으면 요청 하나가 분당 토큰
    한도를 넘어 재시도로도 통과하지 못한다(실제로 5종목이 전부 그렇게 죽었다).

    앞에서부터 자르면 안 되는 이유가 핵심이다 — '매출 및 수주상황'이 뒤쪽에
    있는데, 그게 전방 관계(누구에게 파는가)의 유일한 근거다.
    """

    def _doc(self):
        return ("II. 사업의 내용\n\n"
                "1. 사업의 개요\n당사는 시멘트를 제조합니다. " + "개요 " * 100 + "\n\n"
                "2. 주요 제품 및 서비스\n시멘트 | 62.0% | 레미콘 | 38.0%\n" + "제품 " * 100 + "\n\n"
                "3. 원재료 및 생산설비\n석회석과 유연탄을 매입합니다. " + "원재료 " * 100 + "\n\n"
                "4. 매출 및 수주상황\n주요 매출처는 건설사와 레미콘 업체입니다. " + "매출 " * 100 + "\n\n"
                "5. 위험관리 및 파생거래\n" + "위험 " * 2000 + "\n\n"
                "7. 연구개발활동\n" + "연구 " * 2000 + "\n\n"
                "8. 기타 참고사항\n" + "기타 " * 2000 + "\n")

    def test_forward_relation_evidence_survives(self):
        """뒤쪽 '매출처'가 살아남지 않으면 이 기능은 의미가 없다."""
        slim, _ = dart.slim_business_section(self._doc())
        assert "주요 매출처는 건설사와 레미콘 업체입니다" in slim

    def test_upstream_and_product_mix_survive(self):
        slim, titles = dart.slim_business_section(self._doc())
        assert "석회석과 유연탄" in slim
        assert "레미콘 | 38.0%" in slim, "매출 비중 표가 날아가면 가중치를 못 준다"
        assert "주요 제품 및 서비스" in titles

    def test_bulk_without_valuechain_information_is_dropped(self):
        slim, _ = dart.slim_business_section(self._doc())
        assert "위험 위험" not in slim
        assert "연구 연구" not in slim
        assert "기타 기타" not in slim

    def test_reduction_is_substantial(self):
        doc = self._doc()
        slim, _ = dart.slim_business_section(doc)
        assert len(slim) < len(doc) * 0.4, f"{len(slim)}/{len(doc)} — 줄지 않았다"

    def test_document_without_subsections_is_left_alone(self):
        """절 구성은 산업마다 다르다. 잘못 걸러서 빈손이 되느니 크게 넣는 게 낫다."""
        plain = "II. 사업의 내용\n" + "본문 " * 500
        slim, titles = dart.slim_business_section(plain)
        assert slim == plain and titles == []

    def test_construction_style_document_without_raw_materials(self):
        """건설사 보고서에는 '원재료' 절이 아예 없다 — 라이브에서 확인한 사실이다."""
        doc = ("II. 사업의 내용\n\n"
               "1. 사업의 개요\n건설업을 영위합니다. " + "개요 " * 100 + "\n\n"
               "2. 주요 제품 및 서비스\n주택 | 55% | 토목 | 45%\n" + "제품 " * 100 + "\n\n"
               "3. 수주상황\n관급 및 민간 발주처로부터 수주합니다. " + "수주 " * 100 + "\n\n"
               "4. 기타 참고사항\n" + "기타 " * 2000 + "\n")
        slim, titles = dart.slim_business_section(doc)
        assert "관급 및 민간 발주처" in slim
        assert "기타 기타" not in slim
        assert titles, "원재료가 없다고 통째로 포기하면 안 된다"


class TestBatchScopeGuard:
    """Batch API는 OAuth 토큰으로 호출할 수 없다(403 permission_error).

    라이브에서 공시 수집에 8분을 쓴 뒤에야 이 403이 났다. 시작 전에 알 수 있는
    실패를 끝까지 끌고 가면 그 시간이 통째로 낭비다.
    """

    def test_oauth_token_with_batch_stops_before_fetching(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        called = []
        monkeypatch.setattr(dx, "collect_documents",
                            lambda *a, **k: called.append(1) or ([], []))
        with pytest.raises(SystemExit) as exc:
            dx.run(["005930"], {}, "20260728", use_batch=True)
        assert "Batch API" in str(exc.value)
        assert "--no-batch" in str(exc.value), "대안이 안내되지 않았다"
        assert not called, "공시를 받기 전에 멈춰야 한다"

    def test_api_key_may_use_batch(self, monkeypatch):
        """API 키는 Batch를 쓸 수 있어야 한다 — 이 가드가 과잉이면 안 된다."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(dx, "collect_documents", lambda *a, **k: ([], []))
        assert dx.run(["005930"], {}, "20260728", use_batch=True)["documents"] == 0

    def test_sequential_mode_is_unaffected_by_the_guard(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        monkeypatch.setattr(dx, "collect_documents", lambda *a, **k: ([], []))
        assert dx.run(["005930"], {}, "20260728", use_batch=False)["documents"] == 0


class TestSubsectionHeaderIsNotATableRow:
    """라이브에서 절 제목으로 '매출액, 영업이익, 매출액, 영업이익'이 잡혔다.

    재무제표 표의 셀이다. 문구만으로 찾으면 표에 걸려 선별이 무력화되고,
    168,442자가 그대로 상한에 걸린다. 제목의 '형태'를 함께 요구해야 한다.
    """

    def _doc(self, body_marker="본문"):
        return ("II. 사업의 내용\n\n"
                "1. 사업의 개요\n" + f"{body_marker} " * 100 + "\n\n"
                "2. 주요 제품 및 서비스\n"
                "| 구분 | 매출액 | 비중 |\n"
                "매출액 | 1,234,567 | 55.0%\n"
                "영업이익 | 98,765 | 4.4%\n" + f"{body_marker} " * 100 + "\n\n"
                "3. 매출 및 수주상황\n주요 매출처는 건설사입니다.\n"
                + f"{body_marker} " * 100 + "\n\n"
                "4. 기타 참고사항\n매출액\n영업이익\n" + "기타 " * 2000 + "\n")

    def test_table_cells_are_not_treated_as_headers(self):
        _, titles = dart.slim_business_section(self._doc())
        assert "매출액" not in titles and "영업이익" not in titles, titles

    def test_numbered_headers_are_found(self):
        _, titles = dart.slim_business_section(self._doc())
        assert "사업의 개요" in titles
        assert "매출 및 수주상황" in titles

    def test_bulk_section_is_actually_removed(self):
        """이게 실패하면 선별이 이름만 선별이다."""
        doc = self._doc()
        slim, _ = dart.slim_business_section(doc)
        assert "기타 기타" not in slim
        assert len(slim) < len(doc) * 0.5, f"{len(slim)}/{len(doc)}"

    def test_korean_letter_enumerators_work(self):
        """회사에 따라 '가. 나. 다.'를 쓴다."""
        doc = ("II. 사업의 내용\n\n"
               "가. 사업의 개요\n" + "개요 " * 300 + "\n\n"
               "나. 원재료 및 생산설비\n석회석을 매입합니다.\n" + "원재료 " * 300 + "\n\n"
               "다. 기타 참고사항\n" + "기타 " * 2000 + "\n")
        slim, titles = dart.slim_business_section(doc)
        assert "원재료 및 생산설비" in titles
        assert "석회석을 매입합니다" in slim
        assert "기타 기타" not in slim


class TestSessionExtractionPath:
    """API 키 없이 구독만 있는 환경에서는 Messages API가 막힌다(라이브에서 429 확인).

    그래도 추출은 가능하다 — 세션 안의 Claude가 같은 문서를 읽고 같은 스키마로
    추출하면 된다. 중요한 것은 **인용 검증이 그대로 돈다**는 점이다. 추출자가
    API든 사람이든 지어낸 인용은 똑같이 폐기돼야 한다.
    """

    SECTION = ("II. 사업의 내용\n\n"
               "2. 주요 제품 및 서비스\n시멘트 | 62.0% | 레미콘 | 38.0%\n\n"
               "4. 매출 및 수주상황\n주요 매출처는 국내 건설사입니다.\n")

    @pytest.fixture
    def stored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dx, "SECTIONS_DIR", tmp_path)
        (tmp_path / "300720.json").write_text(json.dumps({
            "ticker": "300720", "corp_name": "한일시멘트", "section": self.SECTION,
            "report_nm": "사업보고서", "rcept_dt": "20260318",
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(dx, "DATA_DIR", tmp_path)
        monkeypatch.setattr(dx.G, "GRAPH_PATH", tmp_path / "edges.jsonl", raising=False)
        return tmp_path

    def _extraction(self, **over):
        base = {
            "products": [{"industry": "시멘트·레미콘", "product": "시멘트",
                          "revenue_share": 0.62, "tier": "A",
                          "quote": "시멘트 | 62.0% | 레미콘 | 38.0%"}],
            "upstream": [],
            "downstream": [{"industry": "건설·EPC", "customer": "건설사", "tier": "B",
                            "quote": "주요 매출처는 국내 건설사입니다."}],
            "unmapped": "",
        }
        base.update(over)
        return {"300720": base}

    def test_fabricated_quote_is_dropped(self, stored, monkeypatch):
        """세션이 추출했다고 검증이 느슨해지면 안 된다."""
        monkeypatch.setattr(dx.G, "save", lambda *a, **k: None)
        ex = self._extraction(upstream=[{
            "industry": "전력", "material": "전기", "cost_share": None, "tier": "B",
            "quote": "당사는 한국전력으로부터 전력을 공급받습니다.",  # 문서에 없다
        }])
        report = dx.run([], {"model": "x"}, "20260728", extractions=ex)
        assert report["quote_dropped"] == 1
        assert report["quote_kept"] == 2

    def test_real_quotes_become_edges(self, stored, monkeypatch):
        monkeypatch.setattr(dx.G, "save", lambda *a, **k: None)
        report = dx.run([], {"model": "x"}, "20260728", extractions=self._extraction())
        assert report["quote_dropped"] == 0
        assert report["edges"] > 0

    def test_missing_section_refuses_to_verify(self, stored, monkeypatch):
        """원문 없이 검증했다고 하면 그게 최악이다."""
        monkeypatch.setattr(dx.G, "save", lambda *a, **k: None)
        with pytest.raises(SystemExit, match="저장된 절이 없는"):
            dx.run([], {"model": "x"}, "20260728",
                   extractions={"999999": self._extraction()["300720"]})

    def test_no_llm_credentials_needed(self, stored, monkeypatch):
        """이 경로의 존재 이유 — 자격 증명이 없어도 돌아야 한다."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setattr(dx.G, "save", lambda *a, **k: None)
        report = dx.run([], {"model": "x"}, "20260728", extractions=self._extraction())
        assert report["extracted"] == 1


class TestIndustryVocabulary:
    """어휘 통제가 조용히 꺼지면 '시멘트'와 '시멘트 제조업'이 각각 노드가 된다."""

    def test_vocabulary_survives_an_empty_edge_file(self):
        """graph/edges.jsonl은 파이프라인 산출물이라 새 클론에서는 없다."""
        names = dx.known_industries([])
        assert len(names) > 20, f"{len(names)}개 — YAML을 못 읽고 있다"
        assert "시멘트·레미콘" in names and "조선" in names

    def test_edge_file_industries_are_merged_in(self):
        edge = dx.G.make_edge(dx.G.industry_node("우주항공"),
                              dx.G.industry_node("탄소복합재"),
                              dx.G.REL_UPSTREAM, "dart", asof="20260728")
        names = dx.known_industries([edge])
        assert "탄소복합재" in names
        assert "조선" in names, "YAML 어휘가 사라졌다"
