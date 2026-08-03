"""「단일판매·공급계약체결」 공시 → 회사 대 회사 거래.

## 왜 이 경로가 필요한가

사업보고서에서 거래처를 뽑는 방식은 회사가 이름을 **써 줬을 때만** 통한다.
실제로 152종목을 훑어 회사 간 거래 63건을 얻었는데, 그 과정에서 엘앤에프는
'기술·정보유출 우려', 주성엔지니어링은 '고객 투자정보 노출', 알에스오토메이션은
'R사·L사'로 익명 처리해 전방이 통째로 비었다. 게다가 서술형 문장이라 사람이
읽고 옮겨야 해서 종목당 비용이 크다 — 2,763종목에는 못 쓴다.

공급계약 공시는 형식이 다르다. 계약상대방·계약금액·계약기간이 **정해진 항목**으로
들어 있어 파싱만으로 읽히고, 익명 처리가 원칙적으로 불가능하다(계약 상대를 밝히는
것이 공시의 목적이다). 그래서

  - LLM이 필요 없다 (429에 막히지 않는다)
  - 공시를 낸 모든 상장사가 대상이다 (152종목 제한이 사라진다)
  - **계약금액과 기간이 숫자로 온다** — 매출 비중보다 시차 분석에 낫다

## 한계 (숨기지 않는다)

계약을 안 낸 회사는 안 잡힌다. 공시 의무는 '최근 매출액의 일정 비율 이상'인
대형 계약에만 붙으므로, 소액 다건으로 파는 회사는 빠진다. 즉 이 경로는
사업보고서 경로를 **대체하지 않고 보탠다.**

계약상대방이 해외·비상장인 경우도 많다. 티커로 안 붙지만 버리지 않고 따로 센다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import dart
from .universe import Universe

# 공시 이름으로 거른다. 거래소공시(pblntf_ty=I)에는 수백 종류가 섞여 있고,
# 이름에 '공급계약'이 들어가는 건 사실상 이 유형뿐이다.
CONTRACT_PATTERNS = ("단일판매", "공급계약", "수주")

# 정정공시는 원본과 같은 계약을 다시 낸 것이다. 금액이 바뀌었을 수 있으니
# **나중 것이 이긴다** — 접수일 순으로 처리해 뒤엣것이 앞엣것을 덮게 둔다.
_CORRECTION = re.compile(r"\[기재정정\]|\[정정\]|정정신고")


def _num(text: str) -> float | None:
    """'197,970,240,000' → 197970240000.0. 못 읽으면 None."""
    m = re.search(r"[\d,]{4,}", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _date(text: str) -> str:
    """'2022-11-01', '2022.11.01', '2022년 11월 1일' → '20221101'. 못 읽으면 ''."""
    s = str(text or "")
    m = re.search(r"(\d{4})\s*[-.년/]\s*(\d{1,2})\s*[-.월/]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    m = re.search(r"\b(\d{8})\b", s)
    return m.group(1) if m else ""


# 공시 서식의 항목 이름. 회사마다 띄어쓰기·중점이 달라 느슨하게 잡는다.
_FIELDS = {
    "counterparty": r"계약\s*상대(?:방|)",
    "product": r"(?:판매·?공급\s*계약\s*내용|계약\s*내용|공급\s*품목|계약\s*목적물)",
    "amount": r"계약\s*금액",
    "recent_sales": r"최근\s*매출액",
    "ratio": r"매출액\s*대비",
    "begin": r"(?:계약\s*)?시작일",
    "end": r"(?:계약\s*)?종료일",
    "period": r"계약\s*기간",
    "region": r"판매·?공급\s*지역",
}


def parse_contract(text: str) -> dict:
    """공시 본문 텍스트 → 계약 필드.

    표를 XML에서 평문으로 편 뒤라 '항목명 값' 형태로 한 줄에 붙거나 다음 줄로
    넘어간다. 둘 다 받도록 항목명 뒤 200자 안에서 값을 찾는다. 너무 넓게 잡으면
    다음 항목의 값을 끌어오므로, 값을 찾으면 거기서 끊는다.
    """
    flat = re.sub(r"[ \t ]+", " ", str(text or ""))
    out: dict = {}
    for key, pat in _FIELDS.items():
        m = re.search(pat + r"\s*[:：]?\s*(.{0,200})", flat, re.S)
        out[key] = m.group(1).strip() if m else ""

    amount = _num(out["amount"])
    # 퍼센트 기호가 숫자 뒤에 오기도('48.05%'), 항목명 쪽에 붙기도 한다
    # ('매출액대비(%) 48.05'). 한쪽만 보면 서식에 따라 조용히 비는데, 비중은
    # 이 공시에서 파급 크기를 재는 유일한 값이라 비면 안 된다.
    ratio = None
    rm = (re.search(r"([\d.]+)\s*%", out["ratio"])
          or re.search(r"\(\s*%\s*\)\s*([\d.]+)", out["ratio"])
          or re.search(r"([\d.]+)", out["ratio"]))
    if rm:
        try:
            pct = float(rm.group(1))
        except ValueError:
            pct = None
        # 100%를 넘는 값은 비중이 아니라 옆 항목의 금액을 끌어온 것이다.
        ratio = round(pct / 100, 4) if pct is not None and 0 < pct <= 100 else None

    begin = _date(out["begin"]) or _date(out["period"])
    # 기간이 '2022-11-01 ~ 2026-07-29' 한 줄로 오면 두 번째 날짜가 종료일이다.
    end = _date(out["end"])
    if not end:
        dates = re.findall(r"\d{4}\s*[-.년/]\s*\d{1,2}\s*[-.월/]\s*\d{1,2}",
                           out["period"])
        end = _date(dates[1]) if len(dates) > 1 else ""

    counterparty = _clean(out["counterparty"])
    return {
        "counterparty": counterparty if _is_name(counterparty) else "",
        "product": _clean(out["product"])[:80],
        "amount": amount,
        "recent_sales": _num(out["recent_sales"]),
        "sales_ratio": ratio,
        "begin": begin,
        "end": end,
        "region": _clean(out["region"])[:40],
    }


_NOISE = re.compile(r"^[\s:：·\-]+|[\s:：·\-]+$")

# 값이 아니라 서식의 안내문·각주다. 이게 섞이면 '과 계약을 체결한 일자입니다. -
# 상기 8. 공시유보…' 같은 문장이 거래처 이름으로 들어간다(실제로 60건 중 2건).
_BOILERPLATE = re.compile(
    r"입니다|상기\s*\d|해당사항|기재하지|참조|공시유보|계약을\s*체결")


def _clean(s: str) -> str:
    """항목 값에서 다음 항목 이름과 안내문이 딸려 온 부분을 잘라낸다."""
    s = str(s or "").split("\n")[0]
    for pat in _FIELDS.values():
        s = re.split(pat, s)[0]
    # 각주 표시는 '주1)' 또는 줄 앞의 '주)'다. 그냥 `주\)`로 자르면 회사명의
    # **'(주)'를 먹는다** — '삼성전자(주)'가 '삼성전자('로 잘려 티커에 안 붙었다.
    s = re.split(r"\(단위|(?<![(가-힣])주\d*\)|※|비고", s)[0]
    # 짝이 안 맞는 여는 괄호 뒤는 잘린 조각이다. 괄호째 버린다.
    if s.count("(") > s.count(")"):
        s = s[:s.rfind("(")]
    return _NOISE.sub("", s)


def _is_name(s: str) -> bool:
    """거래처 이름으로 볼 만한가.

    못 읽은 것을 빈칸으로 두면 '거래처가 없다'로 읽히지만, 안내문을 이름으로
    두면 **없는 회사가 생긴다.** 후자가 나쁘므로 의심스러우면 버린다.
    """
    s = (s or "").strip()
    if len(s) < 2 or _BOILERPLATE.search(s):
        return False
    # 조사 하나만 남은 조각('과', '와')이나 문장부호만 남은 것
    return bool(re.search(r"[가-힣A-Za-z]{2,}", s))


# corp_code 없이 조회할 때 DART가 허용하는 최대 기간.
#   "DART 오류 100: corp_code가 없는 경우 검색기간은 3개월만 가능합니다."
# 3개월이라 했으니 80일로 잘라 여유를 둔다. 기간을 나눠 여러 번 부르면 되는
# 문제라, 시장 전체를 훑는다는 이 경로의 전제는 그대로다.
WINDOW_DAYS = 80


def _search_window(begin: str, end: str, page_limit: int) -> list[dict]:
    found: list[dict] = []
    for page in range(1, page_limit + 1):
        data = dart._get("list.json", {
            "bgn_de": begin, "end_de": end, "pblntf_ty": "I",
            "page_count": 100, "page_no": page}, timeout=30).json()
        status = data.get("status")
        if status == "013":          # 해당 기간에 공시 없음 — 정상적인 빈 결과
            break
        if status != "000":
            raise dart.DartError(f"DART 오류 {status}: {data.get('message')}")
        for f in data.get("list") or []:
            name = (f.get("report_nm") or "")
            if any(p in name for p in CONTRACT_PATTERNS):
                found.append({"rcept_no": f["rcept_no"], "report_nm": name.strip(),
                              "rcept_dt": f.get("rcept_dt", ""),
                              "corp_name": f.get("corp_name", ""),
                              "stock_code": (f.get("stock_code") or "").strip()})
        if page >= int(data.get("total_page") or 1):
            break
    return found


def search(days_back: int = 90, page_limit: int = 20) -> list[dict]:
    """최근 N일치 공급계약 공시 목록. 회사 단위가 아니라 **시장 전체**를 훑는다.

    corp_code를 안 주면 DART는 그 기간의 모든 공시를 준다. 그래서 종목 수와
    무관하게 돈다 — 이 경로가 152종목 제한을 푸는 이유다. 다만 그때는 검색기간이
    3개월로 제한되므로 요청 기간을 창으로 잘라 이어 붙인다.
    """
    now = datetime.now()
    found: list[dict] = []
    offset = 0
    while offset < days_back:
        span = min(WINDOW_DAYS, days_back - offset)
        end = (now - timedelta(days=offset)).strftime("%Y%m%d")
        begin = (now - timedelta(days=offset + span)).strftime("%Y%m%d")
        found += _search_window(begin, end, page_limit)
        offset += span

    # 창 경계에서 같은 공시가 두 번 잡힐 수 있다. 접수번호로 겹치는 것을 지운다.
    seen: dict[str, dict] = {}
    for f in found:
        seen[f["rcept_no"]] = f
    # 접수일 오름차순 — 정정공시가 원본을 덮게 하려면 나중 것이 뒤에 와야 한다.
    return sorted(seen.values(), key=lambda f: f["rcept_dt"])


def to_edges(contracts: list[dict], universe: Universe, asof: str
             ) -> tuple[list[dict], dict]:
    """계약 목록 → 회사 간 거래 엣지, 그리고 상장으로 안 붙은 상대방들.

    방향은 **물건이 흐르는 쪽**이다. 공시를 낸 회사가 파는 쪽이므로
    공시자 → 계약상대방이다. 사업보고서 경로와 같은 규약이라 겹쳐 볼 수 있다.

    weight는 매출액 대비 비중을 쓴다. 없으면 계약금액을 최근매출액으로 나눠
    직접 만든다 — 그 두 값은 서식에 거의 항상 함께 있다.
    """
    from . import graph as G

    edges: list[dict] = []
    unlisted: dict[str, str] = {}
    for c in contracts:
        seller = (c.get("stock_code") or "").strip()
        if not seller or seller not in universe:
            continue          # 비상장 공시자는 파급 대상이 아니다
        info = c.get("parsed") or {}
        buyer = universe.resolve(info.get("counterparty", ""))
        if not buyer:
            if info.get("counterparty"):
                unlisted[info["counterparty"]] = c.get("corp_name", "")
            continue
        if buyer == seller:
            continue
        ratio = info.get("sales_ratio")
        if ratio is None and info.get("amount") and info.get("recent_sales"):
            ratio = round(info["amount"] / info["recent_sales"], 4)
        edges.append(G.make_edge(
            G.ticker_node(seller), G.ticker_node(buyer), G.REL_DOWNSTREAM,
            "contract", origin=seller, asof=asof, weight=ratio,
            product=info.get("product", "")[:80], confidence=0.9,
            evidence=(f"{c.get('report_nm','')}({c.get('rcept_dt','')}) "
                      f"{info.get('begin','')}~{info.get('end','')} "
                      f"{int(info['amount']):,}원" if info.get("amount")
                      else f"{c.get('report_nm','')}({c.get('rcept_dt','')})")[:150]))
    return edges, unlisted


# ---- 수집 CLI ------------------------------------------------------------
#
# 수집(공시 조회·본문 내려받기)과 그래프 반영을 나눈다. 수집은 DART 키가 필요하고
# 수백 번 요청하므로 워크플로에서 돌리고, 결과 JSON을 커밋한다. 그래프 반영은
# 그 파일만 있으면 되므로 파이프라인이 매번 다시 한다 — 사업보고서 경로와 같은 구조다.

CONTRACTS_FILE = dart.ROOT / "data" / "contracts.json" if hasattr(dart, "ROOT") else None


def collect(days_back: int = 365, limit: int | None = None) -> list[dict]:
    """공시 목록을 훑고 본문을 받아 필드까지 채운 계약 목록."""
    filings = search(days_back=days_back)
    if limit:
        filings = filings[-limit:]
    print(f"공급계약 공시 {len(filings)}건 (최근 {days_back}일)", flush=True)

    out: list[dict] = []
    failed = 0
    for i, f in enumerate(filings, 1):
        try:
            text = dart.xml_to_text(dart.fetch_document(f["rcept_no"]))
            f["parsed"] = parse_contract(text)
            out.append(f)
        except Exception as e:                    # 한 건 실패로 전체를 버리지 않는다
            failed += 1
            if failed <= 5:
                print(f"  본문 실패 {f['rcept_no']} {f.get('corp_name','')}: {e}",
                      flush=True)
        if i % 50 == 0:
            print(f"  ... {i}/{len(filings)}", flush=True)
    named = sum(1 for f in out if f["parsed"]["counterparty"])
    amounts = sum(1 for f in out if f["parsed"]["amount"])
    print(f"파싱 완료 {len(out)}건 (실패 {failed}건) — "
          f"계약상대방 확보 {named}건, 금액 확보 {amounts}건", flush=True)
    return out


def main():
    import argparse
    import json
    from pathlib import Path

    p = argparse.ArgumentParser(description="공급계약 공시를 모아 회사 간 거래를 만든다.")
    p.add_argument("--days", type=int, default=365, help="조회 기간(일). 기본 365")
    p.add_argument("--limit", type=int, help="본문을 받을 최대 건수(시험용)")
    p.add_argument("--out", default="data/contracts.json")
    args = p.parse_args()

    rows = collect(days_back=args.days, limit=args.limit)
    path = Path(args.out)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {path} ({len(rows)}건)")


def load(path=None) -> list[dict]:
    """저장된 계약 목록. 없으면 빈 목록 — 이 경로가 아직 안 돌았다는 뜻이다."""
    import json
    from pathlib import Path
    path = Path(path or (Path(__file__).resolve().parent.parent / "data" / "contracts.json"))
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  계약 파일을 읽지 못했습니다: {e}")
        return []


if __name__ == "__main__":
    main()
