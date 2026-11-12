#!/usr/bin/env python3
"""讀 data/daily/*.json → 算指標、篩選 → 輸出 data/screener.json、data/kline.json

所有可調參數集中在 CFG。標「推測」的是從原網站截圖反推、無法確認的定義。
"""
import glob
import json
import os
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

CFG = {
    "LOOKBACK": 300,        # 讀最近幾個交易日
    # RS（IBD 式）：0.4×近3月 + 0.2×近6月 + 0.2×近9月 + 0.2×近12月 報酬，全市場排 1~99
    "RS_WINDOWS": [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)],
    "RS_MIN": 85,           # RS85 按鈕
    "EX_DAYS": 20,          # 比指數強：近 N 日個股報酬 − 加權指數報酬（pp）
    "MA_DAYS": 60,          # 季線
    "YH_DAYS": 250,         # 年高
    "AVG_DAYS": 20,         # 日均成交值
    "FLOW_BASE": 120,       # 金流（推測）：近20日均 ÷ 再往前 120 日均
    "MIN_AVG": 3.0,         # 入榜門檻（推測）：日均 ≥ 3 億
    "ABOVE_MA": True,       # 入榜門檻（推測）：收盤站上季線
    # 比大盤強（推測）：比指數強 ≥ +10pp，且 RS 線（個股/指數）在近 60 日高點 2% 以內
    "BEAT_PP": 10.0,
    "BEAT_LINE_DAYS": 60,
    "BEAT_LINE_TOL": 0.02,
    "BEAT_RS_MIN": 70,      # 比大盤強也要求 RS ≥ 70（原站截圖每檔 RS 都 ≥ 87）
    "INST_DAYS": 20,        # 法人 N 日累計
    "KLINE_DAYS": 200,      # 日 K 給前端幾天
}


def load_days():
    files = sorted(glob.glob(os.path.join(DATA, "daily", "*.json")))[-CFG["LOOKBACK"]:]
    days = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            days.append(json.load(f))
    return days


def load_json(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def nan_tail_mean(a, n, min_n=None):
    """每欄取最後 n 列的平均（忽略 NaN），有效筆數不足 min_n 給 NaN"""
    t = a[-n:]
    cnt = np.sum(~np.isnan(t), axis=0)
    with np.errstate(invalid="ignore"):
        m = np.nansum(t, axis=0) / cnt
    m[cnt < (min_n or n)] = np.nan
    return m


def main():
    days = load_days()
    if len(days) < 70:
        raise SystemExit(f"只有 {len(days)} 個交易日，至少需要 70 日（RS 完整需 253 日），請先回補")
    meta = load_json("meta.json", {})
    tags = load_json("tags.json", {})
    tags = {k: v for k, v in tags.items() if not k.startswith("_")}

    dates = [d["d"] for d in days]
    codes = sorted({c for d in days for c in d["s"]})
    if meta:
        codes = [c for c in codes if c in meta] or codes
    ci = {c: i for i, c in enumerate(codes)}
    T, N = len(days), len(codes)

    O, H, L, C, CHG, VOL, VAL, FN, TN = (np.full((T, N), np.nan) for _ in range(9))
    IDX = np.full(T, np.nan)
    for t, d in enumerate(days):
        if d.get("idx"):
            IDX[t] = d["idx"][0] if d["idx"][0] is not None else np.nan
        for c, row in d["s"].items():
            j = ci.get(c)
            if j is None:
                continue
            o, h, lo, cl, ch, v, val = row
            O[t, j], H[t, j], L[t, j], C[t, j] = (np.nan if x is None else x for x in (o, h, lo, cl))
            CHG[t, j] = np.nan if ch is None else ch
            VOL[t, j], VAL[t, j] = v, val
        for c, (fn, tn) in d.get("i", {}).items():
            j = ci.get(c)
            if j is not None:
                FN[t, j], TN[t, j] = fn, tn
    IDX = _ffill1(IDX)

    # ---- 還原報酬：用「漲跌」算日報酬（已排除除權息跳空），再連乘
    prevC = _ffill(np.vstack([np.full((1, N), np.nan), C[:-1]]))
    with np.errstate(invalid="ignore", divide="ignore"):
        ref = C - CHG
        r = np.where(~np.isnan(CHG) & (ref > 0), CHG / ref, C / prevC - 1)
    r = np.where(np.isnan(C), 0.0, r)
    r = np.where(np.isnan(r), 0.0, r)
    started = np.cumsum(~np.isnan(C), axis=0) > 0
    AP = np.where(started, np.cumprod(1 + r, axis=0), np.nan)

    def ret(n):
        if T <= n:
            return np.full(N, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            return AP[-1] / AP[-1 - n] - 1

    last = T - 1
    trade = ~np.isnan(C[last])

    # ---- RS
    score = np.zeros(N)
    wsum = np.zeros(N)
    for n, w in CFG["RS_WINDOWS"]:
        rn = ret(n)
        ok = ~np.isnan(rn)
        score[ok] += w * rn[ok]
        wsum[ok] += w
    first = ret(CFG["RS_WINDOWS"][0][0])
    valid = trade & ~np.isnan(first) & (wsum > 0)
    score = np.where(valid, score / np.where(wsum > 0, wsum, 1), np.nan)
    rs = np.full(N, np.nan)
    vi = np.where(valid)[0]
    if len(vi) > 1:
        order = np.argsort(np.argsort(score[vi]))  # 0..n-1
        rs[vi] = 1 + np.floor(98 * order / (len(vi) - 1))

    # ---- 比指數強
    n = CFG["EX_DAYS"]
    idx_ret = IDX[-1] / IDX[-1 - n] - 1 if T > n else np.nan
    ex = (ret(n) - idx_ret) * 100

    # ---- 季線、年高
    ma = nan_tail_mean(C, CFG["MA_DAYS"], int(CFG["MA_DAYS"] * 0.9))
    with np.errstate(invalid="ignore", divide="ignore"):
        dma = C[last] / ma - 1
        yh = np.nanmax(np.where(np.isnan(H), C, H)[-CFG["YH_DAYS"]:], axis=0)
        dyh = C[last] / yh - 1

    # ---- 成交值（萬元 → 億）
    V = VAL / 1e4
    a = CFG["AVG_DAYS"]
    avg = nan_tail_mean(V, a, int(a * 0.75))
    base = nan_tail_mean(V[:-a], CFG["FLOW_BASE"], 20) if T > a + 20 else np.full(N, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        flow = avg / base
    vtoday = V[last]
    rank = np.full(N, np.nan)
    ti = np.where(trade & ~np.isnan(vtoday))[0]
    rank[ti[np.argsort(-vtoday[ti])]] = np.arange(1, len(ti) + 1)

    # ---- 法人（張 × 收盤 → 億）
    Cf = _ffill(C)
    FA = np.nan_to_num(FN) * Cf / 1e5
    TA = np.nan_to_num(TN) * Cf / 1e5
    k = CFG["INST_DAYS"]
    f20, t20 = np.nansum(FA[-k:], axis=0), np.nansum(TA[-k:], axis=0)
    has_inst = bool(days[-1].get("i"))
    fd = FA[last] if has_inst else np.full(N, np.nan)
    td = TA[last] if has_inst else np.full(N, np.nan)

    # ---- 比大盤強（推測）
    with np.errstate(invalid="ignore", divide="ignore"):
        line = AP / IDX[:, None]
        lmax = np.nanmax(line[-CFG["BEAT_LINE_DAYS"]:], axis=0)
        near_high = line[last] >= lmax * (1 - CFG["BEAT_LINE_TOL"])
    beat = trade & (ex >= CFG["BEAT_PP"]) & near_high & (rs >= CFG["BEAT_RS_MIN"])

    # ---- 篩選
    base_ok = trade & (avg >= CFG["MIN_AVG"])
    if CFG["ABOVE_MA"]:
        base_ok &= C[last] > ma
    rsok = rs >= CFG["RS_MIN"]
    pick = base_ok & (rsok | beat)

    def rd(x, p=2):
        return None if x is None or np.isnan(x) else round(float(x), p)

    rows = []
    for j in np.where(pick)[0]:
        c = codes[j]
        m = meta.get(c, {})
        rows.append({
            "c": c, "n": m.get("n", c), "t": tags.get(c) or m.get("ind", ""), "m": m.get("m", ""),
            "cl": rd(C[last, j]), "rs": int(rs[j]) if not np.isnan(rs[j]) else None,
            "ex": rd(ex[j], 1), "ma": rd(dma[j] * 100, 1), "yh": rd(dyh[j] * 100, 1),
            "fl": rd(flow[j], 1), "f20": rd(f20[j], 1), "t20": rd(t20[j], 1),
            "av": rd(avg[j], 1), "v": rd(vtoday[j], 1), "rk": int(rank[j]) if not np.isnan(rank[j]) else None,
            "fd": rd(fd[j]), "td": rd(td[j]), "a": bool(rsok[j]), "b": bool(beat[j]),
        })
    rows.sort(key=lambda x: -(x["v"] or 0))

    out = {
        "date": dates[-1], "built": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)),
        "days": T, "universe": int(np.sum(valid)), "inst": has_inst,
        "idx": [rd(IDX[-1]), rd(idx_ret * 100, 1) if not np.isnan(idx_ret) else None],
        "cfg": CFG, "rows": rows,
    }
    _dump(out, os.path.join(DATA, "screener.json"))

    kd = CFG["KLINE_DAYS"]
    kl = {"d": dates[-kd:], "k": {}}
    for row in rows:
        j = ci[row["c"]]
        kl["k"][row["c"]] = [[_r(O[t, j]), _r(H[t, j]), _r(L[t, j]), _r(C[t, j]), _r(VOL[t, j], 0)]
                             for t in range(max(0, T - kd), T)]
    _dump(kl, os.path.join(DATA, "kline.json"))
    print(f"{dates[-1]}：{T} 日資料，RS 樣本 {int(np.sum(valid))} 檔，入榜 {len(rows)} 檔"
          f"（RS{CFG['RS_MIN']} {sum(r['a'] for r in rows)}、比大盤強 {sum(r['b'] for r in rows)}、"
          f"兩種都符合 {sum(r['a'] and r['b'] for r in rows)}）")


def _r(x, p=2):
    return None if np.isnan(x) else (int(x) if p == 0 else round(float(x), p))


def _ffill(a):
    a = a.copy()
    for t in range(1, len(a)):
        m = np.isnan(a[t])
        a[t, m] = a[t - 1, m]
    return a


def _ffill1(a):
    a = a.copy()
    for t in range(1, len(a)):
        if np.isnan(a[t]):
            a[t] = a[t - 1]
    return a


def _dump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
