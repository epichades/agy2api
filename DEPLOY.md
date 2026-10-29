# VPS 部署、升級與私密設定

目前來源已整合上游 `6cc4562` 的 8 個提交及部署／相容性修正。此文件是操作指南；真實 VPS、OAuth、模型配額、長時間負載與高可用仍需另行驗收。證據見 [PROJECT_REVIEW.md](PROJECT_REVIEW.md)，API 範圍見 [RESPONSES_API.md](RESPONSES_API.md)，發布隱私見 [PRIVACY.md](PRIVACY.md)。

## 執行範圍與健康檢查

- 支援單程序；`python web.py` 與 app lifespan 明確拒絕 `WORKERS` 不為 1。不要另外用 Uvicorn CLI 的 `--workers` 繞過設定，OAuth callback 狀態不支援跨程序。Redis 不代表已實作多 worker／HA。
- DB／設定、缺少密碼、不安全狀態金鑰或已配置 Redis 初始化失敗時拒絕啟動。修好後重啟可再試，不會將失敗永久快取成成功。
- DB 設定每次讀取會查詢目前存儲，包含其他連線的更新／刪除；環境變數仍優先。
- `/health/ready` 只有啟動完成、實際 DB round trip 及已配置 Redis 可連線時回 200，否則 503；不顯示錯誤細節／帳號。它不驗證 OAuth、模型授權、配額或推論。
- 代理不需要本機 GPU；沒有壓測資料可承諾最低 RAM、併發或延遲。

## 發布與相依

使用經審查的發布分支；不可直接推送含私人祖先的舊分支。`git archive HEAD` 不含未提交修改，發布前確認程式、測試、dist 已全部納入。不要整份複製 `.git`、私密 `.env`、DB 或備份。

`pyproject.toml`＋`uv.lock` 鎖定相依，`.python-version` 指定 Python 3.13。`requirements.lock` 是相同 lock 匯出的 production 版本與 hashes，供 pip／Docker 使用。正式 pip 安裝加 `--require-hashes`；`requirements.txt` 只作下限宣告，不作可重現的正式安裝來源。

啟動腳本用 `uv sync --locked --no-dev`，不更新 Git，也不偷偷重新解析版本。manifest 改動後應重新產生／驗證／發布 lock；程序重啟不應執行更新。`restart.sh` 相對自身目錄啟動已安裝 venv；`loop.sh` 是需 Linux flock 的前景 supervisor、先建 data 再鎖定、log 送 stdout。有 systemd 時勿同時啟動 loop。

## 私有 `.env`

`.env.example` 可公開；實際 `.env` 權限 600，至少產生兩組獨立長密碼。以下拒絕覆寫既有檔案、不印出密碼：

```bash
cd /opt/agy2api
python3 - <<'PY'
import os
from pathlib import Path
import secrets
os.umask(0o077)
text = Path('.env.example').read_text()
for key in ('API_PASSWORD', 'PANEL_PASSWORD'):
    assert text.count(key + '=\n') == 1, key
    text = text.replace(key + '=\n', key + '=' + secrets.token_hex(32) + '\n', 1)
with Path('.env').open('x') as output:
    output.write(text)
Path('.env').chmod(0o600)
PY
```

| 設定／資料 | 要求 |
|---|---|
| API_PASSWORD | 給模型 API 用戶端的 Bearer／API key，不是 Google key |
| PANEL_PASSWORD | 面板管理用，與 API 密碼分開；面板可匯出憑證 |
| Google tokens／email／project ID、OAuth callback URL | 只在受保護面板處理；callback 含一次性 code／state |
| RESPONSES_STATE_KEY 或 responses-state.key | 伺服器獨立加密金鑰，與 DB 一併備份，不從 API password 衍生 |
| POSTGRESQL_URI／REDIS_URL／認證 PROXY | 帳密、私有主機只放私有環境；未用留空 |
| TLS／SSH／VPN、紀錄、對話與備份 | repo 外或明確忽略的位置，限制權限及備份存取 |

非空 process environment > 即時 DB 設定 > 預設；空值多半回退 DB，不代表清除。web.py 用 dotenv 載入且不覆寫既有 environment，不執行 `.env` shell 指令。HOST／PORT／WORKERS／金鑰等程序設定改動後重啟。勿公開 env、完整 Compose 展開或 container inspect environment。

## 原生 Python 3.13 + systemd

範例假設 Linux 已安裝 Python 3.13／venv、systemd，審閱後來源在 `/opt/agy2api`；使用者／路徑為通用範例。

```bash
sudo useradd --system --home-dir /opt/agy2api --shell /usr/sbin/nologin agy2api
sudo chown -R agy2api:agy2api /opt/agy2api
sudo install -d -o agy2api -g agy2api -m 700 /opt/agy2api/data/creds /opt/agy2api/data/request_logs
sudo -u agy2api python3.13 -m venv /opt/agy2api/.venv
sudo -u agy2api /opt/agy2api/.venv/bin/python -m pip install --require-hashes -r /opt/agy2api/requirements.lock
```

私有 `.env` 設 CREDENTIALS_DIR=/opt/agy2api/data/creds、HOST=127.0.0.1、WORKERS=1，檔案擁有者為服務使用者。`/etc/systemd/system/agy2api.service`：

```ini
[Unit]
Description=AGY2API proxy
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
User=agy2api
Group=agy2api
WorkingDirectory=/opt/agy2api
EnvironmentFile=/opt/agy2api/.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/agy2api/.venv/bin/python web.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agy2api.service
sudo systemctl status agy2api.service
curl --fail --silent http://127.0.0.1:7861/health/ready
```

## Docker 與既有資料搬移

目前 Compose 從此 checkout build，安裝 hash-pinned requirements.lock、env_file 注入私有 `.env`，UID/GID 10001 執行。容器內 HOST=0.0.0.0／PORT=7861；只發布宿主 loopback（宿主 PORT 可配置）。jemalloc 使用實際架構找到的路徑，不寫死 x86_64；不代表已做 amd64／arm64 runtime 驗收。

**新版 Compose 使用 named volume `credentials`、`request_logs`，不會自動搬移舊的 `./data/creds`。已有部署先停止舊服務並備份、搬移，完成前不要啟動空 volume。不要使用 down -v。**

以下假設舊資料在 `data/creds`；務必按原 CREDENTIALS_DIR／掛載位置調整。先用舊 Compose 設定停止，再改用新來源：

```bash
# 檔名是佔位，替換成舊部署真正使用的 Compose 檔。
docker compose -f /path/to/previous-compose.yml stop
sudo install -d -m 700 /var/backups/agy2api
sudo sh -c 'umask 077; tar -czf "/var/backups/agy2api/preupgrade-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" .env data/creds data/request_logs'
# 現在使用新原始碼與 Compose，create 不會啟動 app。
docker compose build --pull
docker compose create agy2api
migration_container=$(docker compose ps --all --quiet agy2api)
test -n "$migration_container"
docker cp data/creds/. "$migration_container:/app/creds/"
docker cp data/request_logs/. "$migration_container:/app/data/request_logs/"
docker compose run --rm --no-deps --user 0 --entrypoint sh agy2api -c \
  'chown -R 10001:10001 /app/creds /app/data/request_logs; chmod 700 /app/creds /app/data/request_logs; find /app/creds /app/data/request_logs -type f -exec chmod 600 {} +'
# 確認備份與搬移完成後才啟動。
docker compose up -d --no-build
```

舊部署沒有 request_logs 時省略相關 tar／copy 路徑。必須在所有舊寫入程序停止後複製；不要因新服務看不到帳號就直接重新授權或刪除舊資料。搬移後私下核對登入、帳號數與受控模型驗收，不公開 token／帳號。

全新安裝沒有舊資料，只需私有 `.env`，再 build 與 up。named volume 初次會繼承 image 內已設好擁有者的目錄；不將 DB 放進 image。`.dockerignore` 仍採逐檔 allowlist，不要放寬到涵蓋機密。

```bash
docker compose ps
docker compose logs --tail 80
curl --fail --silent http://127.0.0.1:7861/health/ready
docker image inspect agy2api-local:reviewed --format '{{.Id}}'
```

## HTTPS、SSE 與 OAuth

後端不直接發布公網；使用 HTTPS 反代／受控 VPN 與防火牆。管理面板可限制私人網路。Nginx HTTPS server 範例片段（證書及域名為佔位）：

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;
    ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    access_log off;
    location / {
        proxy_pass http://127.0.0.1:7861;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Gemini query key／OAuth code 可能在 URL；程式遮罩不取代代理日誌政策。SSE buffering、timeout、client cancellation 需驗證整條入口鏈。IP 白名單只信任自己的反代，啟用後也需允許本機 healthcheck 來源。PATH_PREFIX 只在代理保留前綴時配置。

受保護面板完成 Antigravity OAuth／匯入。遠端 VPS 的 redirect 預設指瀏覽器 localhost，可貼回該次完整 callback URL，或以 SSH tunnel 導向實際 callback 埠。起始 OAUTH_CALLBACK_PORT=11451，佔用會另選埠；無需公開 callback 埠範圍，也不可公開 URL。

先取得 `/v1/models` 再用精確模型 ID；帳號授權／額度須實測。Codex base URL 為 https://api.example.com/v1，Claude Code CLI 的 Messages base URL 為 https://api.example.com；能力界線見 Responses 文件。

## 狀態金鑰、Redis 與資料保留

agy2 replay state 使用伺服器私有金鑰認證加密。預設安全建立 CREDENTIALS_DIR/responses-state.key（600）、重啟讀同一檔；也可私下設定有效 Fernet key RESPONSES_STATE_KEY。不可用 API／panel 密碼代替，金鑰隨 DB／私有 env 一併備份。

遺失／輪替金鑰會使舊加密歷史無法回放，需新會話；舊可讀 agy1 封包直接拒絕，升級後這類對話也需重開。不要承諾舊會話無縫延續。預設不自動丟棄思考歷史；RESPONSES_ALLOW_HISTORY_FALLBACK=1 是操作者明確允許資訊降級。加密封包並不使原始對話、用戶端紀錄或上游留存變成可公開資料。

Redis 已列入標準相依；REDIS_URL 空值用記憶體，非空必須連線成功才能啟動。運行中失聯會使 readiness 不通過；本地後備不保證共享親和，仍只支援單 worker。

SQLite credentials.db 含明文 OAuth tokens、帳號、設定及選用 usage 紀錄。主檔初始化為 600；目錄 700、專用帳號與加密備份仍必要。replay 加密沒有加密整份 DB。原生停機備份：

```bash
sudo systemctl stop agy2api.service
sudo install -d -m 700 /var/backups/agy2api
sudo sh -c 'umask 077; tar -C /opt/agy2api -czf "/var/backups/agy2api/private-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" .env data/creds'
sudo systemctl start agy2api.service
```

Docker named volume 停機備份（也須另外私下保存 `.env`）：

```bash
docker compose stop agy2api
backup_container=$(docker compose ps --all --quiet agy2api)
test -n "$backup_container"
umask 077
mkdir -m 700 private-backup
docker cp "$backup_container:/app/creds/." private-backup/
# 複製成功並驗證後再啟動。
docker compose start agy2api
```

恢復同樣先停機、保存目前資料，再 copy 經驗證備份回同一 volume、設 UID/GID 10001 與 600／700 權限後驗收。運行中 SQLite 需用 backup API 等一致性方案，不能只複製主檔。PostgreSQL 另用其受控備份／還原流程。

舊 filename schema 保留原表，搬移 direct／nested token 欄位；可辨識 email 正常遷移，未知身份以合成身份停用保存。原表可供人工辨識／重匯入，勿直接刪除。這些停用記錄不能當成可用帳號。

ENABLE_LOG、USAGE_LOG_ENABLED、REQUEST_LOG_ENABLED 三者獨立。此範例關閉後兩者；文字 LOG_FILE 輪替不限制 usage DB／request_logs 容量，需另訂保留／清理政策。

## 上線驗收界線

離線測試之後，仍需在目標環境驗證：開機／重啟、secret 權限、既有資料升級、未授權請求、真實 OAuth／刷新、模型與額度、文字／工具／思考多輪、斷線／取消、反代 timeout、備份還原及負載。readiness 200 不代替這些驗收。amd64／arm64 Docker、PostgreSQL、真 Redis、VPS 只有執行證據才能標為通過；本文件的命令範例不是已完成遠端部署的宣稱。
