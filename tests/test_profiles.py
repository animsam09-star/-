"""종목 프로필 — 공시를 못 받은 종목의 품목·거래처를 메우는 표.

소속 종목 152개 중 78개만 사업보고서를 받아 뒀다. 나머지는 손으로 쓴 맵에서
이름만 왔고 무엇을 만드는지가 없어서, '전공정 장비' 한 상자에 원익IPS·유진테크·
주성엔지니어링·피에스케이가 서로 대체재처럼 들어가 있었다.
"""

import textwrap

import pytest

from src import graph as G, graph_build as gb, universe as U

UNI = U.Universe("20260728", {t: {"name": n, "market_cap": 1} for t, n in {
    "036930": "주성엔지니어링", "240810": "원익IPS", "000660": "SK하이닉스",
    "005930": "삼성전자",
}.items()})


@pytest.fixture
def profiles(tmp_path):
    p = tmp_path / "_company_profiles.yaml"
    p.write_text(textwrap.dedent("""
        036930:
          name: 주성엔지니어링
          products: [ALD 장비]
          customers: [SK하이닉스]
          source: http://example.com/a
        240810:
          name: 원익IPS
          products: [PECVD 장비]
          customers: [삼성전자, 없는회사]
          source: http://example.com/b
        999999:
          name: 상장폐지된회사
          products: [무언가]
          source: http://example.com/c
    """), encoding="utf-8")
    return p


def test_customers_become_company_trades(profiles):
    edges, _ = gb.from_profiles(UNI, "20260728", profiles)
    pairs = {(e["src"], e["dst"]) for e in edges}
    assert (G.ticker_node("036930"), G.ticker_node("000660")) in pairs
    assert (G.ticker_node("240810"), G.ticker_node("005930")) in pairs


def test_profile_confidence_is_below_disclosure(profiles):
    """기사·IR 자료는 1차 자료가 아니다. 공시와 같은 무게로 두면 안 된다."""
    edges, _ = gb.from_profiles(UNI, "20260728", profiles)
    assert all(e["confidence"] == gb.PROFILE_CONFIDENCE for e in edges)
    assert gb.PROFILE_CONFIDENCE < 0.8
    assert gb.PROFILE_CONFIDENCE > gb.VALUECHAIN_CONFIDENCE


def test_unlisted_names_are_reported_not_silently_dropped(profiles):
    _, report = gb.from_profiles(UNI, "20260728", profiles)
    assert any("없는회사" in u for u in report["unresolved"])
    assert any("999999" in u for u in report["unresolved"])


def test_profiles_do_not_create_membership(profiles):
    """소속은 밸류체인 맵이 정한다. 여기서 또 정하면 두 경로가 충돌한다."""
    edges, _ = gb.from_profiles(UNI, "20260728", profiles)
    assert not any(e["rel"] == G.REL_MEMBER for e in edges)


def test_products_are_available_for_backfill(profiles):
    products = gb.profile_products(profiles)
    assert products["036930"] == "ALD 장비"
    assert products["240810"] == "PECVD 장비"


def test_missing_file_is_not_an_error(tmp_path):
    edges, report = gb.from_profiles(UNI, "20260728", tmp_path / "nope.yaml")
    assert edges == [] and report["profiles"] == 0


def test_shipped_profiles_all_resolve():
    """저장소에 든 프로필이 실제로 붙는지. 티커를 틀리면 조용히 빠진다."""
    products = gb.profile_products()
    assert products, "프로필 파일이 비었다"
    assert all(len(t) == 6 and t.isdigit() for t in products)
