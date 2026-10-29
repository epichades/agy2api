# Responses API：支援範圍、回放狀態與用戶端設定

更新日期：2026-10-29。本文對應目前修正分支的實作；最終 commit、完整測試數字與實際 Codex probe 結果以 [PROJECT_REVIEW.md](PROJECT_REVIEW.md) 為準。部署與私密值管理另見 [DEPLOY.md](DEPLOY.md)、[PRIVACY.md](PRIVACY.md)。

這是 Antigravity 的 OpenAI Responses 相容轉接層。已修正已重現的工具名稱、型別、文字裁切與串流恢復問題，但不代表實現全部 OpenAI／Anthropic／Gemini API，也不保證所有上游語義都能原樣通過。無法實作的能力會拒絕，或明確回報轉換／忽略項目。

## 端點與基本契約

- `POST /v1/responses`，舊前綴 `/antigravity/v1/responses` 亦可使用。
- 認證使用 `Authorization: Bearer <API_PASSWORD>`；管理面板使用不同的 `PANEL_PASSWORD`。
- 資料路徑為 Responses input → 共用 Gemini converter／normalizer → 上游 stream → Responses items／typed SSE。客戶端 `stream=false` 亦聚合上游串流，以保留其中的思考、簽名與工具內容。
- 對話不存入伺服器 response store。客戶端每輪須回傳完整、未改動的必要輸出 items，連同工具結果；`store` 回應為 `false`。
- 預設使用精確模型 ID 選擇有權使用該模型的帳號；沒有該模型回 404，配額／冷卻限制回 429。不會為了成功自動換成另一個模型。`ALLOW_MODEL_REWRITE=1` 是明確允許改寫的例外，保真設定應保持 0。
- `/v1/models` 才是目前帳號可用模型的依據；文件、測試的模型名稱不代表部署者已取得該模型權限。

## 支援與保留行為

| 項目 | 現有契約 |
|---|---|
| 文字與輸出順序 | 保留支援的文字、空白、換行、`...`／`…`，以及思考 → 文字 → 工具等 items 的順序。只有有明確內部標記的 placeholder 會被濾除 |
| function／namespace | 回傳原本的工具名稱與 namespace；上游名稱必要時使用穩定散列後綴維持 64 字元限制，避免舊版單純截斷造成撞名。真正重複或平坦化後有歧義的宣告會拒絕。namespace description 會加入對應工具描述 |
| 工具參數與結果 | 保留 JSON 原始型別，不把字串 `"true"`、`"false"`、`"null"` 猜成 bool／null。工具歷史的畸形 JSON 直接回 400，不以空參數偷偷修補 |
| custom tools | 以上游接受一個 `input` 字串的函數承載，再回傳原字串；不再將特定字串變成另一段 JSON。grammar 不強制執行，會明確回報 |
| Claude thinking 與多工具 | 共用路徑保留所有工具宣告、真正的思考／簽名與工具歷史，不再只傳第一個工具、不再虛構思考塊。Responses 可利用認證加密狀態回放原文 |
| Gemini 簽名 | 已簽名的 reasoning 可用 `agy2.` 狀態回放；function-call 簽名以受保護的 call_id 承載，後續恢復原始上游 ID、名稱與簽名。一般文字 part 的簽名仍缺少通用 Responses 回放位置，會回報 `upstream.text_signature(not_replayable)` |
| SSE | 有 created／in_progress、output items、delta、completed／incomplete／failed 與 sequence_number；支援 UTF-8 分片。畸形資料會失敗，不會在後續 STOP 時偽裝成功，也不會繼續交付其後的工具事件 |
| 串流中斷與取消 | 已交付內容後不重新發出整個上游請求。失敗會終止為 failed；客戶端取消／關閉時釋放上游 iterator。尚未交付內容的重試仍受伺服器重試設定控制 |
| token 上限 | 尊重較小的請求值，最高為 64000。超過上限時回應 echo 有效的 64000，並明確回報 cap |
| 上游 metadata | 支援的 modelVersion、usage、grounding／citation／URL context、安全評分等保留於 `x_agy_upstream` 擴充欄位；可在送出 headers 前取得時，另回傳 `x-agy-upstream-model-version` |
| 圖片與多模態 | 輸入僅支援 user message 的 Base64 image data URL。圖片 detail 不生效，會回報；支援的上游圖片／程式執行內容會以 Markdown 呈現，並於擴充欄位保留結構及回報轉換 |

`prompt_cache_key` 用於衍生會話帳號親和 key，不是原生 prompt-cache retention 或命中保證。帶有已認證回放歷史時，歷史 owner 直接約束帳號選擇；程序重啟、記憶體綁定 TTL 過期或快取指向另一帳號，仍須找回原帳號，驗證成功後才更新綁定。原帳號已刪除、停用、冷卻、無該模型權限或無法取得憑證時回 `409 state_account_unavailable`，避免跨帳號傳送歷史；不會靠重試改用別的帳號。此保護同時適用原生 Messages 的加密回放。

`reasoning.effort` 目前接受但列為 ignored，不控制 Claude 的推理深度；深度取決於精確模型 ID 及上游模型政策。不要將此欄位的 echo 視為已實際生效。

## 不支援與需要檢查的回報

以下能力不會被靜默模擬：

- 伺服器對話狀態：`previous_response_id`、`conversation`、`background`、`prompt`、`context_management`、server compaction、`max_tool_calls`、item_reference。
- OpenAI 內建工具：web_search、file_search、code_interpreter、computer、image_generation、MCP、shell／local_shell、apply_patch、tool_search 等執行工具型別。
- 遠端／file_id 圖片、input_file、input_audio、非文字工具輸出，及上游不接受的 assistant prefill。
- Responses WebSocket、內建搜尋端點與原生 OpenAI 的所有服務層級／狀態能力。

Codex 把本機 MCP／Apps 功能宣告為一般 function／namespace，與 `tools[].type="mcp"` 這種 OpenAI 內建執行工具不同。前者可走工具轉換路徑，但不代表所有外掛或客戶端能力都已驗收。

未知的頂層及支援結構內的未知參數預設回 400。`RESPONSES_UNKNOWN_PARAMS=ignore` 只會接受並回報忽略項目，不能把缺少的能力變成可用。部分可接受但不完全生效的設定包括 effort、verbosity、strict、grammar、cache retention、truncation、服務層級、圖片 detail，以及轉譯過的 JSON Schema 約束。

用戶端應同時檢查：

| 位置 | 用途 |
|---|---|
| `x-agy-ignored-params` HTTP header | headers 送出前已知的忽略、schema 轉譯、上限或明確降級 |
| 最終 response 的 `x_agy_ignored_params` | 也包含串流期間才發現的轉換／降級，避免 headers 已送出後遺失警告 |
| `x_agy_upstream` | 支援的原始 metadata；它不是可直接當成原生 OpenAI annotations 的保證 |
| response status／error | 截斷、畸形 SSE、上游失敗或無法保存認證狀態，不會被當成 completed |

例如，developer 或對話中途的 system 訊息需要轉成上游可表示的形式、schema 需要改寫時，會明確列入回報。上游 metadata（含多模態結構）會遞迴移除 thought／signature／私有回放狀態欄位及思考區塊，只保留公開圖片／程式內容；以 `upstream.metadata_private_fields(omitted)` 或 `upstream.multimodal_private_fields(not_replayed)` 回報。秘密回放內容以認證加密狀態承載，不經 metadata 明文旁路輸出。

## `agy2.` 回放狀態與金鑰

目前 `reasoning.encrypted_content` 使用 **Fernet 認證加密**。狀態內的模型、帳號綁定、原始思考文字與簽名都受到完整性與保密性保護；不再使用舊版可解碼的 JSON＋zlib＋Base64 封裝。它仍是本服務專用狀態，不是 OpenAI 原生加密格式，也不能在不同代理之間通用。

金鑰只能來自下列伺服器擁有的來源：

| 來源 | 操作要求 |
|---|---|
| 私有 `RESPONSES_STATE_KEY` 環境變數 | Fernet 格式的 32-byte URL-safe Base64 key；不要填 API／panel 密碼，不要讓 API 使用者取得 |
| 未設定時的 `${CREDENTIALS_DIR}/responses-state.key` | 原子建立並保持穩定，檔案權限為 `0600`；不接受不安全權限、非私密所有權或符號連結 key 檔 |

金鑰不從 API_PASSWORD／PANEL_PASSWORD 衍生，也不在每次程序啟動時隨機更換。持久化 credentials 目錄或私有環境金鑰，並以和憑證同等嚴格的方式備份。金鑰遺失／輪替後，舊狀態不能回放，需重新開始對話；僅輪替 API 密碼不應破壞原金鑰。

- 舊 `agy1.`、遭竄改、非標準或外來狀態回 400；不做不安全的 legacy fallback。
- 模型不符或同一歷史混合不同 owner 回 400；所選帳號與認證歷史不符回 409，不暗中刪除歷史。
- 回放明文上限 4 MiB、編碼封包上限 6 MiB；不執行解壓縮。無法保存或使用金鑰時不宣稱成功回放。
- `reasoning.summary="none"` 不顯示思考原文；若要求 encrypted_content，客戶端可保存並原樣交回 ciphertext，但單憑 blob 或 API 密碼不能解出原文。
- 原始思考、signature、key、對話與客戶端紀錄仍不可放進 GitHub／issue。認證加密不是讓紀錄可任意公開的許可。

帶有上游工具簽名的 Responses call_id 會使用 `call_agy2_...` 包裝。客戶端必須原樣保存並用於 function_call_output／custom_tool_call_output，不要截短或自行重寫。伺服器驗證狀態與原工具名稱／參數後，恢復真正的上游 call ID；修改受簽名保護的工具歷史會回 400。

### 有損回退必須明確選擇

`RESPONSES_ALLOW_HISTORY_FALLBACK=0` 是預設。上游拒絕簽名時，直接返回該失敗，不自動清掉思考再重試。

只有設為 `1` 才允許在供應商拒絕簽章時，於同模型／同 owner 下移除回放思考，必要時關閉該輪 thinking。這是有損處理，header 與最終 response 會報告 `thinking_history_dropped` 或 `thinking_disabled_for_turn`。`RESPONSES_STRIP_SIG_ON_ACCOUNT_SWITCH` 的舊設定不能繞過已認證 owner 的帳號選擇約束；即使開啟 fallback，也不能在原帳號不可用時把這份歷史送往其他帳號。

## Codex 設定與驗證界線

長工具名稱與完整工具集的舊撞名問題已有回歸修正。真實 Codex CLI 的完整 Apps／外掛、文字與受簽名工具回合 probe 結果，以 [PROJECT_REVIEW.md](PROJECT_REVIEW.md) 的最終紀錄為準；不要沿用修正前「完整工具集一定 400」的舊結論，也不要把假上游 probe 當成真實 Claude／Google 模型驗收。

以下是使用者層級的私有 profile 範例，例如 `~/.codex/agy2api.config.toml`。Provider／認證設定不要放進專案 `.codex/config.toml` 或提交 GitHub。模型名稱請填入實際 `/v1/models` 清單中的精確 ID：

```toml
model_provider = "agy2api"
model = "<exact-model-id-from-v1-models>"
web_search = "disabled"

[model_providers.agy2api]
name = "AGY2API"
base_url = "https://api.example.invalid/v1"
env_key = "AGY2API_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
supports_standalone_web_search = false
stream_max_retries = 0
```

將網域改成自己的 HTTPS endpoint，並在私有本機環境提供 `AGY2API_API_KEY`，值為伺服器 API_PASSWORD。以 `codex --profile agy2api` 選擇該 profile；範本不會替使用者安裝或修改設定。沒有必要為了已修正的長名稱問題，常態停用 Apps／plugins；是否啟用個別工具仍應以實際驗證和需要為準。

`stream_max_retries=0` 是保守的用戶端設定，避免客戶端自行重開請求。伺服器已避免在交付資料後重送；工具執行者仍須處理中斷時結果未知的情況，不應在沒有確認副作用時自行再執行同一個工具。

官方依據：[Codex provider／profile 設定](https://learn.chatgpt.com/docs/config-file/config-advanced)、[設定參考](https://learn.chatgpt.com/docs/config-file/config-reference)、[Responses 狀態回放](https://developers.openai.com/api/docs/guides/migrate-to-responses)。官方協定描述與本服務實作能力須分開判讀。

## Claude 模型與原生 Messages 路徑

**Codex 經 Responses 呼叫 Claude 模型**走 `/v1/responses`；**Claude Code 原生閘道**走 `/v1/messages`。兩條路徑分別測試，不因其中一條通過就推論另一條完整相容。

原生 Messages 路徑本次也已修正：

- 保留有效 thinking 原文、signature、工具區塊的順序，以及工具 input 的 null／原始型別；保留多段文字工具結果與串流空白。
- 串流與非串流共享上游串流映射，非串流聚合思考、signature、工具結果，不依賴會省略資訊的上游 non-stream endpoint。
- 支援的內容出現畸形 SSE、缺失簽名或不完整終止時會失敗；取消時關閉上游，不用虛構的成功訊息收尾。
- `thinking.display=omitted` 會隱藏思考明文，使用相同伺服器私有狀態金鑰，把原文、上游簽章、有效模型與選定帳號認證加密至原生 `thinking.signature` 的 `agy2.` 狀態。後續必須保留空 `thinking` 與完整 signature，伺服器會恢復原文；不以空白思考假裝完成回放。
- 隱藏模式中的上游工具簽章另以 `call_agy2_` 的 `tool_use.id` 保存，綁定原呼叫 ID、工具名稱、參數、模型與帳號；`tool_result.tool_use_id` 必須原樣回傳。修改名稱／參數／密文或使用舊 `agy1` 狀態回 400；換帳號回 409，而且每次上游傳送前均驗證，包括重試。
- `context_management` 只接受空 `edits` 或 `clear_thinking_20251015` 配合 `keep="all"`，因為兩者都保留完整歷史；以 `context_management(no_op_keep_all)` 回報。真正清理、壓縮或工具結果移除仍回 400。`output_config.effort` 接受並回報 `output_config.effort(upstream_model_policy)`，推理深度仍由精確模型 ID 決定。
- 無法表示的 `redacted_thinking`、無法恢復原文的外部空 thinking＋原始 signature、不支援的 stateful／built-in 功能及其他 output_config 功能會回 400；部分 cache_control、strict、citations、beta／服務參數會回報忽略。
- `/v1/messages/count_tokens` 是估算，使用 `x-agy-token-count-estimated: true` 標示，不宣稱精確的原生 tokenizer 計數。

Claude Code CLI 基本連線範例：

```sh
export ANTHROPIC_BASE_URL=https://api.example.invalid
export ANTHROPIC_API_KEY="$AGY2API_API_KEY"
```

base URL 不加 `/v1/responses`；客戶端自行呼叫 `/v1/messages`。另外明確選擇可用模型 ID，避免預設小模型或 alias 不在帳號清單。這是 CLI 範本，不是 Claude Desktop 的完整設定指南。

已使用實際安裝的 **Claude Code 2.1.263**，經 loopback 原生路由與合成 Gemini 上游，驗證預設隱藏思考設定、完整預設函式工具集、文字回覆與一次無副作用的 Bash `printf` 工具回合。思考原文的空白、思考／工具簽章、參數中的 `false` 與工具結果均成功往返；HTTP 回應不含隱藏思考或工具簽章明文。可用 [tests/run_claude_compat_probe.py](tests/run_claude_compat_probe.py) 在隔離環境重現，執行不需真實帳號，也不修改 Claude Code 設定。

這是實際客戶端的合成上游協定驗證，尚未將真實 Claude Code 對話、所有 beta 功能或供應商簽名驗證當成已驗收。官方依據：[Claude Code gateway 協定](https://code.claude.com/docs/en/llm-gateway-protocol)、[連線設定](https://code.claude.com/docs/en/llm-gateway-connect)、[Thinking 回放要求](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Context editing 的 keep-all 語義](https://platform.claude.com/docs/en/build-with-claude/context-editing)、[Effort](https://platform.claude.com/docs/en/build-with-claude/effort)。

## 測試與發布門檻

- [tests/test_responses_audit.py](tests/test_responses_audit.py) 的 10 個舊缺陷案例已移除 xfail，作為正常回歸測試；新增 [test_responses_fixes.py](tests/test_responses_fixes.py) 覆蓋加密、綁定、工具回放、metadata、SSE 與釋放資源。
- [tests/responses/run_tests.py](tests/responses/run_tests.py) 的 15 個 golden／helper 合約已通過。Fixtures 的簽名與請求識別皆為合成值，不是有效供應商簽名的證據。
- 完整測試、真實 CLI 假上游 probe、Docker／VPS／真實模型驗收範圍，以及最後數字，以 [PROJECT_REVIEW.md](PROJECT_REVIEW.md) 為準。測試腳本不需真實帳號，應在隔離環境執行。
- 上線仍需驗證實際帳號的模型／配額、OAuth 刷新、長對話與工具副作用、反向代理 timeout、部署重啟及備份恢復。離線合約通過不等於真實上游或所有客戶端全功能通過。
