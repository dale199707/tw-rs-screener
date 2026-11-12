# 強勢股雷達

台股強勢股篩選頁：RS、比指數強、離季線、離年高、金流、法人買超、成交值排名。每天盤後由 GitHub Actions 自動更新。

## 第一次部署

1. 建一個新的 GitHub repo，把整個資料夾推上去（包含 `.github/` 和 `.nojekyll`）。
2. Settings → Actions → General → Workflow permissions 選 **Read and write permissions**。
3. Settings → Pages → Source 選 **Deploy from a branch**，分支 `main`、資料夾 `/ (root)`。
4. Actions → daily-update → **Run workflow**，`backfill_days` 填 **400**（約 1 小時；中斷可再跑一次，已抓的日期會跳過）。
5. 跑完後打開 `https://<帳號>.github.io/<repo>/`。

之後平日 18:00 與 21:00 自動更新。

## 本機測試

```bash
pip install requests numpy
python scripts/fetch_daily.py --backfill 400   # 首次約 1 小時
python scripts/build.py
python -m http.server 8000                      # 開 http://localhost:8000
```

定義與推測依據見 `CLAUDE.md`；參數在 `scripts/build.py` 的 `CFG`。
