"""공급계약 공시 파싱 — 152종목 제한을 푸는 경로.

사업보고서에서 거래처를 뽑는 방식은 회사가 이름을 써 줬을 때만 통한다.
엘앤에프('기술·정보유출 우려'), 주성엔지니어링('고객 투자정보 노출'),
알에스오토메이션('R사·L사')처럼 익명 처리하면 전방이 통째로 빈다.
공급계약 공시는 계약 상대를 밝히는 것이 목적이라 익명화가 불가능하다.
"""

import pytest

from src import dart_contracts as dc, graph as G, universe as U

UNI = U.Universe("20260803", {t: {"name": n, "market_cap": 1} for t, n in {
    "033500": "동성화인텍", "010140": "삼성중공업", "329180": "HD현대중공업",
}.items()})

# 실제 서식을 평문으로 편 모양. 표가 XML에서 풀리면 항목명과 값이 이렇게 붙는다.
DOC = """
단일판매ㆍ공급계약 체결
1. 판매ㆍ공급계약 내용 LNG운반선용 초저온 보냉자재
2. 계약내역 계약금액(원) 197,970,240,000
최근매출액(원) 412,000,000,000
매출액대비(%) 48.05
3. 계약상대 HD현대중공업
4. 판매ㆍ공급지역 대한민국
5. 계약기간 시작일 2022-11-01
종료일 2026-07-29
6. 주요 계약조건 -
"""


class TestParsing:
    def test_counterparty_amount_and_period(self):
        p = dc.parse_contract(DOC)
        assert p["counterparty"] == "HD현대중공업"
        assert p["amount"] == pytest.approx(197_970_240_000)
        assert p["begin"] == "20221101"
        assert p["end"] == "20260729"

    def test_sales_ratio_comes_from_the_form(self):
        """계약금액만으로는 크기를 모른다. 매출액 대비가 파급 크기의 척도다."""
        assert dc.parse_contract(DOC)["sales_ratio"] == pytest.approx(0.4805)

    def test_product_is_captured(self):
        assert "보냉자재" in dc.parse_contract(DOC)["product"]

    def test_field_value_does_not_swallow_the_next_field(self):
        """항목명 뒤를 넓게 잡으면 다음 항목 값을 끌어온다."""
        p = dc.parse_contract(DOC)
        assert "판매" not in p["counterparty"]
        assert "계약상대" not in p["product"]

    def test_period_on_one_line_still_yields_both_dates(self):
        one = "계약기간 2022-11-01 ~ 2026-07-29\n계약상대 삼성중공업"
        p = dc.parse_contract(one)
        assert (p["begin"], p["end"]) == ("20221101", "20260729")

    def test_missing_fields_do_not_raise(self):
        p = dc.parse_contract("단일판매ㆍ공급계약 체결")
        assert p["counterparty"] == "" and p["amount"] is None


class TestEdges:
    def _one(self, doc=DOC, stock="033500"):
        return dc.to_edges([{"stock_code": stock, "corp_name": "동성화인텍",
                             "report_nm": "단일판매ㆍ공급계약 체결",
                             "rcept_dt": "20221101",
                             "parsed": dc.parse_contract(doc)}], UNI, "20260803")

    def test_direction_is_seller_to_buyer(self):
        """공시를 낸 회사가 파는 쪽이다. 사업보고서 경로와 같은 규약이라야 겹쳐 본다."""
        edges, _ = self._one()
        assert (edges[0]["src"], edges[0]["dst"]) == (
            G.ticker_node("033500"), G.ticker_node("329180"))

    def test_ratio_becomes_edge_weight(self):
        edges, _ = self._one()
        assert edges[0]["weight"] == pytest.approx(0.4805)

    def test_ratio_is_derived_when_the_form_omits_it(self):
        doc = DOC.replace("매출액대비(%) 48.05", "")
        edges, _ = self._one(doc)
        assert edges[0]["weight"] == pytest.approx(197_970_240_000 / 412_000_000_000, rel=1e-3)

    def test_contract_evidence_carries_amount_and_period(self):
        """근거에 금액·기간이 없으면 왜 이 관계가 중요한지 화면에서 알 수 없다."""
        edges, _ = self._one()
        ev = edges[0]["evidence"]
        assert "20221101~20260729" in ev and "197,970,240,000" in ev

    def test_unlisted_counterparty_is_counted_not_dropped(self):
        doc = DOC.replace("HD현대중공업", "Shell International")
        edges, unlisted = self._one(doc)
        assert edges == []
        assert "Shell International" in unlisted

    def test_unlisted_filer_is_skipped(self):
        """비상장 공시자는 파급 대상이 아니다."""
        edges, _ = self._one(stock="")
        assert edges == []

    def test_confidence_beats_the_narrative_path(self):
        """정해진 항목에서 온 값이라 서술형 문장에서 뽑은 것보다 신뢰도가 높다."""
        from src import company_chain
        edges, _ = self._one()
        assert edges[0]["confidence"] > 0.8
