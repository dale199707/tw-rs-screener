# 強勢股雷達（tw-rs-screener）交接文件 v1

依「波の花園」網站的兩張截圖逆向重建的台股強勢股篩選頁。獨立專案，不依賴 tw-river。

## 架構

```
GitHub Actions（平日 18:00、21:00）
  scripts/fetch_daily.py  → data/daily/YYYYMMDD.json   每日行情 + 三大法人（保留最近 320 個交易日）
                          → data/meta.json             名稱 / 市場 / 產業（每 7 天更新）
  scripts/build.py        → data/screener.json         入榜股票與各欄指標
                          → data/kline.json            入榜股票近 200 日 OHLCV
GitHub Pages：index.html（單檔、純 JS、CDN 只有 lightweight-charts 4.2.3）
data/tags.json：手動維護的族群標籤（有填就取代官方產業別）
```

## 資料來源（依欄位「名稱」解析，不寫死順序）

| 用途 | 端點 |
|---|---|
| 上市行情 + 加權指數 | `twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json` |
| 上市三大法人 | `twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json` |
| 上櫃行情 | `tpex.org.tw/www/zh-tw/afterTrading/otc?date=YYYY/MM/DD&type=EW&response=json` |
| 上櫃三大法人 | `tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=YYYY/MM/DD&response=json` |
| 名稱 / 產業 | `isin.twse.com.tw/isin/C_public.jsp?strMode=2`（上市）、`=4`（上櫃），Big5，只取 CFI 為 ES 開頭的普通股 |

- 股票範圍：4 碼數字、非 0 開頭（排除 ETF、權證、特別股）。
- 找不到欄位會丟出錯誤並印出實際欄位，照著改 `col(...)` 的關鍵字即可。
- **v1 只用模擬資料測過**（開發環境連不到證交所）。第一次在 Actions 上跑時要看 log 確認四個來源都 `ok`。

## 欄位定義

| 欄位 | 定義 | 確定程度 |
|---|---|---|
| RS | 0.4×63日 + 0.2×126日 + 0.2×189日 + 0.2×252日 還原報酬，全市場排 1–99 | 推測（IBD 標準作法） |
| 比指數強 | 近 20 日還原報酬 − 加權指數報酬（pp） | 天數推測 |
| 離季線 | 收盤 ÷ MA60 − 1 | 確定 |
| 離年高 | 收盤 ÷ 250 日最高價 − 1 | 確定（兩張圖南亞年高都≈350.6） |
| 金流 | 近 20 日均成交值 ÷ 再往前 120 日均成交值 | 推測：兩天之間分母幾乎不變；東台 19.6x 超過「分母含近 20 日」的上限 12.5x，所以分母不含近期 |
| 法人 20 日 | 近 20 日外資 / 投信買賣超張數 × 當日收盤，只顯示買超 ≥ 1 億者 | 金額為估算（官方只給股數） |
| 日均 | 近 20 日均成交值（億） | 推測 |
| 成交值名次 | 當天成交值在上市＋上櫃普通股中的排名 | 確定 |
| 外資 / 投信當天 | 當日買賣超張數 × 收盤（億） | 估算 |

還原報酬：日報酬 = 漲跌 ÷ (收盤 − 漲跌)。官方「漲跌」以除權息參考價計算，所以連乘後自動排除除權息跳空。

## 篩選

- 入榜門檻（推測）：收盤 > MA60、日均 ≥ 3 億。依據：截圖每一檔離季線都是正的，最小日均 3.4 億。
- RS85：RS ≥ 85。
- 比大盤強（推測，最不確定）：比指數強 ≥ +10pp、RS ≥ 70、RS 線（還原價 ÷ 加權指數）在近 60 日高點 2% 以內。依據：南亞 RS99、+19pp 卻沒被標「兩種都符合」，所以不是單純比指數強 > 0。
- 全部 = RS85 ∪ 比大盤強；兩種都符合 = 交集，金線標示。

參數全在 `scripts/build.py` 的 `CFG`。

## 慣例

- JSON 單行精簡：`separators=(',',':')`、`ensure_ascii=False`。
- git：commit → `pull --rebase -X theirs` → push；只 `git add data/`，不用 `git add -A`。
- 前端改完用 node 檢查括號平衡。
- 紅漲綠跌；原站「+-6pp」的負號 bug 已修正。
