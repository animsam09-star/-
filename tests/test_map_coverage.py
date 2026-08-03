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
