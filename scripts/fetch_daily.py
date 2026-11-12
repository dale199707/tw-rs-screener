#!/usr/bin/env python3
"""抓證交所 / 櫃買中心每日行情 + 三大法人，存成 data/daily/YYYYMMDD.json

用法：
  python scripts/fetch_daily.py                 # 補最近 7 個日曆天（缺的或法人不完整的才抓）
  python scripts/fetch_daily.py --date 20261112 # 指定日期
  python scripts/fetch_daily.py --backfill 400  # 首次回補：往回 400 個日曆天
  python scripts/fetch_daily.py --start 20251001 --end 20251130  # 指定區間
  python scripts/fetch_daily.py --meta          # 強制更新 data/meta.json（名稱/市場/產業）

每日檔格式（單行精簡 JSON）：
  {"d":"20261112",
   "idx":[收盤, 漲跌],                         # 加權指數
   "s":{"2330":[開,高,低,收,漲跌,成交張數,成交值(萬元)], ...},
   "i":{"2330":[外資買賣超(張),投信買賣超(張)], ...},
   "ok":{"twse":1,"tpex":1,"t86":1,"tpexi":1}}

解析一律「依欄位名稱」找欄位，不寫死欄位順序；找不到就印出實際欄位並報錯，方便對照修正。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY = os.path.join(ROOT, "data", "daily")
META = os.path.join(ROOT, "data", "meta.json")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/html,*/*"}
SLEEP_TWSE = 3.5   # 證交所約 5 秒 3 次以內較安全
SLEEP_TPEX = 2.5
KEEP = 320         # data/daily 保留幾個交易日

_session = requests.Session()
_session.headers.update(UA)


# ---------------------------------------------------------------- 共用工具
def dump(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def get_json(url, params, sleep):
    """GET 並回傳 JSON；失敗重試 3 次。測試時可用 monkeypatch 換掉。"""
    last = None
    for k in range(3):
        try:
            r = _session.get(url, params=params, timeout=30)
            time.sleep(sleep)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
            time.sleep(sleep * (k + 2))
    raise RuntimeError(f"{url} {params} 失敗：{last}")


def get_text(url, encoding, sleep):
    r = _session.get(url, timeout=60)
    time.sleep(sleep)
    r.encoding = encoding
    return r.text


_TAG = re.compile(r"<[^>]+>")


def clean(s):
    return _TAG.sub("", str(s)).replace(",", "").replace("　", "").strip()


def num(s):
    """'1,234.5' → 1234.5；'--' / '' / 'X' 等 → None"""
    t = clean(s)
    if t in ("", "--", "---", "----", "X", "除權", "除息", "除權息", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def norm_fields(fields):
    return [re.sub(r"\s+", "", clean(f)) for f in fields]


def col(fields, *must, exclude=()):
    """回傳第一個『同時包含所有 must 字串、且不含 exclude』的欄位索引"""
    for i, f in enumerate(fields):
        if all(m in f for m in must) and not any(x in f for x in exclude):
            return i
    return None


def need(fields, idx, label):
    if idx is None:
        raise RuntimeError(f"找不到欄位「{label}」，實際欄位：{fields}")
    return idx


def iter_tables(js):
    """證交所/櫃買的 JSON 有時是 {tables:[...]}，有時 fields/data 在最上層"""
    if isinstance(js.get("tables"), list):
        for t in js["tables"]:
            if t and t.get("fields") is not None:
                yield t
    if js.get("fields") is not None and js.get("data") is not None:
        yield js


def is_stock_code(code):
    """一般股票：4 碼數字、不是 0 開頭（排除 ETF/權證/特別股等）"""
    return len(code) == 4 and code.isdigit() and code[0] != "0"


# ---------------------------------------------------------------- 證交所
def fetch_twse_quotes(ymd):
    js = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                  {"date": ymd, "type": "ALLBUT0999", "response": "json"}, SLEEP_TWSE)
    if str(js.get("stat", "")).upper() != "OK":
        return None, None  # 非交易日
    idx = None
    stocks = {}
    for t in iter_tables(js):
        f = norm_fields(t["fields"])
        if col(f, "證券代號") is not None and col(f, "收盤價") is not None:
            ic = need(f, col(f, "證券代號"), "證券代號")
            io, ih, il, iclose = (need(f, col(f, "開盤價"), "開盤價"), need(f, col(f, "最高價"), "最高價"),
                                  need(f, col(f, "最低價"), "最低價"), need(f, col(f, "收盤價"), "收盤價"))
            isign = need(f, col(f, "漲跌(+/-)"), "漲跌(+/-)")
            idiff = need(f, col(f, "漲跌價差"), "漲跌價差")
            ivol = need(f, col(f, "成交股數"), "成交股數")
            ival = need(f, col(f, "成交金額"), "成交金額")
            for row in t["data"]:
                code = clean(row[ic])
                if not is_stock_code(code):
                    continue
                c = num(row[iclose])
                if c is None:
                    continue
                sign = clean(row[isign])
                d = num(row[idiff])
                chg = None if d is None or sign in ("X",) else (-d if "-" in sign else d)
                vol = num(row[ivol]) or 0
                val = num(row[ival]) or 0
                stocks[code] = [num(row[io]), num(row[ih]), num(row[il]), c, chg,
                                int(round(vol / 1000)), int(round(val / 1e4))]
        elif col(f, "收盤指數") is not None and idx is None:
            iname = need(f, col(f, "指數"), "指數")
            iclose = col(f, "收盤指數")
            isign = col(f, "漲跌(+/-)")
            ipts = col(f, "漲跌點數")
            for row in t["data"]:
                if clean(row[iname]) == "發行量加權股價指數":
                    c = num(row[iclose])
                    d = num(row[ipts]) if ipts is not None else None
                    s = clean(row[isign]) if isign is not None else ""
                    idx = [c, (-d if "-" in s else d) if d is not None else None]
    if not stocks:
        raise RuntimeError(f"MI_INDEX {ymd} 沒解析到個股表，tables 標題："
                           f"{[t.get('title') for t in js.get('tables', [])]}")
    return stocks, idx


def fetch_twse_inst(ymd):
    js = get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                  {"date": ymd, "selectType": "ALLBUT0999", "response": "json"}, SLEEP_TWSE)
    if str(js.get("stat", "")).upper() != "OK":
        return None  # 尚未公布
    out = {}
    for t in iter_tables(js):
        f = norm_fields(t["fields"])
        ic = col(f, "證券代號")
        if ic is None:
            continue
        ifor = col(f, "外陸資買賣超股數", "不含外資自營商")
        if ifor is None:
            ifor = col(f, "外", "買賣超", exclude=("自營商買賣超股數(外資自營商)", "外資自營商"))
        need(f, ifor, "外陸資買賣超股數(不含外資自營商)")
        itr = need(f, col(f, "投信買賣超股數"), "投信買賣超股數")
        for row in t["data"]:
            code = clean(row[ic])
            if is_stock_code(code):
                out[code] = [int(round((num(row[ifor]) or 0) / 1000)),
                             int(round((num(row[itr]) or 0) / 1000))]
    return out


# ---------------------------------------------------------------- 櫃買中心
def _slash(ymd):
    return f"{ymd[:4]}/{ymd[4:6]}/{ymd[6:]}"


def fetch_tpex_quotes(ymd):
    js = get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                  {"date": _slash(ymd), "type": "EW", "response": "json"}, SLEEP_TPEX)
    stocks = {}
    for t in iter_tables(js):
        f = norm_fields(t["fields"])
        ic = col(f, "代號")
        iclose = col(f, "收盤")
        if ic is None or iclose is None:
            continue
        io, ih, il = need(f, col(f, "開盤"), "開盤"), need(f, col(f, "最高"), "最高"), need(f, col(f, "最低"), "最低")
        ichg = need(f, col(f, "漲跌", exclude=("漲停", "跌停")), "漲跌")
        ivol = need(f, col(f, "成交股數"), "成交股數")
        ival = need(f, col(f, "成交金額"), "成交金額")
        for row in t.get("data") or []:
            code = clean(row[ic])
            if not is_stock_code(code):
                continue
            c = num(row[iclose])
            if c is None:
                continue
            vol = num(row[ivol]) or 0
            val = num(row[ival]) or 0
            stocks[code] = [num(row[io]), num(row[ih]), num(row[il]), c, num(row[ichg]),
                            int(round(vol / 1000)), int(round(val / 1e4))]
    return stocks or None


def fetch_tpex_inst(ymd):
    js = get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                  {"type": "Daily", "sect": "EW", "date": _slash(ymd), "response": "json"}, SLEEP_TPEX)
    out = {}
    for t in iter_tables(js):
        f = norm_fields(t["fields"])
        ic = col(f, "代號")
        if ic is None:
            continue
        ifor = col(f, "外資及陸資", "不含外資自營商", "買賣超")
        if ifor is None:
            ifor = col(f, "外資", "買賣超", exclude=("外資自營商",))
        need(f, ifor, "外資及陸資(不含外資自營商)-買賣超股數")
        itr = need(f, col(f, "投信", "買賣超"), "投信-買賣超股數")
        for row in t.get("data") or []:
            code = clean(row[ic])
            if is_stock_code(code):
                out[code] = [int(round((num(row[ifor]) or 0) / 1000)),
                             int(round((num(row[itr]) or 0) / 1000))]
    return out or None


# ---------------------------------------------------------------- 名稱/產業
def fetch_meta():
    """從 ISIN 公開資訊頁抓名稱、市場、產業別（上市 strMode=2、上櫃 strMode=4）"""
    meta = {}
    for mode, mkt in (("2", "T"), ("4", "O")):
        html = get_text(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", "big5-hkscs", 2)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
            raw = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
            if len(raw) < 6:
                continue
            tds = [clean(x) for x in raw]
            first = _TAG.sub("", raw[0]).replace("　", " ").strip()   # 代號與名稱間是全形空白
            m = re.match(r"^(\d{4})\s+(.+)$", first)
            if not m or not is_stock_code(m.group(1)):
                continue
            cfi = tds[5] if len(tds) > 5 else ""
            if cfi and not cfi.startswith("ES"):   # 只要普通股
                continue
            meta[m.group(1)] = {"n": m.group(2).strip(), "m": mkt, "ind": tds[4]}
    if len(meta) < 300:
        raise RuntimeError(f"ISIN 只解析到 {len(meta)} 檔，格式可能變了")
    return meta


# ---------------------------------------------------------------- 主流程
def path_of(ymd):
    return os.path.join(DAILY, f"{ymd}.json")


def load(ymd):
    p = path_of(ymd)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def complete(rec):
    return rec and all(rec.get("ok", {}).get(k) for k in ("twse", "tpex", "t86", "tpexi"))


def fetch_day(ymd, force=False):
    rec = None if force else load(ymd)
    if complete(rec):
        return "skip"
    if rec is None:
        tw, idx = fetch_twse_quotes(ymd)
        if tw is None:
            return "closed"
        rec = {"d": ymd, "idx": idx, "s": dict(tw), "i": {}, "ok": {"twse": 1}}
    ok = rec["ok"]
    if not ok.get("tpex"):
        try:
            tp = fetch_tpex_quotes(ymd)
            if tp:
                rec["s"].update(tp)
                ok["tpex"] = 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! {ymd} 櫃買行情失敗：{e}")
    if not ok.get("t86"):
        try:
            ti = fetch_twse_inst(ymd)
            if ti:
                rec["i"].update(ti)
                ok["t86"] = 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! {ymd} 上市法人失敗：{e}")
    if not ok.get("tpexi"):
        try:
            oi = fetch_tpex_inst(ymd)
            if oi:
                rec["i"].update(oi)
                ok["tpexi"] = 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! {ymd} 上櫃法人失敗：{e}")
    dump(rec, path_of(ymd))
    return "ok" if complete(rec) else "partial"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--backfill", type=int, default=0, help="往回補幾個日曆天")
    ap.add_argument("--start", help="區間起日 YYYYMMDD（搭配 --end）")
    ap.add_argument("--end", help="區間迄日 YYYYMMDD")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--meta", action="store_true")
    a = ap.parse_args()

    today = dt.datetime.utcnow() + dt.timedelta(hours=8)  # 台北時間
    if a.date:
        days = [a.date]
    elif a.start:
        d0 = dt.datetime.strptime(a.start, "%Y%m%d")
        d1 = dt.datetime.strptime(a.end, "%Y%m%d") if a.end else today
        days = []
        while d0 <= d1:
            if d0.weekday() < 5:
                days.append(d0.strftime("%Y%m%d"))
            d0 += dt.timedelta(days=1)
    else:
        n = a.backfill or 7
        days = [(today - dt.timedelta(days=k)).strftime("%Y%m%d") for k in range(n)]
        days = [d for d in days if dt.datetime.strptime(d, "%Y%m%d").weekday() < 5]
        days.reverse()

    for ymd in days:
        st = fetch_day(ymd, a.force)
        if st != "skip":
            print(ymd, st, flush=True)

    # 只保留最近 KEEP 個交易日檔（build 只用 300 日）
    files = sorted(f for f in os.listdir(DAILY) if f.endswith(".json"))
    for f in files[:-KEEP]:
        os.remove(os.path.join(DAILY, f))

    # meta：每 7 天更新一次，或指定 --meta
    old = None
    if os.path.exists(META):
        with open(META, encoding="utf-8") as f:
            old = json.load(f)
    stale = old is None or (today - dt.datetime.strptime(old.get("_t", "20000101"), "%Y%m%d")).days >= 7
    if a.meta or stale:
        try:
            m = fetch_meta()
            m["_t"] = today.strftime("%Y%m%d")
            dump(m, META)
            print("meta", len(m) - 1)
        except Exception as e:  # noqa: BLE001
            print("! meta 更新失敗：", e)
            if old is None:
                sys.exit(1)


if __name__ == "__main__":
    main()
