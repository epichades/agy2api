"""
[local patch] 旧版 (gcli2api, filename 键) antigravity_credentials 表 -> 新版 (email 键) 迁移。

上游在检测到旧结构时直接 DROP TABLE（账号全部丢失）。本地改为:
1. 旧表 RENAME 为 antigravity_credentials_legacy_<ts>（保留原始数据，不删除）
2. 为新 ORM 释放旧索引名；PostgreSQL 只改名，保留 UNIQUE 约束的依赖
3. 新表建好后，按 user_email 把账号逐行迁移到新表（token/refresh_token/project_id/状态）
"""

import hashlib
import json
import re
import time
from typing import Optional

from sqlalchemy import inspect, text

from log import log

TABLE = "antigravity_credentials"


def rename_legacy_table(sync_conn) -> Optional[str]:
    """若为旧结构则改名保留，返回旧表新名；否则 None。"""
    inspector = inspect(sync_conn)
    if not inspector.has_table(TABLE):
        return None
    cols = {c["name"] for c in inspector.get_columns(TABLE)}
    if "filename" not in cols and "email" in cols:
        return None
    legacy = f"{TABLE}_legacy_{time.time_ns()}"
    sync_conn.execute(text(f"ALTER TABLE {TABLE} RENAME TO {legacy}"))
    for idx in inspect(sync_conn).get_indexes(legacy):
        name = idx.get("name")
        if name and not name.startswith("sqlite_"):
            quoted_name = sync_conn.dialect.identifier_preparer.quote(name)
            if sync_conn.dialect.name == "postgresql":
                # PostgreSQL reflects UNIQUE-constraint backing indexes too.
                # DROP INDEX rejects those dependencies. Renaming all reflected
                # indexes keeps the old table intact and frees explicit names
                # such as idx_ag_disabled for the replacement table.
                suffix = hashlib.sha256(f"{legacy}:{name}".encode()).hexdigest()[:24]
                replacement = f"agy_legacy_{suffix}"
                sync_conn.execute(text(f"ALTER INDEX {quoted_name} RENAME TO {replacement}"))
            else:
                sync_conn.execute(text(f'DROP INDEX IF EXISTS {quoted_name}'))
    log.warning(f"[MIGRATE] 旧版凭证表已改名保留为 {legacy}（不删除），稍后迁移到新表")
    return legacy


def migrate_rows(sync_conn, legacy: str) -> int:
    from src.storage.database import get_cooldown_group

    if not re.fullmatch(TABLE + r"_legacy_[0-9]+", legacy):
        raise ValueError("Invalid legacy table identifier")
    rows = sync_conn.execute(text(f"SELECT * FROM {legacy}")).mappings().all()
    n = 0
    for r in rows:
        try:
            cd = json.loads(r.get("credential_data") or "{}")
        except Exception:
            cd = {}
        if not isinstance(cd, dict):
            cd = {}
        # Older schemas stored fields directly; retain those values as well as
        # nested credential_data. Unknown identities are quarantined, not lost.
        for key in ("access_token", "token", "refresh_token", "client_id", "client_secret", "project_id", "expiry", "email"):
            if not cd.get(key) and r.get(key) is not None:
                cd[key] = r[key]
        email = str(r.get("user_email") or cd.get("email") or "").strip()
        filename = str(r.get("filename") or "")
        if not email:
            stem = filename.rsplit("/", 1)[-1]
            if stem.endswith(".json"):
                stem = stem[:-5]
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", stem):
                email = stem
        unidentified = not email
        if unidentified:
            digest = hashlib.sha256(f"{legacy}|{r.get('id')}|{filename}".encode()).hexdigest()[:24]
            email = f"legacy-{digest}@unidentified.invalid"
            log.warning("[MIGRATE] 无身份的旧凭证已保留为停用记录，原表可供人工恢复")
        exists = sync_conn.execute(
            text(f"SELECT 1 FROM {TABLE} WHERE email=:e"), {"e": email}
        ).first()
        if exists:
            continue
        cooldowns = {}
        try:
            for k, v in (json.loads(r.get("model_cooldowns") or "{}") or {}).items():
                g = get_cooldown_group(k)
                if v and (g not in cooldowns or v > cooldowns[g]):
                    cooldowns[g] = v
        except Exception:
            cooldowns = {}
        now = time.time()
        sync_conn.execute(
            text(
                f"INSERT INTO {TABLE} (email, access_token, refresh_token, client_id, client_secret, "
                "project_id, expiry, disabled, error_codes, error_messages, last_success, "
                "model_cooldowns, tier, created_at, updated_at) VALUES (:email, :at, :rt, :cid, :cs, "
                ":pid, :exp, :dis, :ec, :em, :ls, :mc, :tier, :ca, :ua)"
            ),
            {
                "email": email,
                "at": cd.get("access_token") or cd.get("token") or "",
                "rt": cd.get("refresh_token") or "",
                "cid": cd.get("client_id") or "",
                "cs": cd.get("client_secret") or "",
                "pid": cd.get("project_id") or "",
                "exp": str(cd.get("expiry") or ""),
                "dis": unidentified or bool(r.get("disabled") or 0),
                "ec": r.get("error_codes") or "[]",
                "em": "{}",
                "ls": r.get("last_success"),
                "mc": json.dumps(cooldowns),
                "tier": r.get("tier") or "pro",
                "ca": r.get("created_at") or now,
                "ua": now,
            },
        )
        n += 1
    log.warning(f"[MIGRATE] 已从 {legacy} 迁移 {n} 个账号到 {TABLE}")
    return n
