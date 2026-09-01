"""정성 데이터 수집: 후보 종목별 네이버 뉴스와 DART 공시를 모은다.

API 키가 없으면 해당 소스는 건너뛰고 빈 목록을 반환한다(파이프라인은 계속 진행).
- 네이버 뉴스: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
- DART 공시:  DART_API_KEY
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .dart import load_corp_codes

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    return _TAG_RE.sub("", text).replace("&quot;", '"').replace("&amp;", "&").strip()


# ---- 뉴스 걸러내기 ---------------------------------------------------------
#
# 네이버 뉴스 검색은 종목명을 **문자열로** 찾는다. 종목명이 흔한 낱말이면
# 상장사와 무관한 기사가 그대로 딸려 온다. 실측한 예:
#
#   오로라(039830, 완구)   → 오로라농장 거봉 / 오로라 골프&리조트 KLPGA /
#                            걸그룹 오로라 / 르노 '오로라 프로젝트'  (8건 중 7건 무관)
#   한국공항(005430, 조업) → 전부 '한국공항**공사**' 기사       (8건 중 8건 무관)
#
# 이 잡음을 그대로 원인 분석에 넣으면 분석이 골프 대회를 상승 원인으로 읽는다.
# 조용히 섞이는 게 최악이라 두 관문을 **둘 다** 통과한 것만 남긴다. 한쪽만으로는
# 못 막는다 — 이름 경계는 '오로라 골프'(띄어쓰기)를 못 잡고, 시장 키워드는
# '한국공항공사 협약'을 못 잡는다.

# 뒤에 이 조사가 붙으면 이름이 거기서 끝난 것이다. 그 외 한글이 이어지면
# 더 긴 고유명사의 앞부분이다(한국공항공사, 오로라농장, 오로라월드).
_PARTICLES = ("은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도",
              "로", "으", "만", "부터", "까지", "에서", "보다", "라", "랑",
              "께", "한테", "처럼", "마다", "조차", "밖에", "이라", "이나", "나")

# 주가를 움직일 만한 사건인가. 없으면 회사 이야기라도 시세와 무관한 홍보성이다.
_MARKET_KW = re.compile(
    r"주가|급등|급락|상한가|하한가|신고가|목표주가|증권|애널리스트|투자의견|"
    r"코스닥|코스피|상장|공시|거래량|시가총액|배당|자사주|유상증자|무상증자|"
    r"수주|계약|납품|공급|실적|매출|영업이익|적자|흑자|증설|가동|양산|수출|"
    r"인수|합병|지분|매각|신제품|출시|임상|허가|승인|특허|리콜|파업|화재|"
    r"목표가|어닝|가이던스|단가|판가"
    # '수요'는 넣지 않는다 — '관객 수요가 증가'처럼 일반 서술에 흔해서
    # 시세와 무관한 홍보 기사를 그대로 통과시킨다(CJ CGV 실측).
)


def mentions_company(name: str, text: str) -> bool:
    """종목명이 **그 회사를 가리키며** 나오는가.

    이름 바로 뒤에 조사가 아닌 한글이 붙으면 다른 고유명사다. '한국공항공사'는
    '한국공항'이 아니고, '오로라농장'은 '오로라'가 아니다.
    """
    if not name:
        return False
    for m in re.finditer(re.escape(name), text):
        nxt = text[m.end():m.end() + 3]
        if not nxt or not re.match(r"[가-힣]", nxt):
            return True
        if nxt.startswith(_PARTICLES):
            return True
    return False


def relevant(name: str, item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('description', '')}"
    return mentions_company(name, text) and bool(_MARKET_KW.search(text))


_NEWS_NOTICE_SHOWN = False


def _news_notice(msg: str) -> None:
    """뉴스 수집 상태를 **한 번만** 알린다.

    조용히 빈 목록을 돌려주면 '키가 없다'와 '뉴스가 없다'가 구분되지 않는다.
    실제로 원인 분석 후보 20건이 전부 '뉴스 없음'이었는데, 그게 키 미등록
    때문인지 정말 기사가 없어서인지 로그만 봐서는 알 수 없었다. 원인 분석이
    가장 크게 기대는 입력이라 그 구분이 안 되면 진단이 막힌다.

    종목마다 찍으면 30줄이 되므로 첫 한 번만 찍는다.
    """
    global _NEWS_NOTICE_SHOWN
    if not _NEWS_NOTICE_SHOWN:
        print(f"  {msg}")
        _NEWS_NOTICE_SHOWN = True


def fetch_news(name: str, cfg: dict, stats: dict | None = None) -> list[dict]:
    """종목 관련 뉴스. 이름만 겹치는 기사와 시세와 무관한 기사는 버린다.

    `stats`를 주면 {"raw", "kept"}를 누적한다 — 몇 건이 걸러졌는지 알아야
    '뉴스가 없다'와 '뉴스가 다 무관했다'를 구분할 수 있다.
    """
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        missing = [k for k, v in (("NAVER_CLIENT_ID", cid),
                                  ("NAVER_CLIENT_SECRET", csec)) if not v]
        _news_notice(f"뉴스 수집 안 함 — {', '.join(missing)} 미설정. "
                     "developers.naver.com에서 '검색' API를 추가한 앱의 키를 "
                     "Secrets에 등록하세요.")
        return []
    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            # 100은 네이버가 허용하는 최대치다. 30으로 받으면 잡음을 걸러낸 뒤
            # 남는 게 없다 — 이름이 흔한 종목일수록 무관한 기사가 앞을 채운다.
            params={"query": name, "display": 100, "sort": "date"},
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        # 401/403은 키가 틀렸거나 그 앱에 '검색' API가 안 붙은 것이다. 종목
        # 이름 문제가 아니므로 다음 종목에서도 똑같이 실패한다 — 사유를 밝힌다.
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (401, 403):
            _news_notice(f"네이버 뉴스 인증 실패({code}) — 키가 틀렸거나 그 "
                         "애플리케이션에 '검색' API가 추가되지 않았습니다.")
        else:
            print(f"  뉴스 수집 실패({name}): {e}")
        return []

    cutoff = datetime.now().astimezone() - timedelta(days=cfg["news_days"])
    raw, items = 0, []
    for it in resp.json().get("items", []):
        try:
            pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z")
        except (ValueError, KeyError):
            continue
        if pub < cutoff:
            continue
        raw += 1
        row = {
            "title": _strip(it.get("title", "")),
            "description": _strip(it.get("description", "")),
            "date": pub.strftime("%Y-%m-%d"),
            "link": it.get("originallink") or it.get("link", ""),
        }
        # 최신순으로 오므로 통과한 것부터 채우면 그대로 최신 관련 기사가 된다.
        if relevant(name, row):
            items.append(row)
        if len(items) >= cfg["news_per_stock"]:
            break
    if stats is not None:
        stats["raw"] = stats.get("raw", 0) + raw
        stats["kept"] = stats.get("kept", 0) + len(items)
        if not items and raw:
            stats.setdefault("all_noise", []).append(name)
    return items


def fetch_filings(ticker: str, corp_codes: dict[str, str], cfg: dict) -> list[dict]:
    api_key = os.getenv("DART_API_KEY")
    if not api_key or ticker not in corp_codes:
        return []
    end = datetime.now().strftime("%Y%m%d")
    begin = (datetime.now() - timedelta(days=cfg["dart_days"])).strftime("%Y%m%d")
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": api_key, "corp_code": corp_codes[ticker],
                    "bgn_de": begin, "end_de": end, "page_count": 20},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  공시 수집 실패({ticker}): {e}")
        return []
    if data.get("status") != "000":
        return []
    return [{"title": f["report_nm"].strip(), "date": f["rcept_dt"]}
            for f in data.get("list", [])]


def collect(candidates: list[dict], base_date: str, cfg: dict) -> dict:
    """후보 종목별 {ticker: {news, filings}} 수집 결과를 저장/반환한다."""
    corp_codes = {}
    if os.getenv("DART_API_KEY"):
        try:
            corp_codes = load_corp_codes()
        except Exception as e:
            print(f"DART corp_code 매핑 실패: {e}")

    out, stats = {}, {}
    for c in candidates:
        out[c["ticker"]] = {
            "news": fetch_news(c["name"], cfg, stats),
            "filings": fetch_filings(c["ticker"], corp_codes, cfg),
        }
        time.sleep(0.2)  # API rate limit 배려

    (DATA_DIR / f"evidence_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    n_news = sum(len(v["news"]) for v in out.values())
    n_fil = sum(len(v["filings"]) for v in out.values())
    print(f"뉴스 {n_news}건, 공시 {n_fil}건 수집")
    if stats.get("raw"):
        print(f"  뉴스 원본 {stats['raw']}건 중 {stats['kept']}건 채택 "
              f"(이름만 겹치거나 시세와 무관한 기사 제외)")
    # 한 건도 안 남은 종목은 '기사가 없다'가 아니라 '전부 동명이인이었다'는 뜻이다.
    # 원인 분석에서 '뉴스 없음'으로 보일 종목이라 미리 이름을 밝혀 둔다.
    if stats.get("all_noise"):
        names = ", ".join(stats["all_noise"][:8])
        more = f" 외 {len(stats['all_noise']) - 8}종목" if len(stats["all_noise"]) > 8 else ""
        print(f"  관련 뉴스 0건(검색 결과가 전부 무관): {names}{more}")
    return out
