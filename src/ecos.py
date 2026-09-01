"""한국은행 ECOS 어댑터 — 산업연관표를 엣지 가중치로 쓰기 위한 진입점.

## 왜 필요한가

DART 경로는 관계의 **근거**가 강하다(회사가 직접 쓴 문장 + 인용 검증). 대신
**정량 강도가 자주 비어 있다.** 2026-07-28 추출에서 SK하이닉스는 원재료의 47%가
'기타'였고, 현대자동차·삼성SDI·에코프로비엠은 원재료 표가 선별 본문에 잡히지
않아 후방을 하나도 특정하지 못했다. 산업연관표는 그 자리를 계수로 메운다.

반대로 산업연관표만으로는 **종목까지 못 내려간다.** '반도체가 화학에서 X%
투입받는다'는 알려주지만 그게 솔브레인인지 동진쎄미켐인지는 말해 주지 않는다.
둘은 대체 관계가 아니라 서로의 구멍을 메우는 관계다.

## 부호를 계수에서 가져오면 안 된다

투입계수는 **금액 기준 평균**이라 방향(강도)만 있고 부호가 없다. 구리 가격이
오를 때 제련은 재고 평가익으로 수혜, 전선은 원가로 피해인데 계수는 둘을
구분하지 않는다. 계수는 weight로만 쓰고, sign은 DART·팩터 쪽에서 가져온다.

## 필드명을 추측하지 않는다

KRX에서 필드명을 잘못 짚어 예외 없이 빈 컬럼이 된 적이 있다. 여기서는 통계표
목록을 **먼저 조회해** 산업연관표에 해당하는 표를 찾고, 그 표의 실제 항목
구성을 찍어서 확정한다. 아래 URL 구조는 **아직 라이브로 확인하지 않은 가정**이며,
discover()가 그것부터 검증한다.

    https://ecos.bok.or.kr/api/{서비스}/{인증키}/json/kr/{시작}/{끝}/...
"""

from __future__ import annotations

import os

import requests

BASE_URL = "https://ecos.bok.or.kr/api"

# 등록된 이름이 하나로 정해져 있지 않다. 이름이 어긋나면 '키 없음'으로 조용히
# 건너뛰어 산출물이 비고, 그건 '산업 간 연관이 없다'와 리포트에서 구분되지 않는다.
# 그래서 흔한 변형을 모두 본다. **값은 절대 돌려주지 않고 이름만 알린다.**
ENV_KEYS = ("ECOS_API", "ECOS_API_KEY", "ECOS_KEY", "BOK_API_KEY")
ENV_KEY = ENV_KEYS[0]

# 통계표 목록에서 산업연관표를 골라내는 데 쓰는 말들. 이름이 정확히 무엇인지
# 모르므로 넓게 잡고, 걸린 것을 사람이 보고 고른다.
IO_TABLE_HINTS = ("산업연관", "투입산출", "투입계수", "생산유발")

MISSING_HINT = (
    f"ECOS 인증키가 없습니다. 다음 중 하나로 등록하세요: {', '.join(ENV_KEYS)}\n"
    "  ecos.bok.or.kr → 오픈API → 인증키 신청에서 무료로 발급됩니다.\n"
    "  GitHub Actions: Settings → Secrets and variables → Actions")


class EcosError(RuntimeError):
    pass


def credential_kind() -> str:
    """찾은 환경 변수의 **이름**. 없으면 빈 문자열. 값은 절대 돌려주지 않는다."""
    for name in ENV_KEYS:
        if os.getenv(name):
            return name
    return ""


def has_key() -> bool:
    return bool(credential_kind())


def _key() -> str:
    name = credential_kind()
    if not name:
        raise EcosError(MISSING_HINT)
    return os.environ[name]


def fetch(service: str, *path, start: int = 1, end: int = 100,
          timeout: int = 30) -> list[dict]:
    """ECOS 한 서비스의 레코드 목록.

    ECOS는 오류를 HTTP 상태가 아니라 **본문의 RESULT 블록**으로 돌려준다.
    그걸 안 보면 '결과 0건'과 '인증키 오류'가 호출부에서 똑같아 보인다.
    """
    parts = [BASE_URL, service, _key(), "json", "kr", str(start), str(end)]
    parts.extend(str(p) for p in path)
    url = "/".join(parts)

    try:
        resp = requests.get(url, timeout=timeout)
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise EcosError(f"{service} 조회 실패: {e}") from None

    if "RESULT" in payload:
        r = payload["RESULT"]
        raise EcosError(
            f"{service} 오류 {r.get('CODE')}: {r.get('MESSAGE')}\n"
            f"  (인증키·서비스명·요청 범위를 확인하세요)")

    # 서비스마다 최상위 키 이름이 다르다. 하나뿐이므로 이름을 가정하지 않고 집는다.
    keys = [k for k in payload if isinstance(payload.get(k), dict)]
    if len(keys) != 1:
        raise EcosError(
            f"{service} 응답 구조를 해석하지 못했습니다. 최상위 키: {list(payload)[:10]}")
    block = payload[keys[0]]
    rows = block.get("row")
    if rows is None:
        raise EcosError(
            f"{service} 응답에 row가 없습니다. 실제 키: {list(block)[:10]}")
    return rows


def discover(limit: int = 1000) -> dict:
    """산업연관표에 해당하는 통계표를 찾아 실제 응답 구조를 함께 돌려준다.

    첫 라이브 실행에서 이걸 찍어 통계표코드와 필드명을 확정한다. 확정 전까지는
    어떤 코드도 소스에 박지 않는다 — 박아 두면 틀렸을 때 예외가 아니라 빈
    결과가 되고, 그건 '연관이 없다'와 구분되지 않는다.
    """
    rows = fetch("StatisticTableList", start=1, end=limit)
    if not rows:
        # 키 구성을 빈 경우에도 같게 유지한다. 호출부가 match_count를 못 찾고
        # KeyError로 죽으면, 조회가 비었다는 사실이 예외에 묻힌다.
        return {"fields": [], "sample": {}, "total": 0,
                "matches": [], "match_count": 0}

    fields = sorted(rows[0])
    matches = [r for r in rows
               if any(h in str(r.get(f, "")) for f in fields for h in IO_TABLE_HINTS)]
    return {
        "fields": fields,
        "sample": rows[0],
        "total": len(rows),
        "matches": [{k: r.get(k) for k in fields} for r in matches[:40]],
        "match_count": len(matches),
    }


if __name__ == "__main__":   # python -m src.ecos
    import json
    import sys

    if not has_key():
        sys.exit(MISSING_HINT)
    try:
        out = discover()
    except EcosError as e:
        sys.exit(str(e))
    print(f"통계표 {out['total']}건 중 산업연관 관련 {out['match_count']}건")
    print(f"필드: {out['fields']}\n")
    print(json.dumps(out["matches"], ensure_ascii=False, indent=1)[:4000])
