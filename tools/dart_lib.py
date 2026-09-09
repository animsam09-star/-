"""DART 보고서 직접 파싱 (dart.fss.or.kr). opendart OpenAPI는 샌드박스에서 차단됨.
어떤 숫자도 지어내지 않는다. 못 찾으면 None.
"""
import urllib.request, urllib.parse, re, html, time, os

UA = {'User-Agent': 'Mozilla/5.0'}
NBSP = '\xa0'
CACHE = 'cache'
os.makedirs(CACHE, exist_ok=True)


def _get(url, tries=6, timeout=40):
    last = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode('utf-8', 'replace')
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _cached(key, fn):
    p = os.path.join(CACHE, key)
    if os.path.exists(p):
        return open(p, encoding='utf-8').read()
    v = fn()
    open(p, 'w', encoding='utf-8').write(v)
    return v


def search_reports(corp_name, y0, y1):
    """연도별로 끊어서 정기보고서 목록 수집 -> {rcpNo: '보고서명 (YYYY.MM)'}"""
    out = {}
    base = 'https://dart.fss.or.kr/dsab007/detailSearch.ax'
    for yr in range(y0, y1 + 1):
        for ptype in ('A001', 'A002', 'A003'):
            data = urllib.parse.urlencode({
                'currentPage': 1, 'maxResults': 100, 'textCrpNm': corp_name,
                'startDate': f'{yr}0101', 'endDate': f'{yr}1231', 'publicType': ptype}).encode()
            h = ''
            for _ in range(5):
                try:
                    req = urllib.request.Request(base, data=data, headers={**UA, 'Content-Type': 'application/x-www-form-urlencoded'})
                    h = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')
                    break
                except Exception:
                    time.sleep(2)
            for m in re.finditer(r'main\.do\?rcpNo=(\d{14})"[^>]*>(.*?)</a>', h, re.S):
                nm = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', m.group(2))).strip()
                out.setdefault(m.group(1), nm)
    return out


def get_nodes(rcpNo):
    """보고서 문서 트리 노드 목록 (text + viewer params)."""
    h = _cached(f'main_{rcpNo}.html', lambda: _get(f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}'))
    blocks = re.findall(
        r"node\d+\['text'\]\s*=\s*\"([^\"]+)\";.*?"
        r"node\d+\['rcpNo'\]\s*=\s*\"(\d+)\";.*?"
        r"node\d+\['dcmNo'\]\s*=\s*\"(\d+)\";.*?"
        r"node\d+\['eleId'\]\s*=\s*\"(\d+)\";.*?"
        r"node\d+\['offset'\]\s*=\s*\"(\d+)\";.*?"
        r"node\d+\['length'\]\s*=\s*\"(\d+)\";.*?"
        r"node\d+\['dtd'\]\s*=\s*\"([^\"]+)\";", h, re.S)
    return [dict(text=t, rcpNo=r, dcmNo=d, eleId=e, offset=o, length=l, dtd=dtd)
            for (t, r, d, e, o, l, dtd) in blocks]


def fetch_section(n):
    url = (f"https://dart.fss.or.kr/report/viewer.do?rcpNo={n['rcpNo']}&dcmNo={n['dcmNo']}"
           f"&eleId={n['eleId']}&offset={n['offset']}&length={n['length']}&dtd={n['dtd']}")
    return _cached(f"sec_{n['rcpNo']}_{n['eleId']}_{n['offset']}.html", lambda: _get(url))


def find_node(nodes, *keywords, prefer=None):
    c = [n for n in nodes if all(k in n['text'] for k in keywords)]
    if not c:
        return None
    if prefer:
        for n in c:
            if prefer in n['text']:
                return n
    return c[0]


def to_num(s):
    """'150,850' / '(1,041)' / '-' -> float|None. 괄호=음수."""
    if s is None:
        return None
    s = str(s).strip().replace(',', '').replace(NBSP, '').replace(' ', '')
    if s in ('', '-', '—', '–', '△'):
        return None
    neg = (s.startswith('(') and s.endswith(')')) or s.startswith('△') or s.startswith('▲')
    s = s.strip('()').lstrip('△▲')
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def tables_text(section_html):
    """섹션 HTML -> [표][행][셀텍스트]"""
    out = []
    for tbl in re.findall(r'<table[^>]*>.*?</table>', section_html, re.S | re.I):
        rows = []
        for tr in re.findall(r'<tr[^>]*>.*?</tr>', tbl, re.S | re.I):
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S | re.I)
            cells = [html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c))).strip() for c in cells]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


def plain(section_html):
    return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', section_html)))


def pick_con_node(nodes):
    """연결 건설계약 주석 노드. '건설계약 수주현황(상세)'는 제외(사업의 내용 소속 별표)."""
    cands = [n for n in nodes if '건설계약' in n['text'] and '수주현황' not in n['text']]
    for n in cands:
        if '(연결)' in n['text']:
            return n
    if cands:
        return cands[0]
    return find_node(nodes, '연결재무제표 주석')


def pick_seg_node(nodes):
    cands = [n for n in nodes if '영업부문' in n['text']]
    for n in cands:
        if '(연결)' in n['text']:
            return n
    if cands:
        return cands[0]
    return find_node(nodes, '연결재무제표 주석')


def pick_biz_node(nodes):
    return find_node(nodes, '매출 및 수주상황') or find_node(nodes, '사업의 내용')

# ---------- 표 파싱 공용 헬퍼 (references/parsing-pitfalls.md 참조) ----------

def norm(s):
    """공백·NBSP 제거. 라벨/시그니처 비교 전에 반드시 통과시킬 것."""
    return str(s).replace(' ', '').replace(NBSP, '')


def flat_norm(t):
    """표 전체를 정규화된 한 줄로. 시그니처 판별용."""
    return norm(' '.join(' '.join(r) for r in t))


def split_label_vals(row):
    """선행 라벨 셀과 값 셀을 분리. '-'/빈칸은 None으로 위치 보존.

    숫자만 골라 담으면 값이 밀린다. 반드시 이 함수를 쓸 것.
    """
    i, labels = 0, []
    while i < len(row):
        c = row[i].strip()
        if to_num(c) is None and c not in ('', '-', '\u2013', '\u2014', NBSP):
            labels.append(c)
            i += 1
        else:
            break
    return labels, [to_num(c) for c in row[i:]]


def total_row(tbl, last=True):
    """합계 행 반환. 라벨 변형(합계/계/총 합 계/부문 합계/공종별 합계) 모두 인식.
    같은 표에 소계가 여럿이면 기본적으로 마지막 것을 총계로 본다."""
    found = None
    for r in tbl:
        if not r:
            continue
        f = norm(r[0])
        if f in ('합계', '계') or f.startswith('합') or f.startswith('총') or '합계' in f:
            found = r
            if not last:
                return r
    return found


def axis_of(tbl, axis_labels, alias=None):
    """표의 분류축과 열 이름 판별.

    axis_labels: 기대하는 축 라벨 리스트 (예: ['빌딩','토목','플랜트','조경'])
    alias:       동의어 매핑 (예: {'인프라': '토목'})
    반환: (열이름 리스트) — 판별 실패 시 []
    중복 '합계'는 첫 번째만 남긴다(뒤엣것은 보통 전기말).
    """
    alias = alias or {}
    for r in tbl:
        cn = [norm(c) for c in r]
        hits = [c for c in cn if c in axis_labels or c in alias]
        if len(hits) >= max(2, len(axis_labels) - 1):
            cols = []
            for c in cn:
                if c in axis_labels or c in alias:
                    cols.append(alias.get(c, c))
                elif c in ('계', '합계') or '합계' in c:
                    if '합계' not in cols:
                        cols.append('합계')
            return cols
    return []


def cumulative_index(tbl):
    """'3개월'/'누적'이 분리된 표면 1(누적), 아니면 0(첫 금액열)."""
    f = flat_norm(tbl)
    return 1 if ('누적' in f and '개월' in f) else 0


def pick_report_by_period(names):
    """{rcpNo: '보고서명 (YYYY.MM)'} -> {(연도, 분기): rcpNo}. 정정본 우선."""
    import re as _re
    per = {}
    for rcp, nm in names.items():
        m = _re.search(r'\((\d{4})\.(\d{2})\)', nm)
        if not m:
            continue
        y = int(m.group(1))
        q = {'03': 1, '06': 2, '09': 3, '12': 4}.get(m.group(2))
        if not q:
            continue
        key = (y, q)
        prev = per.get(key)
        if prev is None:
            per[key] = rcp
            continue
        prev_corr, cur_corr = '정정' in names[prev], '정정' in nm
        if cur_corr and not prev_corr:
            per[key] = rcp
        elif cur_corr and prev_corr:
            per[key] = max(prev, rcp)
    return per


def dart_url(rcpNo):
    return f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}'


# ---------- 라벨 매칭 (정확 일치 금지) ----------
# 행/열 라벨은 회사·연도마다 표기가 흔들린다. `lab in ('영업손익',)` 같은 정확 일치를 쓰면
# '영업이익(손실)' 하나 때문에 블록이 통째로 빈다. 반드시 아래 함수를 쓸 것.

LABEL_ALIASES = {
    # 표준명: 접두사 후보 (정규화 후 startswith 로 매칭)
    '순매출액': ('순매출액',),
    '외부매출액': ('외부매출액',),
    '내부거래': ('내부거래', '내부매출'),
    '매출액': ('매출액', '수익'),
    '영업손익': ('영업손익', '영업이익', '영업손실'),
    '매출원가': ('매출원가',),
    '매출총이익': ('매출총이익', '매출이익'),
    '판매관리비': ('판매비와관리비', '판매관리비', '판관비'),
    '총자산': ('총자산', '부문자산', '자산'),
    '총부채': ('총부채', '부문부채', '부채'),
    '수주총액': ('수주총액', '기본도급액'),
    '기납품액': ('기납품액', '완성공사액'),
    '수주잔고': ('수주잔고', '계약잔액'),
    '합계': ('합계', '계', '총계', '소계'),
}

# 라벨에서 떼어낼 군더더기
_LABEL_NOISE = ('(손실)', '(수익)', '(*)', '(주)', '(단위:백만원)')


def canon_label(raw, aliases=None):
    """행/열 라벨을 표준명으로 정규화. 못 찾으면 None.

    - 공백/NBSP 제거, (*1) 같은 각주 제거, '(손실)' 등 군더더기 제거
    - 정확 일치가 아니라 **접두사 매칭**
    """
    if raw is None:
        return None
    t = norm(raw)
    t = re.sub(r'\(\*+\d*\)', '', t)
    for n in _LABEL_NOISE:
        t = t.replace(norm(n), '')
    if not t:
        return None
    table = aliases or LABEL_ALIASES
    # 긴 후보부터 검사해야 '매출액'이 '순매출액'을 가로채지 않는다
    cands = []
    for std, pres in table.items():
        for p in pres:
            cands.append((len(p), p, std))
    for _, p, std in sorted(cands, reverse=True):
        if t.startswith(p):
            return std
    # 합계류는 앞에 수식어가 붙는다: '부문 합계', '공종별 합계', '기업 전체 총계'
    if '합계' in t or '총계' in t or t.endswith('소계') or t == '계':
        return '합계'
    return None


def check_completeness(dataset, blocks, label=''):
    """지표별로 '통째로 빈 기간'을 집계한다.

    내부 정합성 검사(부문합=합계)는 양쪽이 모두 비면 건너뛰므로
    블록 전체가 누락된 오류를 잡지 못한다. 추출 직후 반드시 이 함수를 돌릴 것.

    dataset : {기간: {블록키: {...}}}
    blocks  : [(블록키, 값이 있어야 하는 하위경로 리스트)] 예: [('segment', ['순매출액','순액'])]
    반환    : {지표명: [빈 기간, ...]}
    """
    out = {}
    for key, path in blocks:
        missing = []
        for period, rec in dataset.items():
            if not rec or not rec.get('rcpNo'):
                continue
            x = rec.get(key)
            for p in path:
                x = (x or {}).get(p) if isinstance(x, dict) else None
            if not x:
                missing.append(period)
        name = f"{key}." + ".".join(path) if path else key
        if missing:
            out[name] = missing
    if label:
        print(f'--- 결측 점검: {label} ---')
        if not out:
            print('   결측 없음')
        for k, v in out.items():
            print(f'   {k}: {len(v)}개 {v[:6]}')
    return out
