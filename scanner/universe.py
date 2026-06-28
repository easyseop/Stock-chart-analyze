"""종목 유니버스 — 대량 캐시/백테스트용 종목 리스트.

universe.csv (code,name,ccy) 를 쓰고 읽는다. 없으면 config.STOCKS로 폴백.
build()는 S&P500 + KOSPI 시가총액 상위로 자동 구성한다.
"""
from __future__ import annotations

import csv
import os

import config

DEFAULT_PATH = "universe.csv"
FIELDS = ["code", "name", "ccy"]


def load(path: str = DEFAULT_PATH) -> list[dict]:
    """유니버스 로드. 파일 없으면 config.STOCKS(워치리스트)로 폴백."""
    if not os.path.exists(path):
        return [s for s in config.STOCKS if s.get("code")]
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            if row.get("code"):
                out.append({"code": row["code"].strip(),
                            "name": (row.get("name") or row["code"]).strip(),
                            "ccy": (row.get("ccy") or "USD").strip()})
    return out


def save(rows: list[dict], path: str = DEFAULT_PATH) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def add_one(code: str, name: str | None = None, ccy: str | None = None,
            path: str = DEFAULT_PATH) -> bool:
    """티커를 유니버스에 영구 등록(중복이면 무시). 새로 추가했으면 True.

    즉석조회 종목이 캐시 리셋에도 사라지지 않도록 git 추적되는 universe.csv에 남긴다.
    """
    code = code.strip().upper()
    if not code:
        return False
    rows = load(path)
    if any(r.get("code", "").upper() == code for r in rows):
        return False
    if ccy is None:
        ccy = "KRW" if (len(code) == 6 and code[:5].isdigit()) else "USD"
    rows.append({"code": code, "name": (name or code), "ccy": ccy})
    save(rows, path)
    return True


def resolve_name(code: str, ccy: str | None = None) -> str | None:
    """티커의 실제 종목명을 best-effort로 조회(즉석조회를 이름으로 검색 가능하게).

    한국주(6자리): FinanceDataReader KRX 상장목록에서 매칭. 미국주: yfinance info.
    실패하면 None(호출측에서 코드명 폴백). 네트워크/예외 모두 무시.
    """
    code = code.strip().upper()
    if ccy is None:
        ccy = "KRW" if (len(code) == 6 and code[:5].isdigit()) else "USD"
    try:
        if ccy == "KRW":
            import FinanceDataReader as fdr
            df = fdr.StockListing("KRX")
            col = "Code" if "Code" in df.columns else df.columns[0]
            ncol = "Name" if "Name" in df.columns else None
            if ncol is not None:
                hit = df[df[col].astype(str).str.zfill(6) == code]
                if len(hit):
                    nm = str(hit.iloc[0][ncol]).strip()
                    return nm or None
        else:
            import yfinance as yf
            info = yf.Ticker(code).info or {}
            nm = (info.get("shortName") or info.get("longName") or "").strip()
            return nm or None
    except Exception:
        return None
    return None


def fix_names(path: str = DEFAULT_PATH) -> int:
    """name이 code와 같은(=이름 미확보) 행들의 실제 종목명을 채운다. 채운 개수 반환."""
    rows = load(path)
    fixed = 0
    for r in rows:
        code = str(r.get("code", "")).strip()
        if code and str(r.get("name", "")).strip().upper() == code.upper():
            nm = resolve_name(code, r.get("ccy"))
            if nm and nm.upper() != code.upper():
                r["name"] = nm
                fixed += 1
    if fixed:
        save(rows, path)
    return fixed


def add_krx_top(kospi_top: int = 200, kosdaq_top: int = 100,
                path: str = DEFAULT_PATH) -> int:
    """KOSPI/KOSDAQ 시총 상위 N종목을 기존 유니버스에 추가(미국주 보존·중복 제외).

    한국 우량주가 자동 수집 대상에 들어오도록 universe.csv에 영구 등록한다. 추가된 수 반환.
    """
    rows = load(path)
    seen = {str(r.get("code", "")).upper() for r in rows}
    before = len(rows)
    rows += _krx_rows("KOSPI", kospi_top, seen)
    rows += _krx_rows("KOSDAQ", kosdaq_top, seen)
    save(rows, path)
    return len(rows) - before


def _krx_rows(market: str, n_take: int, seen: set) -> list[dict]:
    """KOSPI/KOSDAQ 상장목록을 시총 내림차순으로(0=전체) rows 생성."""
    import FinanceDataReader as fdr
    kdf = fdr.StockListing(market)
    cap = next((c for c in ("Marcap", "MarketCap", "Amount") if c in kdf.columns), None)
    if cap:
        kdf = kdf.sort_values(cap, ascending=False)   # 큰 종목 먼저(백필 우선순위)
    codecol = "Code" if "Code" in kdf.columns else kdf.columns[0]
    out, taken = [], 0
    for _, r in kdf.iterrows():
        code = str(r.get(codecol, "")).strip().zfill(6)
        if len(code) == 6 and code[:5].isdigit() and code not in seen:
            seen.add(code)
            out.append({"code": code, "name": str(r.get("Name", code)), "ccy": "KRW"})
            taken += 1
            if n_take and taken >= n_take:
                break
    return out


def build(path: str = DEFAULT_PATH, sp500: bool = True,
          kospi_top=None, kosdaq_top=None) -> list[dict]:
    """유니버스 구성·저장. kospi_top/kosdaq_top: None=제외, 0=전체, N=시총상위N."""
    import FinanceDataReader as fdr
    rows: list[dict] = []
    seen: set = set()

    if sp500:
        df = fdr.StockListing("S&P500")
        for _, r in df.iterrows():
            code = str(r.get("Symbol", "")).strip()
            if code and code not in seen:
                seen.add(code)
                rows.append({"code": code, "name": str(r.get("Name", code)),
                             "ccy": "USD"})

    if kospi_top is not None:
        rows += _krx_rows("KOSPI", kospi_top, seen)
    if kosdaq_top is not None:
        rows += _krx_rows("KOSDAQ", kosdaq_top, seen)
    save(rows, path)
    return rows


# NASDAQ 공식 심볼 디렉터리(보통주 전체 — ETF/잡주 제외용 플래그 포함)
_NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
import re as _re
_SYM_RE = _re.compile(r"^[A-Z]{1,5}$")              # 보통주 티커(워런트·우선주 접미 제외)
_NAME_SKIP = _re.compile(
    r"\b(ETF|ETN|Fund|Trust|Warrant|Unit|Preferred|Depositary|Notes?|"
    r"Right|Acquisition|SPAC|Index)\b|%", _re.IGNORECASE)


def _fetch_symbol_file(url: str) -> list[list[str]]:
    import urllib.request
    data = urllib.request.urlopen(url, timeout=30).read().decode("latin-1")
    out = []
    for line in data.splitlines():
        if "|" in line and not line.startswith("File Creation"):
            out.append(line.split("|"))
    return out[1:] if out else []   # 첫 줄은 헤더


_NAME_TAIL = _re.compile(
    r"\s*-?\s*(Common Stock|Ordinary Shares?|Common Shares?|Class\s+[A-Z]\b.*|"
    r"American Depositary Shares?.*|Depositary Shares?.*|"
    r"\(.*?\))\s*$", _re.IGNORECASE)


def _clean_name(name: str, sym: str) -> str:
    """표시용 종목명 정리 — 'Common Stock'·'Class A'·괄호설명 등 꼬리표 제거."""
    nm = (name or sym).split(" - ")[0]
    prev = None
    while nm != prev:                       # 'Inc. Class A Common Stock'처럼 중첩 제거
        prev = nm
        nm = _NAME_TAIL.sub("", nm).strip().rstrip(",").strip()
    return (nm or sym)[:42]


def build_us_all(path: str = DEFAULT_PATH) -> list[dict]:
    """NASDAQ+NYSE 등 미국 보통주 전체 유니버스(ETF·테스트·잡주 제외)를 구성·저장.

    이후 백필로 전부 수집한 뒤 --prune 'illiquid:N' 으로 거래대금 상위 N만 남긴다.
    """
    rows: list[dict] = []
    seen: set = set()

    def add(sym, name, etf, test):
        sym = (sym or "").strip().upper()
        if etf == "Y" or test == "Y" or sym in seen:
            return
        if not _SYM_RE.match(sym) or _NAME_SKIP.search(name or ""):
            return
        seen.add(sym)
        rows.append({"code": sym, "name": _clean_name(name, sym), "ccy": "USD"})

    # nasdaqlisted: Symbol|Name|Market Cat|Test|Financial|RoundLot|ETF|NextShares
    for r in _fetch_symbol_file(_NASDAQ_LISTED):
        if len(r) >= 8:
            add(r[0], r[1], r[6], r[3])
    # otherlisted: ACT Symbol|Name|Exchange|CQS|ETF|RoundLot|Test|NASDAQ Symbol
    for r in _fetch_symbol_file(_OTHER_LISTED):
        if len(r) >= 7:
            add(r[0], r[1], r[4], r[6])

    save(rows, path)
    return rows
