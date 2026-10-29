# 上游整合與待辦修正完成報告

完成日期：2026-10-29（Asia/Taipei）。本次完成前次評估列出的程式、部署設定、相容性、隱私與發布整理待辦；原有修改已保留。後續依使用者要求將原始碼上傳 GitHub，保留遠端既有 Actions 設定，提交加上 `[skip ci]`。沒有部署 VPS 或使用真實模型帳號。下列「通過」均限定所列測試範圍，不等於已驗收遠端供應商或正式服務。

## 上游 8 個提交：全數納入

已核對上游 master 為 `6cc45620f1ecbe12bda7792b2daf8940d8a4ffde`，相對原 fork 基底 `ace4b2c` 共 8 個提交。4 個實質修正均適用本專案；其餘合併／版本紀錄一併保留，沒有需要排除的模型路由或隱私變更。

| 提交 | 納入理由 |
|---|---|
| [9a78733](https://github.com/su-kaka/agy2api/commit/9a78733) | 錯誤碼輸入欄位保留編輯中的原始文字，修正無法輸入多個數值；與本機設定隱私介面整合 |
| [db7bee6](https://github.com/su-kaka/agy2api/commit/db7bee6) | 詳細日誌複製在非 secure context 有 fallback，且回饋留在彈窗內 |
| [8e37d7d](https://github.com/su-kaka/agy2api/commit/8e37d7d) | 調整複製結果提示位置，保留前一修正的可操作性 |
| [c84f6bb](https://github.com/su-kaka/agy2api/commit/c84f6bb) | refresh／存儲前將 expiry 序列化成字串，避免 datetime 重用及 PostgreSQL/asyncpg 類型問題 |
| [f2009a6](https://github.com/su-kaka/agy2api/commit/f2009a6) | 保留上游 expiry 修正的合併歷史 |
| [e3508c4](https://github.com/su-kaka/agy2api/commit/e3508c4) | 保留對應版本紀錄 |
| [07ec1aa](https://github.com/su-kaka/agy2api/commit/07ec1aa) | 保留設定頁修正的上游合併歷史 |
| [6cc4562](https://github.com/su-kaka/agy2api/commit/6cc4562) | 保留上游目前版本紀錄，作為乾淨發布基底 |

前端由整合後來源重新建置；沒有將上游 dist 直接覆蓋本機隱私修正。原始碼交付使用 `codex/agy2api-source`：初始乾淨快照 `46641c9` 直接以 `6cc4562` 為 parent，後續修正承接該快照；`.github/workflows` 保持與遠端原有版本一致，不新增 CI workflow，也不建立 PR 或 tag 觸發建置。CI 修改另保留於本機 `codex/agy2api-ready`，該分支未包含本節之後的修正。舊 master、原 bundle／備份 refs 與中間整合分支不推送；它們保留在本機供回復，不能使用 `push --mirror` 發布。

此方式保留全部上游血緣，同時避免讓含舊私人部署筆記的本機 5 個提交成為發布祖先。既有公開上游歷史不在本次重寫範圍；沒有執行遠端歷史重寫或憑證撤銷。`src/api/Response_example.txt` 已取消追蹤，僅作被忽略的本機檔保留，不納入發布快照。

## 修正完成矩陣

| 範圍 | 完成行為 | 驗證／來源 |
|---|---|---|
| 部署版本與相依 | Compose 從目前來源 build、env_file 注入、非 root、loopback port、持久化 volume；uv／pip hash lock、架構對應 jemalloc、相對路徑 startup scripts | [DEPLOY.md](DEPLOY.md)、[deployment regressions](tests/test_deployment_regressions.py)、Shell／lock／相依檢查 |
| 啟動與資料庫 | DB／設定失敗可重試並拒絕半就緒啟動；readiness 實際查 DB／配置 Redis；設定更新／刪除不再被舊 process cache 遮蔽 | [config.py](config.py)、[web.py](web.py)、[storage](src/storage/database.py)、真實原生程序 smoke |
| 憑證保存 | expiry 字串修正；舊表保留、direct／nested token 遷移；未知身份以停用記錄保留，原始資料仍可恢復 | [legacy migration](src/storage/legacy_migration.py)、[stability regressions](tests/test_stability_audit.py) |
| Responses 工具 | 長 namespace 名稱採可區分的穩定映射；工具／custom 字串 true/false/null 不再被猜測轉型；call_id、簽章及回放關係驗證 | [Responses regressions](tests/test_responses_audit.py)、[extended cases](tests/test_responses_fixes.py)、真實 Codex 客戶端工具循環 |
| 文字與推理保真 | 保留已支援文字／簽名的原始空白、正常省略號及順序；處理 Gemini／Claude 簽章狀態；不支援語義明確拒絕／回報，output cap 與 metadata 一致 | Responses／converter 回歸與 golden 合約 |
| 串流與取消 | 已交付內容後不再重送整段請求；重試遵守設定及交付狀態；malformed SSE／中途錯誤明確失敗；取消後清理 iterator | [stream recovery](tests/test_stream_recovery.py)、Responses 回歸 |
| 原生 Claude Messages | 保留多段 tool result、文字空白及思考／簽章回放；處理 signature-only／omitted thinking 與工具循環；不支援的原生能力明確處理 | 原生 converter 測試、真實 Claude Code 客戶端假上游循環 |
| 推理狀態安全 | 由公開可讀 agy1 改為伺服器獨立金鑰的認證加密 agy2；模型／帳號／工具狀態綁定，限制輸入大小，拒絕竄改／舊格式 | [encrypted state](src/responses/encrypted.py)、安全及邊界回歸 |
| 機密輸出與發布 | 共用及標準 logger 遮罩認證／query 機密；私密檔排除與 Docker allowlist；檢查待發布檔案及祖先；乾淨快照不包含私人提交歷史 | [PRIVACY.md](PRIVACY.md)、相關隱私／打包回歸及 diff 檢查 |

已修復的案例已轉成一般 regression，沒有保留 xfail 來遮掩失敗；原先「完整 Codex 工具集一定 400」「工具字串會變型」「輸出後會重送」等結論不再適用目前修正版。

## 2026-10-29 後續相容性與操作修正

- 修正 `46641c9` 的 Chat Completions 嚴格模型造成常見 SDK 欄位 422 的回歸。現在接受常見擴充，維持核心型別驗證與上游欄位白名單，未套用選項只回報名稱；47 項 ASGI／串流案例涵蓋 `null`、未知欄位、usage、UTF-8 分片、取消及錯誤。`include_usage` 另發標準結尾事件，Chat 抗截斷按每個上游 attempt 的最後累積計數加總，缺漏明確標為不完整。
- 已認證歷史 owner 直接約束帳號選取；重啟、TTL 過期或錯誤 cache 都不會改用別的帳號。驗證通過後才更新綁定；原帳號不可用回 409。31 項回歸涵蓋 Responses、原生 Claude、重試及 callback 失敗；獨立複查相關 55 項通過。
- `loop.sh` 使用受管 supervisor；`restart.sh` 對受管程序送出限時重啟，`stop` 只停止自己的 child process group。備份工具包含完整專案、實際憑證目錄、指定私密環境檔、SQLite 一致性快照及 checksum，還原只寫入新 staging。這些工具以合成程序與資料驗證，尚不代表既有目標主機已完成備份／切換。
- `/version/info` 增加程序啟動時的 build commit／來源／dirty；無 Git 的產物可指定 `AGY_BUILD_SHA`，不再以舊 `version.txt` 冒充目前部署版本。
- 選用 `LOG_ACCOUNT_TAG_KEY` 以獨立私密 HMAC 金鑰產生帳號代號，保留 email 遮罩；管理腳本登入取得八小時 token 的遷移方式已補入 DEPLOY，模型 API 認證維持分離。

## 實際客戶端與原生程序證據

所有探針使用合成 key、隔離暫存狀態與 loopback 假上游；沒有真實 Google／Claude 推論，也沒有公開原始提示詞、token、JWT、金鑰或 callback URL。

| 驗證 | 實際結果與界線 |
|---|---|
| 完整離線測試 | [run_offline_checks.py](tests/run_offline_checks.py)：**331 passed、無 xfail**；1 個 Starlette 相依棄用警告。程序生命週期案例需要允許本機私有 Unix socket |
| Responses golden／helper 合約 | **15/15 passed**；只驗證已列出的轉換與事件契約 |
| Codex CLI 0.155.1 | 保留完整 Apps／plugins 工具集，Claude 及 Gemini 路徑均完成假上游簽章工具循環；各工具循環均為兩次 HTTP 200、CLI exit 0；長工具名、合成工具結果及回放簽章通過。這是實際客戶端傳輸／工具契約驗證，不是模型品質或真實授權驗收 |
| Claude Code 2.1.263 | 原生 Messages 路徑通過預設 omitted thinking 文字與簽章 Bash 工具循環；opaque 回放成功且 omitted 模式未明文暴露思考／簽章，對端仍是假上游 |
| 原生 HTTP 程序 | [run_runtime_smoke.py](tests/run_runtime_smoke.py)：**17 項 outcome 全真、exit 0**。實際啟動 listener，驗證 readiness、未授權拒絕、panel 登入、API key 不能管理面板、HTTP 設定保存、SQLite／金鑰重啟持續性、缺密碼啟動失敗及所有子程序清理 |
| 原始碼／發布檢查 | `git diff --check` 與工作樹私密預檢通過；前端 build、Shell 語法、lock 與 pip 相依檢查通過。中間分支的歷史門檻預期阻擋舊 fixture 資料，因此不發布該分支，只交付直接承接上游的乾淨快照 |

可重跑入口為 [Codex probe](tests/responses/run_codex_compat_probe.py)、[Claude Code probe](tests/run_claude_compat_probe.py) 及原生 smoke。實際 client 需本機安裝相應版本與 loopback 權限；工具探針只執行受控合成命令，不能拿來宣稱所有 client 版本或所有外掛均已驗收。

## 必須維持的設定與升級規則

- `WORKERS=1` 是明確支援界線；原生入口／lifespan 拒絕其他值。設定讀取同步修正與 Redis 依賴補齊，不等於 OAuth、跨實例或高可用已完成驗收。
- `API_PASSWORD`、`PANEL_PASSWORD` 使用不同的私密隨機值；實際值留在 `.env`／部署秘密管理，不提交到 GitHub。預設精確模型 ID，不暗中換模型。
- 保護並備份 `CREDENTIALS_DIR/responses-state.key`；也可私下指定有效 `RESPONSES_STATE_KEY`。此金鑰與 API／panel 密碼分離，重啟沿用同一份。
- 如啟用 `LOG_ACCOUNT_TAG_KEY`，另行備份這份獨立私密設定；未啟用時日誌仍使用通用 email 遮罩。
- agy1 不再接受；舊格式對話需重新開始。金鑰遺失／輪替也會中斷舊加密回放，不承諾無縫延續。
- 預設不丟棄思考歷史來恢復成功；`RESPONSES_ALLOW_HISTORY_FALLBACK=1` 是操作者明確允許降級，不屬無損模式。
- Compose 的新 named volumes 不自動搬移舊 bind 資料；先停機、備份、copy、修正權限並核對，再啟動。步驟見 DEPLOY；不使用 `down -v`。
- 診斷、usage DB、完整 request log 各自控制。狀態加密沒有加密整份憑證 DB，也不保證上游／客戶端不留存；帳號、原始對話、備份仍屬私密資料。

## 尚未驗證與能力界線

以下屬實際環境驗收，沒有執行證據，不能標為通過：Docker build／container runtime（含 amd64／arm64）、目標 VPS 與 systemd、真實 Redis 服務、PostgreSQL、Google OAuth／token 刷新與模型配額、真實 Claude／Gemini 推論、長時間負載、容量、反代全鏈 SSE timeout／取消及正式備份還原。

readiness 200 只證明本機服務與所配置存儲可用，不代表上述上游能力。此版本仍是明確能力集合的協定轉接器；OpenAI 內建工具、伺服器端對話儲存／compaction、原生網路搜尋與其他未實作能力不能因端點名稱相同就視為支援。詳細保留、拒絕及降級契約見 RESPONSES_API。GitHub 交付只含原始碼分支，不建立遠端發布或對 VPS 套用設定。

## 外部依據

官方文件用來判斷協定要求，是否完成仍以本機程式及上述測試範圍為準：

- [Codex provider 設定](https://learn.chatgpt.com/docs/config-file/config-advanced)、[Codex 設定參考](https://learn.chatgpt.com/docs/config-file/config-reference)。
- [OpenAI Responses 遷移與狀態回放](https://developers.openai.com/api/docs/guides/migrate-to-responses)。
- [Claude Code gateway 協定](https://code.claude.com/docs/en/llm-gateway-protocol)、[Claude thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)。
