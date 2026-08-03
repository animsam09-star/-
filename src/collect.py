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


def fetch_news(name: str, cfg: dict) -> list[dict]:
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
            params={"query": name, "display": 30, "sort": "date"},
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
    items = []
    for it in resp.json().get("items", []):
        try:
            pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z")
        except (ValueError, KeyError):
            continue
        if pub < cutoff:
            continue
        items.append({
            "title": _strip(it.get("title", "")),
            "description": _strip(it.get("description", "")),
            "date": pub.strftime("%Y-%m-%d"),
            "link": it.get("originallink") or it.get("link", ""),
        })
        if len(items) >= cfg["news_per_stock"]:
            break
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

    out = {}
    for c in candidates:
        out[c["ticker"]] = {
            "news": fetch_news(c["name"], cfg),
            "filings": fetch_filings(c["ticker"], corp_codes, cfg),
        }
        time.sleep(0.2)  # API rate limit 배려

    (DATA_DIR / f"evidence_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    n_news = sum(len(v["news"]) for v in out.values())
    n_fil = sum(len(v["filings"]) for v in out.values())
    print(f"뉴스 {n_news}건, 공시 {n_fil}건 수집")
    return out
