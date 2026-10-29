# 私密設定與 GitHub 發佈界線

本文件記錄 2026-10-29 修正後的隱私行為。設定名稱與無效的範例值可以公開；真實帳號、密碼、資料庫、部署位置與對話內容只留在受控環境。`.env.example` 應維持可公開範本，實際值填入被忽略的 `.env` 或 VPS 的私密環境設定。部署步驟見 [DEPLOY.md](DEPLOY.md)，Responses 的限制見 [RESPONSES_API.md](RESPONSES_API.md)。

## 哪些內容可以公開

| 可以公開的內容 | 公開範例應如何寫 |
|---|---|
| `API_PASSWORD`、`PANEL_PASSWORD` 等設定名稱、用途、必填條件 | 空值或 `REPLACE_WITH_RANDOM_SECRET`，不要填入可用密碼 |
| 路由、預設埠、一般部署命令 | `127.0.0.1:7861`、`api.example.invalid`、`/opt/agy2api` 等通用範例 |
| 模型路由與相容性程式碼 | 使用 `/v1/models` 回傳的精確模型 ID；不包含自己的帳號清單或請求內容 |
| 測試與 API body 範例 | 完全合成的訊息、工具參數、假簽章與 `example.invalid` 帳號；不直接複製線上紀錄 |
| 程式原有的 OAuth client 常數 | 這些是上游 CLI 相容實作的公共 client 識別資料，與使用者的 access/refresh token 不同；不要把使用者自訂 client secret 放入原始碼 |

## 必須私有的內容與設定方式

| 類別 | 需要設定或保存什麼 | 保管方式與影響 |
|---|---|---|
| API 用戶端密碼 | `API_PASSWORD`，用於聊天、Gemini、Messages、Responses 端點 | 使用獨立隨機密碼，透過 Bearer header 傳送；它不是 Google API key，也不是 Google 帳號密碼 |
| 管理面板密碼 | `PANEL_PASSWORD`，可管理帳號、匯出憑證、修改設定與檢視紀錄 | 使用與 API 不同的隨機密碼；持有面板 session token 亦能取得管理權限，不分享瀏覽器 localStorage、完整 HAR 或登入截圖 |
| Google 帳號憑證 | OAuth `refresh_token`、`access_token`，以及帳號 email、project ID、到期時間 | 經受保護面板完成 OAuth 或匯入；不要貼進 issue、測試 fixture 或對話範例。Refresh token 可長期換取存取權，需特別保護 |
| OAuth 手動回呼 | 登入流程的 callback URL、authorization code、state | 只交回同一個受保護面板流程；不要儲存在 README、截圖、終端歷史或支援工單 |
| 本機資料庫 | `CREDENTIALS_DIR` 下的 `credentials.db`、SQLite WAL/SHM、舊資料遷移保留表 | 資料庫含明文憑證，面板保存的密碼與代理設定也可能在其中；使用專用服務帳號、限制目錄權限，備份另行加密 |
| Responses 狀態金鑰 | 伺服器專用 `RESPONSES_STATE_KEY`，或持久化的 `CREDENTIALS_DIR/responses-state.key` | 與 API／面板密碼獨立，不交給 API 用戶端；必須隨 DB／私有環境設定一起備份，移機或重建時恢復同一金鑰才能續用舊對話 |
| 加密回放資料 | `reasoning.encrypted_content` 與帶簽章狀態的 `call_agy2_` 工具 `call_id` | 雖已認證加密，仍是私密會話狀態；保護客戶端歷史、HTTP 偵錯紀錄與備份，不公開 blob 或金鑰 |
| PostgreSQL | `POSTGRESQL_URI` 包含資料庫帳密與主機 | 只放私密環境設定，空值代表使用 SQLite。資料庫 dump、連線字串與備份都不得公開 |
| 網路代理 | `PROXY` 可能包含 user/password；自訂 API 端點可能包含私密主機 | 只填入可信任的端點。`OAUTH_PROXY_URL` 的目的端會接收 OAuth token 交換資料；Antigravity 目的端會接收 Bearer token 與請求內容，不要任意使用第三方中轉 |
| 主機與操作資訊 | 真實 VPS IP、tailnet 名稱、私有域名、登入帳號、絕對個人路徑、SSH key | 實際資產清單放 repo 外，或放被忽略的 `*.private.md`；公開文件只保留通用範例 |
| 紀錄、對話與備份 | usage DB、詳細請求檔、輪替日誌、客戶端聊天歷史、匯出憑證、Git bundle | 視為私密資料；不要隨程式碼上傳 GitHub、容器 registry、issue 附件或公開雲端磁碟 |

目前這份工作區檢查未讀取任何真實 `.env` 或帳號資料庫；是否存在有效憑證、VPS 權限與備份加密狀態，需在實際部署環境另行確認。

## 設定優先順序與何時需要重啟

對 `config.py` 的一般設定讀取，優先順序是「程序環境變數 > 資料庫設定快取 > 程式預設值」。環境變數若已設定，面板中的資料庫值不能覆蓋它。空字串多數 getter 會視為未設定並回退，不應以空值當作停用開關。

`web.py` 的 dotenv 載入預設不覆寫已存在的程序環境變數；部署工具若另外 `source .env`，則以該工具實際載入的環境為準。不要將含密碼的 `env`、`printenv`、完整 Compose 展開結果貼到公開紀錄。

- 修改 `.env`、服務管理器的環境設定後，重啟服務以載入新值。
- `HOST`、`PORT`、`WORKERS` 是啟動設定；`LOG_FILE` 及其輪替 handler 在匯入時建立，變更後需重啟。Responses 功能旗標變更也建議在重新啟動後驗證。
- 面板儲存設定會重載目前程序快取；服務目前只支援 `WORKERS=1`，其他值會拒絕啟動。不要以外部 Uvicorn 多程序選項繞過這項部署限制。密碼或憑證政策變更後，重啟並確認環境變數是否仍鎖住舊值。
- 面板 session 簽署金鑰由面板密碼衍生。輪替面板密碼後需重新登入；這與 Responses 的獨立狀態金鑰不同。輪替 Google refresh token 需由 Google 帳號端撤銷舊授權，僅刪除本機檔案不等於撤銷。
- 變更 `RESPONSES_STATE_KEY`、金鑰檔或其持久化目錄時，需以受控停機／重啟及恢復程序處理；不要在服務運行中任意更換或刪除。輪替 API／面板密碼不會自動替換 Responses 狀態金鑰。

## 三種日誌開關各控制什麼

| 設定 | 目前預設與作用 | 關閉後仍可能存在什麼 |
|---|---|---|
| `ENABLE_LOG` | 預設 `1`，控制本專案共用 logger 的控制台與選用檔案輸出；`0` 停止該 logger 寫出 | 不會停用 usage DB、詳細請求紀錄、反向代理或客戶端紀錄；也不會刪除舊檔 |
| `USAGE_LOG_ENABLED` | 預設 `true`，寫入 usage table，含帳號 email、client IP、模型、時間、狀態與 token 計數 | 設成 `false` 只停止新 usage 紀錄，不會刪除先前資料、帳號憑證或詳細請求檔 |
| `REQUEST_LOG_ENABLED` | 預設 `false`；啟用後只在 `REQUEST_LOG_ERROR_CODES` 命中時寫入詳細 JSON（預設碼為 `400`） | 此功能與 usage 開關分開；`USAGE_LOG_ENABLED=false` 不代表詳細請求檔不會寫入 |

詳細請求檔位於 `data/request_logs/`，保存完整 payload、response body、帳號 email、URL 與錯誤訊息。程式只遮罩指定的認證 headers，**不會將整份 payload 或回應匿名化**。若因排錯短暫啟用，先使用合成資料，完成後關閉並清理產物與備份。

共用 logger 現在會遮罩 email、代理帳密、Bearer／部分 token，以及已知私密 query 名稱，例如 `key`、`code`、`state`；也會辨識經 URL 編碼的參數名稱。啟動時會將相同過濾套用至 Uvicorn 標準 logger 的控制台與檔案輸出，包含格式化參數及例外文字。這些是針對性過濾，不是完整資料去識別化：任意提示詞、原始碼、未知參數名稱與未接入的第三方 logger 仍需避免記錄。

Gemini 支援 `?key=` 認證；即使已修正程式內 query 遮罩，仍建議避免讓敏感 URL 進入日誌。`python web.py` 會停用 Uvicorn access log；若改用 Uvicorn CLI，應明確加入 `--no-access-log`。反向代理也應停用或清理 query/body/header 紀錄，不要記錄完整含 query 的 URL。程式內的 logger 過濾不會清理反向代理、客戶端或外部平台產生的紀錄。

`LOG_FILE` 建議放在受限的 `data/` 內或 repo 外；它的預設輪替容量為每檔 10 MiB、2 份備份，可由 `LOG_FILE_MAX_BYTES`／`LOG_FILE_BACKUPS` 調整。此輪替只適用該文字日誌，不限制 usage DB 或詳細請求檔容量。這兩類資料沒有自動到期清理保證，需另訂保留與刪除程序。

偏重隱私的部署可明確使用：

```dotenv
REQUEST_LOG_ENABLED=false
USAGE_LOG_ENABLED=false
ENABLE_LOG=1
LOG_LEVEL=warning
LOG_FILE=./data/agy2api.log
```

是否保留 warning 日誌由操作需求決定。這些開關只控制本服務的一部分紀錄，不能承諾 Google 上游、代理、VPS 平台或 Codex/Claude 用戶端不保存資料。

## Responses 的認證加密狀態與金鑰

目前 `src/responses/encrypted.py` 產生 `agy2.` 封包，使用伺服器專用 Fernet 金鑰認證加密 JSON，保留上游實際回傳的 thinking、thought signature、模型與帳號關聯。有效金鑰由 `RESPONSES_STATE_KEY` 提供時優先使用；它必須是 Fernet 的 32-byte URL-safe Base64 key，不是任意密碼字串。

未設定該環境變數時，程式會在程序環境的 `CREDENTIALS_DIR`（未設定時為 `./creds`）安全建立或讀取 `responses-state.key`。檔案需為服務使用者擁有的私有普通檔案，POSIX 權限不得開放給其他使用者；建立時為 `600`，目錄建議 `700`。現有檔案權限／擁有者不安全或金鑰格式錯誤會拒絕使用，不會當作 API 密碼回退。這個金鑰不從 `API_PASSWORD` 或 `PANEL_PASSWORD` 衍生；API 呼叫者不應持有它。

**狀態金鑰備份是對話連續性的必要條件。** 使用檔案模式時，必須持久化並備份整個正確的 credentials 目錄，包含 `responses-state.key`；使用環境模式時，必須另行安全備份該環境設定。移機、容器重建或資料還原時，只恢復 DB 而漏掉金鑰仍無法讀回原對話狀態。檔案遺失後自動建立的新金鑰也不能解開舊封包。輪替金鑰目前沒有多金鑰過渡機制，既有會話需重新開始。

舊 `agy1.` 使用可解碼的 zlib／base64，缺乏認證加密，現在不再接受；回傳舊版、被竄改、外來或無法以目前金鑰解開的 replay state 時，端點會明確回應 `400`，不會悄悄採用其中內容。升級前的可讀 blob 和其備份仍可能包含明文 thinking，應清理或繼續私密保管，不能因新版加密就公開舊產物。

`reasoning.summary=none` 會隱藏回應中的明文 reasoning summary；客戶端要求 `reasoning.encrypted_content` 時仍可收到加密回放封包，無法僅以 base64 解出 thinking。上游帶簽章的工具呼叫也會將認證加密狀態包進 `call_agy2_` 的工具 `call_id`，綁定原呼叫、函數名稱與參數雜湊；客戶端必須原樣回傳對應 ID，不能把它當成無敏感性的可公開追蹤編號。

原生 `/v1/messages` 的 `thinking.display=omitted` 也沿用同一獨立私有金鑰：空 `thinking` 的 `signature` 會承載 `agy2.` 加密原文／上游簽章，帶簽章的 `tool_use.id` 則使用 `call_agy2_`，不再以 `__thought__` 明文附加。模型、帳號及工具名稱／參數均驗證後才傳送上游，包含換帳號重試；不合法、竄改、舊版或跨模型狀態回 400，跨帳號回 409。客戶端仍須私密保存並原樣回傳這些欄位，備份／金鑰輪替要求與 Responses 相同。要求 `display=summarized` 時仍會回傳可見思考及既有原始簽章形式，不具有隱藏模式的明文輸出保護。

加密只保護這些 replay state，不會加密整份 SQLite、原始提示詞、明文工具參數／結果、已要求顯示的 reasoning summary 或其他客戶端歷史。它是本服務的狀態格式，不是 OpenAI 原生加密推理格式；切換服務、模型、帳號或金鑰仍受回放驗證限制。是否允許資訊降級另由 `RESPONSES_ALLOW_HISTORY_FALLBACK` 控制；完整語義見 Responses 文件。

## 私密歷史與乾淨發佈分支

2026-10-28 匯入的舊本機 `master`／原始 bundle 歷史，自 `6eed6a1` 開始的 `DEPLOY.md` 含真實部署主機／tailnet、帳號路徑與明文部署密碼。這些舊分支、bundle、stash 與備份 refs 仍只供本機恢復，不屬於發佈內容。更新目前文件不會移除其祖先資訊；直接推送舊分支仍會送出那些歷史。

`src/api/Response_example.txt` 是既有上游已追蹤的 body trace；本次發布快照將它移出 Git 追蹤，本機合成範例受 ignore 保護。本次未確認舊 trace 含有效個人 token，但不能把新增 ignore 當成取消追蹤或清除歷史；若要處理已公開歷史，仍需另行審查與協作者同步。

本次發佈流程採用已核對的上游 `6cc4562` 為乾淨基底，保留其 8 個上游更新，將**經審查的最終工作樹差異**集中為新的乾淨快照提交。依使用者要求，GitHub 原始碼分支為 `codex/agy2api-source`，保留遠端原有 Actions 設定並以 `[skip ci]` 提交；完整 CI 修改留在本機 `codex/agy2api-ready`。中間整合分支不作發佈。最終分支建立後，仍需核對其基底、提交祖先與實際檔案，確保包含必要的新檔、測試與前端資源，並且不以舊 bundle 的私密提交為祖先；不能只在舊歷史後面補一個刪除 commit。

只點名推送經審查的最終分支；不要公開原始 bundle，也不要使用 `git push --all` 或 `git push --mirror`。前者可能送出其他本機分支，後者還可能送出 stash 或備份 refs。若舊憑證已對外洩漏，建立乾淨分支不等於撤銷憑證，仍須在憑證來源端另行處理。

## 發佈前檢查：只輸出檔名與類別

可先執行本機 [publication preflight](scripts/check_publication.py)：`python3 scripts/check_publication.py --working-tree` 只檢查目前已追蹤／非 ignored 的候選檔；`python3 scripts/check_publication.py --base origin/master --ref HEAD` 會檢查全部待發佈祖先中的新內容，連後續已刪除的檔案也會擋下。它不讀私密路徑內容或真實 ignored 檔案，只輸出檔名／類別；退出碼 `0` 為通過、`1` 為需處理項目、`2` 為未完成檢查。這是固定規則的本機門檻，不能取代人工審查任意提示詞與個資。

以下命令不輸出設定值，也不讀取真實 `.env` 或憑證內容：

```bash
# 目前檔案狀態，以及尚未追蹤但可能被加入的檔案。
git status --short
git diff --name-only
git diff --cached --name-only
git ls-files --others --exclude-standard

# 已被追蹤、但依目前 ignore 規則原本應忽略的檔案。
git ls-files -ci --exclude-standard

# 確認私密範例路徑會被排除；只會列出命中的路徑。
git check-ignore --no-index .env .env.production creds/credentials.db \
  creds/responses-state.key data/creds/responses-state.key \
  data/request_logs/example.json app.log.1 log.txt.1 \
  backups/private.md operator.private.md agy2api.bundle deployment.tar.gz

# 最終乾淨快照相對本次上游基底影響哪些路徑；只能列檔，不能取代內容審查。
git diff --name-only 6cc4562...HEAD
git log --format= --name-only 6cc4562..HEAD -- \
  DEPLOY.md loop.sh restart.sh src/api/Response_example.txt

# 暫存區有沒有意外加入常見私密路徑；有輸出就停下檢查。
git diff --cached --name-only --diff-filter=ACM | \
  rg '(^|/)(\.env($|\.)|creds/|data/|backups?/)|\.(bundle|db|sqlite3?|pem|key|p12|pfx|log)(\.|$)|\.private\.md$'
```

最後一行在沒有命中時，`rg` 正常會以狀態碼 `1` 結束。清單檢查不會發現把密碼寫入一般 `.py`、README 或前端 bundle 的情況；這些公開候選檔案仍需人工或只回報檔名的 secret scanner 審查。不要將 scanner 的原始命中值貼到 issue。Git ignore 與 Docker ignore 互相獨立：目前 Docker 採逐檔 allowlist，新增 runtime 模組或公開資源時需一起確認打包清單。

本機可啟用版本內的 pre-push 檢查：`git config --local core.hooksPath .githooks`。它逐一檢查這次推送的 outgoing 歷史；新遠端 ref 以已核對的上游／origin 為基底。既有自訂 hooks 應先整合，勿直接覆蓋。它不會執行推送，也不能保證發現任意自由文字中的個資；不要以 `--no-verify` 或 `--mirror` 繞過發布檢查。
