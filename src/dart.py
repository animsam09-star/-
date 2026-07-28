"""DART 원문 수집 — 사업보고서의 '사업의 내용'을 텍스트로 뽑는다.

수직축 엣지를 1차 자료에서 만들기 위한 원료 공급 모듈이다. 추론은 하지 않고,
공시 원문을 가져와 필요한 절만 잘라 캐시하는 데까지만 책임진다.

## 익명화 문제와 그 해법

사업보고서의 매출처·매입처는 대체로 익명이다("A사", "국내 대형 건설사").
그래서 '쌍용C&E → 현대건설' 같은 회사 쌍은 공시에서 못 만든다.

**이분 그래프가 이 문제를 통째로 우회한다.** 필요한 건 회사 쌍이 아니라
산업 간 관계이고, 그건 양쪽에서 독립적으로 나온다.

    쌍용C&E 사업보고서: 주요 제품 = 시멘트 / 매출처 = 건설업체
    현대건설 사업보고서: 주요 원재료 = 시멘트·레미콘

'시멘트 →전방→ 건설'이 두 문서에서 각각 도출되고, 서로 교차 검증까지 된다.
특정 거래 상대를 몰라도 된다.

## 왜 문서 구조가 아니라 제목 텍스트로 자르는가

DART document API는 확장자가 .xml이지만 실제로는 ZIP을 준다. 안의 XML은 DART
고유 마크업이고 시기·회사별로 태그 구조가 다르다. 태그 구조에 의존하면 조용히
빈 결과가 나오므로, 태그는 텍스트화에만 쓰고 절 경계는 **제목 문자열**로 찾는다.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOC_CACHE = DATA_DIR / "dart_docs"

API = "https://opendart.fss.or.kr/api"

# 정기공시 중 '사업의 내용'이 가장 충실한 순서. 사업보고서가 최우선이다.
REPORT_PRIORITY = ("사업보고서", "반기보고서", "분기보고서")

# 로마숫자는 전각(Ⅱ)과 반각(II), 아라비아(2)가 섞여 나온다.
# 'II'는 두 글자라 문자 클래스로는 못 잡는다 — 반드시 교체(alternation)여야 한다.
_SECTION_START = re.compile(r"(?:^|\n)[ \t]*(?:Ⅱ|II|2)[.\s][ \t.]{0,3}\s*사업의\s*내용")
_SECTION_END = re.compile(
    r"(?:^|\n)[ \t]*(?:Ⅲ|III|3)[.\s][ \t.]{0,3}\s*(?:재무에\s*관한\s*사항|재무제표)")

# 절을 못 찾았다고 볼 하한. 목차 항목은 수십 자에 그치므로 이보다 훨씬 짧다.
MIN_SECTION_CHARS = 200
# 제목 줄에서 제목 뒤에 허용하는 글자 수. 실제 상호참조는 "- 5. 위험관리 및
# 파생거래를 참조하시기 바랍니다."처럼 길다. 목차의 점선·쪽번호는 짧아서 통과한다.
MAX_HEADING_TAIL = 12

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
_BLANK = re.compile(r"\n{3,}")


class DartError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("DART_API_KEY")
    if not key:
        raise DartError("DART_API_KEY가 설정되지 않았습니다.")
    return key


def _get(path: str, params: dict, timeout: int = 60) -> requests.Response:
    resp = requests.get(f"{API}/{path}", params={**params, "crtfc_key": _key()},
                        timeout=timeout)
    resp.raise_for_status()
    return resp


def load_corp_codes(force: bool = False) -> dict[str, str]:
    """종목코드 → DART corp_code. collect.py와 공유하는 단일 구현."""
    cache = DATA_DIR / "corp_codes.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    resp = _get("corpCode.xml", {})
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml = zf.read(zf.namelist()[0]).decode("utf-8")
    mapping = {}
    for el in ElementTree.fromstring(xml).iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if stock and corp:
            mapping[stock] = corp
    DATA_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps(mapping), encoding="utf-8")
    return mapping


def find_business_report(corp_code: str, years_back: int = 2) -> dict | None:
    """가장 최근의 정기보고서 접수번호를 찾는다.

    사업보고서 > 반기 > 분기 순으로 고른다. '사업의 내용'의 원재료·매출처 표는
    사업보고서가 가장 상세하다.
    """
    end = datetime.now().strftime("%Y%m%d")
    begin = f"{datetime.now().year - years_back}0101"
    try:
        data = _get("list.json", {"corp_code": corp_code, "bgn_de": begin,
                                  "end_de": end, "pblntf_ty": "A",
                                  "page_count": 100}, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        raise DartError(f"공시 목록 조회 실패: {e}") from e

    status = data.get("status")
    if status == "013":          # 조회된 데이터 없음 — 정상적인 빈 결과다
        return None
    if status != "000":
        raise DartError(f"DART 오류 {status}: {data.get('message')}")

    filings = data.get("list") or []
    for kind in REPORT_PRIORITY:
        matches = [f for f in filings if kind in (f.get("report_nm") or "")]
        if matches:
            best = max(matches, key=lambda f: f.get("rcept_dt", ""))
            return {"rcept_no": best["rcept_no"], "report_nm": best["report_nm"].strip(),
                    "rcept_dt": best.get("rcept_dt", ""), "corp_name": best.get("corp_name", "")}
    return None


def _decode(raw: bytes) -> str:
    """XML 선언의 인코딩을 우선 쓰고, 아니면 후보를 순서대로 시도한다.

    DART 문서는 EUC-KR이 많지만 최근 건은 UTF-8도 있다. 잘못 디코딩하면
    예외 없이 깨진 글자만 나오고, 그 상태로 LLM에 들어가면 조용히 헛것을 읽는다.
    """
    head = raw[:200].decode("ascii", errors="ignore").lower()
    m = re.search(r'encoding=["\']([\w-]+)["\']', head)
    candidates = [m.group(1)] if m else []
    candidates += ["utf-8", "cp949", "euc-kr"]

    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp949", errors="replace")


def xml_to_text(xml: str) -> str:
    """DART 마크업을 사람이 읽는 텍스트로. 표 구조는 살린다.

    원재료 비중·매출처 표가 이 모듈의 핵심 산출물이라, 셀 경계를 뭉개면
    '어느 원재료가 몇 %'인지가 사라진다.
    """
    text = re.sub(r"<!--.*?-->", " ", xml, flags=re.S)
    text = re.sub(r"</(?:TR|TABLE|P|TITLE|SECTION-\d)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<BR\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:TD|TE|TH)\s*>", " | ", text, flags=re.I)
    text = _TAG.sub("", text)
    # 수치 문자 참조(&#183;)까지 풀어야 한다. 남겨 두면 LLM이 원문에서 '&#183;'를 보고
    # 인용에는 '·'로 적어, 정상 관계가 인용 검증에서 환각으로 오판돼 폐기된다.
    text = html.unescape(text).replace("\xa0", " ")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip(" |").strip() for line in text.split("\n"))
    return _BLANK.sub("\n\n", text).strip()


def fetch_document(rcept_no: str, use_cache: bool = True) -> str:
    """접수번호의 원문 전체를 텍스트로. data/dart_docs/에 캐시한다.

    같은 보고서를 두 번 받지 않는다 — 공시는 확정된 과거 문서라 변하지 않고,
    document API가 이 파이프라인에서 가장 무거운 호출이다.
    """
    DOC_CACHE.mkdir(parents=True, exist_ok=True)
    cached = DOC_CACHE / f"{rcept_no}.txt"
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")

    resp = _get("document.xml", {"rcept_no": rcept_no})
    body = resp.content

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")] or zf.namelist()
            if not names:
                raise DartError(f"문서 압축 파일이 비어 있습니다: {rcept_no}")
            # 본문이 가장 큰 파일이다(첨부·표지보다 크다)
            main = max(names, key=lambda n: zf.getinfo(n).file_size)
            raw = zf.read(main)
    except zipfile.BadZipFile:
        # 오류 응답은 ZIP이 아니라 XML로 온다
        msg = _decode(body)[:300]
        raise DartError(f"문서 조회 실패({rcept_no}): {msg}") from None

    text = xml_to_text(_decode(raw))
    cached.write_text(text, encoding="utf-8")
    return text


def extract_business_section(text: str) -> str:
    """'II. 사업의 내용'만 잘라낸다.

    전체 문서는 수십만 자라 그대로 LLM에 넣으면 비싸고 정확도도 떨어진다.
    필요한 정보(주요 제품, 원재료, 매출처)는 전부 이 절에 있다.

    **목차 함정** — 문서 앞머리 목차에도 같은 제목이 있다. 첫 매치를 쓰면 목차
    몇 줄만 잘라 놓고 성공한 것처럼 보인다. 목차에서는 'III. 재무에 관한 사항'이
    바로 다음 줄에 오므로 길이가 수십 자에 그쳐, 더 긴 후보가 이긴다.

    **상호참조 함정** — 뒤쪽 절에 "Ⅱ. 사업의 내용 - 5. 위험관리를 참조하시기
    바랍니다" 같은 문장이 있다. 여기서 시작하면 이미 III을 지난 뒤라 **종료
    경계가 없고**, 그 후보가 문서 끝까지 이어져 가장 길어진다. 길이만 보면
    상호참조가 항상 이긴다 — 실제로 현대건설과 LS가 그렇게 잘못 잘렸다.

    그래서 두 가지를 요구한다.
      1. 제목 줄에는 제목만 있어야 한다. 뒤에 문장이 붙어 있으면 상호참조다.
      2. 종료 경계를 찾은 후보를 우선한다. 못 찾은 후보(문서 끝까지)는 길이가
         아무리 길어도 뒤로 밀린다.

    경계를 못 찾으면 빈 문자열을 돌려준다 — 통짜 문서를 넘기면 비용만 커지고
    엉뚱한 절을 읽는다.
    """
    bounded, unbounded = "", ""
    for start in _SECTION_START.finditer(text):
        # 제목 줄의 나머지. "- 5. 위험관리를 참조하시기 바랍니다" 같은 꼬리가
        # 붙어 있으면 제목이 아니라 본문 속 상호참조다.
        line_end = text.find("\n", start.end())
        tail = text[start.end():line_end if line_end != -1 else len(text)].strip()
        if len(tail) > MAX_HEADING_TAIL:
            continue

        body = text[start.start():]
        end = _SECTION_END.search(body, 1)
        section = (body[:end.start()] if end else body).strip()
        if end:
            bounded = max(bounded, section, key=len)
        else:
            unbounded = max(unbounded, section, key=len)

    best = bounded or unbounded
    return best if len(best) >= MIN_SECTION_CHARS else ""


# '사업의 내용' 안의 하위 절 제목.
#
# 문구만으로 찾으면 **재무제표 표의 셀에 걸린다.** 실제로 '매출액', '영업이익'이
# 절 제목으로 잡혀 선별이 무력화됐다(168,442자 → 상한에 그대로 걸림). 그래서
# 제목의 '형태'를 함께 요구한다:
#   - 줄 전체가 제목일 것 (뒤에 데이터가 붙어 있으면 표의 행이다)
#   - 번호가 앞에 붙을 것 (1. / 가. / (1) — 회사마다 다르지만 무언가는 있다)
#   - 표 구분자(|)와 숫자가 제목 안에 없을 것
#   - 30자 이내
_SUBSECTION_LINE = re.compile(
    r"^[ \t|]*\(?(?:\d{1,2}|[가-하]|[IVXivx]{1,4})[.)]\s*"
    r"([^\n|0-9]{2,30}?)\s*[ \t|]*$", re.M)

# 수직축에 쓰이는 절만 남긴다.
#   주요 제품 → 산업 분류와 매출 비중
#   원재료·생산 → 후방(무엇을 사 오는가)
#   매출·수주·시장 → 전방(누구에게 파는가)
# 위험관리·연구개발·지적재산권·환경·기타참고는 분량은 크지만 밸류체인 정보가 없다.
_KEEP = re.compile(r"사업의\s*개요|사업\s*개요|주요\s*제품|제품\s*및\s*서비스|"
                   r"원재료|생산설비|생산\s*및\s*설비|매출|수주|시장\s*점유|"
                   r"주요\s*계약|영업의\s*개황")
MIN_KEPT_CHARS = 800


def slim_business_section(section: str) -> tuple[str, list[str]]:
    """'사업의 내용'에서 밸류체인에 쓰이는 하위 절만 남긴다.

    사업보고서 한 건의 이 절은 13만 자에 달해, 통째로 넣으면 요청 하나가
    분당 토큰 한도를 넘겨 **아무리 재시도해도 통과하지 못한다**(실제로 5종목이
    전부 그렇게 실패했다). 앞에서부터 잘라내면 뒤쪽 '매출처'가 통째로 날아가는데,
    그게 전방 관계의 유일한 근거다. 그래서 자르지 말고 **고른다.**

    절 구성은 산업마다 다르다(건설사에는 '원재료'가 아예 없다). 하위 절을 하나도
    못 찾거나 남은 분량이 너무 적으면 원문을 그대로 돌려준다 — 잘못 걸러서
    빈손이 되느니 크게 넣는 편이 낫다.
    """
    marks = [(m.start(), m.group(1).strip())
             for m in _SUBSECTION_LINE.finditer(section)]
    marks = [(p, t) for p, t in marks if t]
    if len(marks) < 3:
        return section, []

    kept, titles = [], []
    for i, (pos, title) in enumerate(marks):
        if not _KEEP.search(title):
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(section)
        kept.append(section[pos:end])
        titles.append(title)

    slim = "\n".join(kept).strip()
    if len(slim) < MIN_KEPT_CHARS:
        return section, []
    return slim, titles


def fetch_business_section(ticker: str, corp_codes: dict[str, str],
                           max_chars: int = 60000, slim: bool = True) -> dict | None:
    """종목 하나의 '사업의 내용'과 출처 메타데이터."""
    corp = corp_codes.get(ticker)
    if not corp:
        return None
    report = find_business_report(corp)
    if not report:
        return None

    section = extract_business_section(fetch_document(report["rcept_no"]))
    if not section:
        return None

    full_len = len(section)
    kept_titles: list[str] = []
    if slim:
        section, kept_titles = slim_business_section(section)

    return {"ticker": ticker, "corp_code": corp, **report,
            "section": section[:max_chars],
            "truncated": len(section) > max_chars,
            "full_chars": full_len, "kept_chars": len(section),
            "kept_sections": kept_titles}


def retry(fn, *args, retries: int = 3, delay: float = 2.0, **kwargs):
    """DART는 간헐적으로 끊긴다. 마지막 시도까지 실패하면 예외를 올린다."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (requests.RequestException, DartError):
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))
