"""종목명이 흔한 낱말이면 무관한 기사가 그대로 딸려 온다.

실측(20260731 후보 20종목):
  오로라(039830, 완구)   → 8건 중 7건이 오로라농장·오로라 골프&리조트·걸그룹 오로라
  한국공항(005430, 조업) → 8건 전부 '한국공항**공사**'
  CJ CGV                 → 8건 전부 시세와 무관한 마케팅 기사

이 잡음이 원인 분석에 그대로 들어가면 분석이 KLPGA 대회를 상승 원인으로 읽는다.
조용히 섞이는 게 최악이라 이름 경계와 시장 키워드 **둘 다** 통과한 것만 남긴다.
한쪽만으로는 못 막는다는 게 이 파일의 핵심 주장이다.
"""

from datetime import datetime, timedelta

import pytest

from src import collect


def _item(title, desc=""):
    return {"title": title, "description": desc}


# ---- 이름 경계 --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "한국공항, 2분기 영업이익 흑자 전환",
    "한국공항의 지상조업 단가가 올랐다",
    "한국공항이 신규 계약을 따냈다",
    "지상조업사 한국공항",                 # 문장 끝
    "한국공항(005430) 주가 급등",          # 괄호
])
def test_real_mentions_pass(text):
    assert collect.mentions_company("한국공항", text)


@pytest.mark.parametrize("text", [
    "한국공항공사·경찰·항공사 등 30여 명 참여",
    "인천·한국공항공사와 항공안전데이터 협약을 맺었다",
])
def test_longer_proper_noun_is_not_the_company(text):
    """'한국공항공사'는 '한국공항'이 아니다. 상장사도 다르다."""
    assert not collect.mentions_company("한국공항", text)


def test_farm_and_resort_are_not_the_toymaker():
    assert not collect.mentions_company("오로라", "홍성군의 '오로라농장'을 찾아")
    assert not collect.mentions_company("오로라", "오로라월드 챔피언십 최종 라운드")


# ---- 두 관문이 각각 필요하다 -------------------------------------------------

def test_name_boundary_alone_misses_spaced_collisions():
    """'오로라 골프&리조트'는 띄어쓰기라 이름 경계로는 못 걸러진다."""
    text = "강원 원주의 오로라 골프&리조트에서 열린 KLPGA 투어 최종 라운드"
    assert collect.mentions_company("오로라", text)          # 경계는 통과하지만
    assert not collect.relevant("오로라", _item(text))       # 시장 키워드가 없다


def test_market_keyword_alone_misses_name_collisions():
    """'한국공항공사 협약'은 시장 키워드(계약/협약)가 있어도 다른 회사다."""
    text = "TS, 인천·한국공항공사와 항공안전데이터 공유 계약 체결"
    assert collect._MARKET_KW.search(text)                   # 키워드는 통과하지만
    assert not collect.relevant("한국공항", _item(text))     # 이름이 다르다


def test_genuine_news_passes_both():
    assert collect.relevant("금호타이어", _item(
        "금호타이어, 2분기 영업이익 1200억…북미 판가 인상 효과"))


def test_promotional_company_news_is_dropped():
    """회사 이야기라도 시세와 무관한 홍보성은 원인이 될 수 없다."""
    assert not collect.relevant("CJ CGV", _item(
        "CGV, 영화 원작 읽는 '북클럽 with 오디세이아' 운영",
        "CJ CGV 관계자는 관객 수요가 증가하고 있다고 말했다"))


# ---- 수집 경로 --------------------------------------------------------------

def _fake_resp(items):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"items": items}
    return Resp()


def _naver(title, desc=""):
    pub = (datetime.now().astimezone()).strftime("%a, %d %b %Y %H:%M:%S %z")
    return {"title": title, "description": desc, "pubDate": pub,
            "originallink": f"http://x/{abs(hash(title))}"}


def test_fetch_news_filters_and_reports(monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: _fake_resp([
        _naver("오로라월드 챔피언십 최종 라운드"),
        _naver("오로라농장 거봉 수확 현장"),
        _naver("오로라, 완구 수출 증가로 2분기 매출 성장"),
    ]))
    stats = {}
    got = collect.fetch_news("오로라", {"news_days": 7, "news_per_stock": 8}, stats)
    assert [g["title"] for g in got] == ["오로라, 완구 수출 증가로 2분기 매출 성장"]
    assert stats == {"raw": 3, "kept": 1}


def test_all_noise_is_named_not_silently_empty(monkeypatch):
    """전부 걸러진 종목은 '기사가 없다'가 아니라 '전부 동명이인이었다'는 뜻이다."""
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: _fake_resp([
        _naver("한국공항공사 청렴 캠페인 개최"),
    ]))
    stats = {}
    assert collect.fetch_news("한국공항", {"news_days": 7, "news_per_stock": 8},
                              stats) == []
    assert stats["all_noise"] == ["한국공항"]


def test_old_articles_are_not_counted_as_noise(monkeypatch):
    """기간 밖 기사는 걸러낸 게 아니라 애초에 대상이 아니다."""
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")
    old = (datetime.now().astimezone() - timedelta(days=90))
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: _fake_resp([
        {"title": "한국공항, 영업이익 흑자", "description": "",
         "pubDate": old.strftime("%a, %d %b %Y %H:%M:%S %z"), "link": "http://x"},
    ]))
    stats = {}
    collect.fetch_news("한국공항", {"news_days": 7, "news_per_stock": 8}, stats)
    assert stats == {"raw": 0, "kept": 0}
