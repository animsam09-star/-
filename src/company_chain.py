"""기업 대 기업 밸류체인.

산업 노드만으로는 수혜주를 못 고른다. '전공정 장비'라고 적어 두면 원익IPS·
유진테크·주성엔지니어링·피에스케이가 한 상자에 들어가는데, 넷은 만드는 장비가
다르고 따라서 어느 CAPEX에 붙어 있는지도 다르다. 산업은 분류지 거래가 아니다.

거래는 공시에 이름으로 적혀 있다.

    한온시스템   전방  현대자동차 21.4%·현대모비스 19.3%·Ford 12.5%
    HD한국조선해양 후방  강재(포스코·현대제철)
    포스코퓨처엠  전방  Ultium Cells·LG에너지솔루션·삼성SDI·SK온

이 모듈은 그 **이름**을 종목 마스터와 대조해 티커로 바꾸고, 회사→회사 엣지를
만든다. 산업 축이 '어느 쪽으로 번지는가'를 말한다면 이쪽은 '누구에게 가는가'를
말한다. 수혜주를 고르는 건 뒤쪽이다.

## 왜 정규식으로 회사명을 뽑지 않는가

'~전자', '~중공업' 같은 접미로 훑으면 '전방산업', '재료산업', '내화물산업'이
회사로 잡힌다(실제로 잡혔다). 존재하지 않는 회사를 만들어 내는 쪽이 못 찾는
것보다 나쁘다. 그래서 **종목 마스터에 실재하는 이름만** 인정한다. 마스터는
2,764종목이고, 거기 없는 이름은 비상장(Ultium Cells·Ford)이거나 오탈자다.
비상장 거래처도 정보이므로 버리지 않고 따로 모아 화면에 남긴다.
"""

from __future__ import annotations

import re

from .universe import Universe, normalize_name

# 공시 표기 → 상장 종목명. 사명이 바뀌었거나 공시가 약칭을 쓰는 경우다.
# 여기 없는 표기는 그냥 안 잡힌다 — 억지로 붙이는 것보다 낫다.
ALIASES = {
    "POSCO": "POSCO홀딩스",
    "포스코": "POSCO홀딩스",
    "현대차": "현대자동차",
    "기아차": "기아",
    "LG화학": "LG화학",
    "SK하이닉스": "SK하이닉스",
    "하이닉스": "SK하이닉스",
    "삼성전자": "삼성전자",
    "LG엔솔": "LG에너지솔루션",
    "한국조선해양": "HD한국조선해양",
    "현대중공업": "HD현대중공업",
    "두산에너빌리티": "두산에너빌리티",
}

# 이 길이 밑으로는 이름으로 안 본다. '한화'가 '한화솔루션'·'한화오션'·'한화시스템'
# 어느 쪽인지 문장만으로 못 가르는데, 짧을수록 우연히 걸릴 확률만 커진다.
MIN_NAME = 3

# 회사가 아니라 업종·유형을 가리키는 말. 이름 매칭 전에 걷어낸다.
_GENERIC = re.compile(r"(전방|후방|재료|소재|부품|장비|기타)?산업|업체|제조사|고객사|"
                      r"거래처|매출처|공급사|조선사|건설사|완성차")


def _candidates(text: str) -> list[str]:
    """문장에서 이름이 될 만한 토막. 구분자와 수치를 걷어낸 조각들이다."""
    cleaned = re.sub(r"\d+(\.\d+)?\s*%", " ", str(text or ""))
    cleaned = _GENERIC.sub(" ", cleaned)
    parts = re.split(r"[·・,/()（）\[\]{}\s、;:]+", cleaned)
    return [p.strip("’'\"“”·") for p in parts if p.strip()]


def find_counterparties(text: str, universe: Universe
                        ) -> tuple[dict[str, str], list[str]]:
    """(상장 거래처 {티커: 잡힌 표기}, 상장으로 확인 안 된 이름들).

    확인 안 된 이름을 함께 돌려주는 이유는 그게 대개 **비상장 거래처**이기
    때문이다(Ultium Cells, Ford, Caterpillar). 파급 대상은 아니지만 '이 회사가
    누구에게 파는가'의 절반이라, 버리면 화면이 실제보다 좁아 보인다.
    """
    hits: dict[str, str] = {}
    unknown: list[str] = []
    for token in _candidates(text):
        if len(token) < MIN_NAME and token not in ALIASES:
            continue
        name = ALIASES.get(token, token)
        ticker = universe.resolve(name)
        if ticker:
            hits.setdefault(ticker, token)
        elif re.search(r"[가-힣A-Za-z]{3,}", token):
            unknown.append(token)
    return hits, unknown


def _share(text: str, token: str) -> float | None:
    """'현대자동차 21.4%'에서 21.4%를 떼어 온다.

    비중이 붙어 있으면 그건 매출 의존도이고, 파급의 크기를 가늠하는 유일한
    정량 근거다. 이름 **바로 뒤**에 오는 것만 인정한다 — 문장 어디선가 찾으면
    옆 회사 비중을 끌어다 붙인다.
    """
    m = re.search(re.escape(token) + r"\s*[(（]?\s*(\d+(?:\.\d+)?)\s*%", str(text))
    return round(float(m.group(1)) / 100, 4) if m else None


def from_extractions(extractions: dict, universe: Universe, asof: str
                     ) -> tuple[list[dict], dict]:
    """{티커: 추출결과} → 회사 간 거래 엣지.

    방향은 **물건이 흐르는 쪽**으로 통일한다. 전방(고객)은 나 → 고객,
    후방(매입처)은 공급사 → 나. 산업 축과 같은 규약이라 두 축을 겹쳐 볼 수 있다.
    """
    from . import graph as G

    edges: list[dict] = []
    unlisted: dict[str, list[str]] = {}
    for ticker, doc in (extractions or {}).items():
        for key, forward in (("downstream", True), ("upstream", False)):
            for item in doc.get(key) or []:
                # **인용문에서는 이름을 찾지 않는다.** 인용문은 검증용이라 길고,
                # 이 관계와 무관한 회사가 잔뜩 들어 있다. 실제로 삼성중공업의
                # 강재 매입 인용문에서 한화엔진·한국카본이 잡혀 '한화엔진이
                # 삼성중공업에 강재를 판다'는 없는 거래가 만들어졌다.
                # 거래 상대는 customer/material 칸에 적힌 것이다.
                text = str(item.get("customer") or item.get("material") or "")
                hits, unknown = find_counterparties(text, universe)
                what = str(item.get("customer") or item.get("material") or "")
                for other, token in hits.items():
                    if other == ticker:
                        continue     # 자기 자신은 거래가 아니다
                    src, dst = ((ticker, other) if forward else (other, ticker))
                    edges.append(G.make_edge(
                        G.ticker_node(src), G.ticker_node(dst), G.REL_DOWNSTREAM,
                        "dart", origin=ticker, asof=asof,
                        weight=_share(text, token),
                        product=what[:80],
                        confidence=0.8,
                        evidence=str(item.get("quote") or "")[:150]))
                for u in unknown:
                    unlisted.setdefault(ticker, []).append(u)
    return edges, unlisted
