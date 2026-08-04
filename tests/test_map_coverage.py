"""맵의 대상 선정은 맵이 **쓰이는 표본**과 같아야 한다.

20260731 실행에서 후보 20종목 중 19종목이 `소속 산업: 미분류`였다. 맵은 시총
상위 100종목으로 채워 왔는데 스크리너에 걸리는 건 급등한 중소형주라, 맵을 만든
표본과 맵이 쓰이는 표본이 서로 달랐다. 맵 소속 종목은 152개로 전체 2,763종목의
5.5%였다.

최종 목적이 '밸류체인 안에서 수혜주 찾기'인데 오르는 종목이 맵 밖에 있으면
맵은 그 판단에 참여하지 못한다. 맵이 예쁜 것과 값을 하는 것은 다르다.
"""

import json

import pytest

from src import dart_extract
from src import graph as G


@pytest.fixture
def universe_file(tmp_path, monkeypatch):
    """스크리닝 유니버스 4종목. 시총 순으로 저장돼 있다."""
    monkeypatch.setattr(dart_extract, "DATA_DIR", tmp_path)
    # ROOT도 함께 옮긴다 — 안 옮기면 저장소의 실제 cases/*.json을 읽어
    # 과거 후보 우선순위가 끼어들고, 테스트가 저장소 상태에 따라 흔들린다.
    monkeypatch.setattr(dart_extract, "ROOT", tmp_path)
    (tmp_path / "screen_universe_20260731.json").write_text(json.dumps({
        "base_date": "20260731",
        "tickers": [
            {"ticker": "005930", "name": "삼성전자", "market_cap": 900, "avg_turnover": 9},
            {"ticker": "058610", "name": "에스피지", "market_cap": 300, "avg_turnover": 5},
            {"ticker": "119850", "name": "지엔씨에너지", "market_cap": 200, "avg_turnover": 4},
            {"ticker": "484810", "name": "티엑스알로보틱스", "market_cap": 100, "avg_turnover": 3},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _mapped(monkeypatch, tickers):
    edges = [G.make_edge(G.ticker_node(t), G.industry_node("산업자동화"),
                         G.REL_MEMBER, "dart", origin=t, asof="20260731")
             for t in tickers]
    monkeypatch.setattr(dart_extract.graph_build.G, "load", lambda *a, **k: edges)


def test_already_mapped_tickers_are_skipped(universe_file, monkeypatch, capsys):
    """이미 소속이 붙은 종목을 다시 뽑으면 예산만 쓰고 커버리지는 그대로다."""
    _mapped(monkeypatch, ["005930", "058610"])
    assert dart_extract.screening_tickers(10) == ["119850", "484810"]


def test_ranked_by_market_cap_within_the_universe(universe_file, monkeypatch):
    """같은 산업이면 큰 회사의 보고서가 산업 구조를 더 명확히 적는다."""
    _mapped(monkeypatch, [])
    assert dart_extract.screening_tickers(2) == ["005930", "058610"]


def test_target_is_the_screening_universe_not_the_whole_market(universe_file, monkeypatch):
    """전 종목이 아니라 '후보가 될 수 있는' 종목만 대상이다.

    시총 상위를 그대로 쓰면 유동성이 없어 스크리너에 절대 안 걸리는 대형주까지
    들어가고, 정작 걸리는 중소형주는 계속 밖에 남는다.
    """
    _mapped(monkeypatch, [])
    got = dart_extract.screening_tickers(99)
    assert got == ["005930", "058610", "119850", "484810"]
    assert len(got) == 4, "유니버스 밖 종목이 섞였다"


def test_progress_is_reported(universe_file, monkeypatch, capsys):
    """몇 개가 남았는지 안 찍으면 커버리지가 느는지 알 수 없다."""
    _mapped(monkeypatch, ["005930"])
    dart_extract.screening_tickers(10)
    out = capsys.readouterr().out
    assert "4종목" in out and "3종목" in out


def test_missing_universe_file_says_what_to_run(tmp_path, monkeypatch):
    """조용히 시총 상위로 되돌아가면 고친 게 무효가 되고 아무도 모른다."""
    monkeypatch.setattr(dart_extract, "DATA_DIR", tmp_path)
    with pytest.raises(SystemExit, match="daily-screen"):
        dart_extract.screening_tickers(10)


def test_mapped_tickers_reads_membership_in_both_directions(monkeypatch):
    """소속 엣지는 종목→산업으로 저장되지만 방향에 기대지 않는다."""
    monkeypatch.setattr(dart_extract.graph_build.G, "load", lambda *a, **k: [
        G.make_edge(G.ticker_node("005930"), G.industry_node("반도체"),
                    G.REL_MEMBER, "dart", origin="005930", asof="20260731"),
        G.make_edge(G.industry_node("반도체"), G.industry_node("서버·전자기기"),
                    G.REL_DOWNSTREAM, "dart", origin="005930", asof="20260731"),
    ])
    assert dart_extract.mapped_tickers() == {"005930"}, "산업 노드가 종목으로 섞였다"


class TestBudgetIsNotSpentOnStocksWithNoValuechain:
    """대상의 앞자리를 밸류체인이 없는 종목이 차지하고 있었다.

    유니버스를 시총 순으로 세우니 상위가 삼성전자우·SK스퀘어·KB금융·삼성생명…
    이었다. 우선주는 본주와 같은 회사고, 은행·보험·증권은 원재료를 사서 전방에
    파는 구조가 아니라 '후방/전방 산업'이라는 질문 자체가 성립하지 않는다.
    사업보고서를 넣어도 나올 게 없는데 추출 예산은 똑같이 든다.

    759종목 중 우선주 19 + 금융·리츠 등 29 = 48종목이고, 전부 상위에 몰려 있다.
    """

    def _universe(self, tmp_path, monkeypatch, rows):
        monkeypatch.setattr(dart_extract, "DATA_DIR", tmp_path)
        monkeypatch.setattr(dart_extract, "ROOT", tmp_path)
        (tmp_path / "screen_universe_20260731.json").write_text(
            json.dumps({"base_date": "20260731", "tickers": rows},
                       ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(dart_extract.graph_build.G, "load", lambda *a, **k: [])
        return tmp_path

    ROWS = [
        {"ticker": "005935", "name": "삼성전자우", "market_cap": 900, "avg_turnover": 9},
        {"ticker": "105560", "name": "KB금융", "market_cap": 800, "avg_turnover": 8},
        {"ticker": "032830", "name": "삼성생명", "market_cap": 700, "avg_turnover": 7},
        {"ticker": "00680K", "name": "미래에셋증권2우B", "market_cap": 650, "avg_turnover": 6},
        {"ticker": "417310", "name": "코람코더원리츠", "market_cap": 600, "avg_turnover": 5},
        {"ticker": "058610", "name": "에스피지", "market_cap": 300, "avg_turnover": 4},
        {"ticker": "119850", "name": "지엔씨에너지", "market_cap": 200, "avg_turnover": 3},
    ]

    def test_preferred_and_financials_are_dropped(self, tmp_path, monkeypatch):
        self._universe(tmp_path, monkeypatch, self.ROWS)
        assert dart_extract.screening_tickers(10) == ["058610", "119850"]

    def test_letter_suffixed_preferred_is_caught(self, tmp_path, monkeypatch):
        """00680K처럼 끝자리가 문자인 우선주가 있어 티커 규칙은 샌다."""
        assert dart_extract._PREFERRED_NAME.search("미래에셋증권2우B")
        assert dart_extract._PREFERRED_NAME.search("한화3우B(전환)")
        assert not dart_extract._PREFERRED_NAME.search("영원무역")

    def test_exclusions_are_reported_not_silent(self, tmp_path, monkeypatch, capsys):
        """조용히 빼면 '왜 이 종목이 안 뽑혔나'를 나중에 알 수 없다."""
        self._universe(tmp_path, monkeypatch, self.ROWS)
        dart_extract.screening_tickers(10)
        out = capsys.readouterr().out
        assert "우선주 2종목" in out and "3종목" in out

    def test_past_candidates_come_first(self, tmp_path, monkeypatch):
        """'후보가 됐다'가 '후보가 될 수 있다'보다 강한 증거다."""
        d = self._universe(tmp_path, monkeypatch, self.ROWS)
        (d / "candidates_20260731.json").write_text(json.dumps(
            {"candidates": [{"ticker": "119850", "name": "지엔씨에너지"}]}),
            encoding="utf-8")
        got = dart_extract.screening_tickers(10)
        assert got[0] == "119850", "시총이 작아도 실제 후보였던 종목이 앞이다"


class TestIndustryVocabularyHasNoTickers:
    """어휘 통제는 산업명만 다뤄야 한다.

    known_industries가 관계 종류만 보고 dst를 가져와서, 회사 간 거래 엣지
    (T:종목 -후방-> T:종목)의 dst인 티커가 산업명으로 섞였다. 어휘 133개 중
    68개가 '005930', '000660' 같은 티커였다 — 추출 프롬프트에 "기존 산업
    목록"이라며 티커를 나열해 준 셈이고, 그러면 어휘 통제가 절반만 작동한다.
    회사 간 거래를 그래프에 넣으면서 생긴 구멍이다.
    """

    def test_company_trade_edges_do_not_leak_tickers(self):
        edges = [
            G.make_edge(G.industry_node("조선"), G.industry_node("철강"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260731"),
            G.make_edge(G.ticker_node("009540"), G.ticker_node("005490"),
                        G.REL_UPSTREAM, "dart", origin="009540", asof="20260731"),
            G.make_edge(G.ticker_node("005490"), G.industry_node("철강"),
                        G.REL_MEMBER, "dart", origin="005490", asof="20260731"),
        ]
        got = dart_extract.known_industries(edges)
        assert "철강" in got and "조선" in got
        assert not any(v.isdigit() for v in got), f"티커가 섞였다: {got}"


class TestDigestFindsProductsWhereverTheyAre:
    """다이제스트가 비면 '자료가 없다'로 읽히지만, 대개는 '못 찾은 것'이다.

    99종목을 훑는 동안 두 건을 건너뛸 뻔했다.

      티엑스알로보틱스 — '사업의 개요'가 [주요 용어 해설] PLC·HMI·센서 설명으로
                        시작한다. 앞에서 900자를 자르니 용어집만 남고 정작
                        '물류자동화 및 로봇자동화 솔루션 전문기업'은 잘렸다.
      바이오비쥬       — 남은 절이 통째로 '주요 제품 등의 가격 변동 추이'였다.
                        DROP 정규식이 '가격변동추이'만 막고 있어 띄어쓴 제목이
                        빠져나갔고, 다이제스트에 숫자만 남았다.

    둘 다 원문에는 제품이 또렷이 적혀 있었다. 요약이 비었다고 자료가 없는 게 아니다.
    """

    def test_spaced_price_table_heading_is_dropped(self):
        assert dart_extract._MEMBERSHIP_DROP.search("나. 주요 제품 등의 가격 변동 추이")
        assert dart_extract._MEMBERSHIP_DROP.search("가격변동추이")

    def test_glossary_heading_is_dropped(self):
        assert dart_extract._MEMBERSHIP_DROP.search("[주요 용어 해설]")
        assert not dart_extract._MEMBERSHIP_DROP.search("주요 제품 및 서비스")

    def test_window_is_chosen_by_content_not_position(self):
        """배경·정의를 앞에 길게 깔고 본론을 뒤에 두는 보고서가 흔하다."""
        glossary = "PLC 논리연산 순서조작 타이머 카운터 정의 " * 30
        meat = "당사는 물류자동화 솔루션 전문기업으로 휠소터를 제조 판매 공급하고 있습니다 "
        got = dart_extract._best_window(glossary + meat, 300)
        assert "휠소터" in got, "앞에서 잘라 본론을 놓쳤다"

    def test_short_text_is_returned_whole(self):
        assert dart_extract._best_window("당사는 타이어를 제조합니다", 300) == \
            "당사는 타이어를 제조합니다"

    def test_window_never_returns_empty(self):
        """단서가 하나도 없어도 뭔가는 돌려줘야 한다 — 빈 값은 '자료 없음'으로 읽힌다."""
        assert dart_extract._best_window("가나다라마바사" * 100, 50)


class TestRelationsNeedAnAnchorIndustry:
    """관계는 '어느 산업에서 출발하는가'가 있어야 만들 수 있다.

    출발 산업은 products(소속)에서 나온다. 소속과 관계를 다른 파일로 나눴더니
    관계 파일의 products가 비어, 인용 검증을 24건 통과하고도 엣지가 0개로
    나왔다. 아무 말도 없어서 원인을 찾는 데 한 사이클이 들었다.
    """

    def _vocab(self):
        return dart_extract.IndustryVocab(["타이어", "완성차"])

    def test_relations_without_products_produce_nothing(self, capsys):
        got = dart_extract.to_edges("073240", {
            "products": [],
            "upstream": [], "unmapped": "",
            "downstream": [{"industry": "완성차", "quote": "완성차업체에 공급",
                            "tier": "B", "customer": "완성차", "segment": "타이어"}],
        }, self._vocab(), "20260731")
        assert got == []

    def test_it_says_why_instead_of_failing_silently(self, capsys):
        dart_extract.to_edges("073240", {
            "products": [],
            "upstream": [], "unmapped": "",
            "downstream": [{"industry": "완성차", "quote": "완성차업체에 공급",
                            "tier": "B", "customer": "완성차", "segment": "타이어"}],
        }, self._vocab(), "20260731")
        out = capsys.readouterr().out
        assert "products" in out and "073240" in out

    def test_no_noise_when_there_is_nothing_to_relate(self, capsys):
        """관계도 없고 소속도 없으면 할 말이 없다 — 경고는 신호일 때만 값이 있다."""
        dart_extract.to_edges("073240", {"products": [], "upstream": [],
                                         "downstream": [], "unmapped": ""},
                              self._vocab(), "20260731")
        assert capsys.readouterr().out == ""

    def test_with_products_the_relation_is_built(self):
        got = dart_extract.to_edges("073240", {
            "products": [{"industry": "타이어", "quote": "타이어를 제조합니다",
                          "tier": "B", "product": "타이어", "revenue_share": None}],
            "upstream": [], "unmapped": "",
            "downstream": [{"industry": "완성차", "quote": "완성차업체에 공급",
                            "tier": "B", "customer": "완성차", "segment": "타이어"}],
        }, self._vocab(), "20260731")
        rels = {(e["src"], e["dst"], e["rel"]) for e in got}
        assert ("I:타이어", "I:완성차", G.REL_DOWNSTREAM) in rels
        assert ("I:완성차", "I:타이어", G.REL_UPSTREAM) in rels, "양방향이어야 한다"
