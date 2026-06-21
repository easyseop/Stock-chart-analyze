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


def build(path: str = DEFAULT_PATH, sp500: bool = True,
          kospi_top: int = 200) -> list[dict]:
    """S&P500 + KOSPI 시총 상위 kospi_top 종목으로 유니버스 구성·저장."""
    import FinanceDataReader as fdr
    rows: list[dict] = []
    seen = set()

    if sp500:
        df = fdr.StockListing("S&P500")
        for _, r in df.iterrows():
            code = str(r.get("Symbol", "")).strip()
            if code and code not in seen:
                seen.add(code)
                rows.append({"code": code, "name": str(r.get("Name", code)),
                             "ccy": "USD"})

    if kospi_top:
        kdf = fdr.StockListing("KOSPI")
        # 시가총액 컬럼이 있으면 상위로 정렬
        capcol = next((c for c in ("Marcap", "MarketCap", "Amount") if c in kdf.columns), None)
        if capcol:
            kdf = kdf.sort_values(capcol, ascending=False)
        codecol = "Code" if "Code" in kdf.columns else kdf.columns[0]
        taken = 0
        for _, r in kdf.iterrows():
            code = str(r.get(codecol, "")).strip().zfill(6)
            if len(code) == 6 and code[:5].isdigit() and code not in seen:
                seen.add(code)
                rows.append({"code": code, "name": str(r.get("Name", code)),
                             "ccy": "KRW"})
                taken += 1
                if taken >= kospi_top:
                    break

    save(rows, path)
    return rows
