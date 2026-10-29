"""
Antigravity 精确模型路由 (local patch, ported to agy2api email-keyed storage)

- 每个启用账号缓存一份上游 fetchAvailableModels 的真实模型列表 (TTL: ANTIGRAVITY_MODEL_LIST_TTL, 默认 300s)
- 请求只路由到「实时模型列表包含该精确模型 ID」的账号；绝不改写 / 降级 / 换模型
- 平均路由：所有具备该模型且未冷却、额度>0 的账号同等对待，随机选择
- 与上游 session_cache 兼容：cache/sticky 模式下的亲和账号仅在其属于「具备该精确模型且未冷却」集合时才复用，
  否则改选其它合格账号（同一模型），绝不换成别的模型
- 无账号提供该模型 -> 404；所有提供该模型的账号都在冷却/额度耗尽 -> 429
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Response

from log import log

# email -> {"ts": float, "models": set, "order": [ids], "quota": {model: (remaining, reset_ts)}}
_model_cache: Dict[str, Dict[str, Any]] = {}
_refresh_lock = asyncio.Lock()
_bg_task: Optional[asyncio.Task] = None
# (email, model) -> until_ts：429/503 且上游未给出冷却时间时的短暂内存冷却
_soft_cooldowns: Dict[Tuple[str, str], float] = {}


def _ttl() -> float:
    try:
        return float(os.getenv("ANTIGRAVITY_MODEL_LIST_TTL", "300"))
    except ValueError:
        return 300.0


def _parse_ts(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def _enabled_states() -> Dict[str, Dict[str, Any]]:
    from src.storage.database import get_database

    db = await get_database()
    states = await db.get_all_credential_states()
    return {e: st for e, st in states.items() if not st.get("disabled")}


async def _get_cred_with_fresh_token(email: str) -> Optional[Dict[str, Any]]:
    from src.credential_manager import refresh_token, should_refresh_token
    from src.storage.database import get_database

    db = await get_database()
    cred = await db.get_credential(email)
    if not cred:
        return None
    if await should_refresh_token(cred):
        cred = await refresh_token(cred, email)
    return cred


async def _fetch_one(email: str) -> Optional[Dict[str, Any]]:
    from config import get_antigravity_api_url
    from src.api.antigravity import build_antigravity_headers
    from src.httpx_client import post_async

    try:
        cred = await _get_cred_with_fresh_token(email)
        token = (cred or {}).get("access_token") or (cred or {}).get("token")
        if not token:
            return None
        url = await get_antigravity_api_url()
        resp = await post_async(
            url=f"{url}/v1internal:fetchAvailableModels",
            json={},
            headers=build_antigravity_headers(token, model_name="agent"),
            timeout=30.0,
        )
        if resp.status_code != 200:
            log.warning(f"[AGY ROUTE] fetchAvailableModels 失败 {email}: HTTP {resp.status_code}")
            return None
        models = (resp.json() or {}).get("models") or {}
        quota = {}
        for mid, m in models.items():
            q = (m or {}).get("quotaInfo") or {}
            if "remainingFraction" in q or "resetTime" in q:
                quota[mid] = (q.get("remainingFraction", 0), _parse_ts(q.get("resetTime", "")))
        return {"ts": time.time(), "models": set(models.keys()), "order": list(models.keys()), "quota": quota}
    except Exception as e:
        log.warning(f"[AGY ROUTE] fetchAvailableModels 异常 {email}: {e}")
        return None


async def _refresh(emails: List[str]) -> None:
    async with _refresh_lock:
        now = time.time()
        todo = [e for e in emails if now - _model_cache.get(e, {}).get("ts", 0) >= _ttl()]
        if not todo:
            return
        results = await asyncio.gather(*[_fetch_one(e) for e in todo])
        for em, res in zip(todo, results):
            if res is not None:
                _model_cache[em] = res
                log.info(f"[AGY ROUTE] 模型列表已刷新: {em} ({len(res['models'])} models)")
            elif em in _model_cache:
                _model_cache[em]["ts"] = now - _ttl() + 60  # 保留旧列表，60s 后重试
            else:
                # 从未成功：严格视为不支持任何模型，60s 后重试
                _model_cache[em] = {"ts": now - _ttl() + 60, "models": set(), "order": [], "quota": {}}


async def get_model_lists(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """返回 {email: cache_entry}，仅含启用账号。"""
    global _bg_task
    enabled = list((await _enabled_states()).keys())
    if force:
        for em in enabled:
            _model_cache.pop(em, None)
    missing = [em for em in enabled if em not in _model_cache]
    if missing:
        await _refresh(missing)
    stale = [em for em in enabled if time.time() - _model_cache.get(em, {}).get("ts", 0) >= _ttl()]
    if stale and (_bg_task is None or _bg_task.done()):
        _bg_task = asyncio.create_task(_refresh(stale))
    return {em: _model_cache[em] for em in enabled if em in _model_cache}


async def get_union_model_ids() -> List[str]:
    lists = await get_model_lists()
    seen: List[str] = []
    for entry in lists.values():
        for mid in entry.get("order", []):
            if mid not in seen:
                seen.append(mid)
    return seen


def note_error(email: str, model_name: Optional[str], status_code: Optional[int], cooldown_until: Optional[float]) -> None:
    """429/503 且上游未给出冷却时间时，设短暂内存冷却，使重试切到其它具备同一模型的账号。"""
    if not model_name or cooldown_until is not None:
        return
    if status_code == 429:
        _soft_cooldowns[(email, model_name)] = time.time() + 60
    elif status_code == 503:
        _soft_cooldowns[(email, model_name)] = time.time() + 20


def _cooldown_until(email: str, state: Dict[str, Any], entry: Dict[str, Any], model_name: str) -> Optional[float]:
    from src.storage.database import get_cooldown_group

    now = time.time()
    cands = []
    cds = state.get("model_cooldowns") or {}
    for key in (get_cooldown_group(model_name), model_name):
        cd = cds.get(key)
        if cd and cd > now:
            cands.append(cd)
    soft = _soft_cooldowns.get((email, model_name))
    if soft and soft > now:
        cands.append(soft)
    q = (entry.get("quota") or {}).get(model_name)
    if q:
        remaining, reset_ts = q
        if (remaining or 0) <= 0 and reset_ts and reset_ts > now:
            cands.append(reset_ts)
    return max(cands) if cands else None


async def classify(model_name: str) -> Tuple[List[str], List[Tuple[str, float]], List[str]]:
    """返回 (ready_emails, cooling[(email, until)], eligible_emails)"""
    lists = await get_model_lists()
    states = await _enabled_states()
    eligible = [em for em, e in lists.items() if model_name in e.get("models", set()) and em in states]
    ready, cooling = [], []
    for em in eligible:
        until = _cooldown_until(em, states[em], lists[em], model_name)
        if until:
            cooling.append((em, until))
        else:
            ready.append(em)
    return ready, cooling, eligible


async def select_credential(
    model_name: str,
    session_key: Optional[str] = None,
    mode: str = "random",
    preferred: Optional[str] = None,
) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
    """
    返回 ((email, credential_data) | None, info)
    info: {"reason": "ok"|"not_available"|"exhausted"|"no_token", "retry_at": ts}
    preferred: 会话亲和 / sticky 账号（仅在 ready 集合中才会被优先使用）
    """
    ready, cooling, eligible = await classify(model_name)
    if not eligible:
        return None, {"reason": "not_available"}
    if not ready:
        return None, {"reason": "exhausted", "retry_at": min(u for _, u in cooling)}

    pool = list(ready)
    random.shuffle(pool)
    how = "balanced"
    if preferred and preferred in pool:
        pool.remove(preferred)
        pool.insert(0, preferred)
        how = f"affinity({mode})"
    for em in pool:
        cred = await _get_cred_with_fresh_token(em)
        if not cred or not (cred.get("access_token") or cred.get("token")):
            continue
        log.info(
            f"[AGY ROUTE] model={model_name} -> {em} [{how if em == preferred else 'balanced'}] "
            f"eligible={len(eligible)} ready={len(ready)}"
        )
        return (em, cred), {"reason": "ok"}
    return None, {"reason": "no_token"}


async def no_credential_response(model_name: str) -> Response:
    """select_credential 返回 None 时构造清晰错误（不做任何降级/换模型）。"""
    ready, cooling, eligible = await classify(model_name)
    if not eligible:
        code, status = 404, "NOT_FOUND"
        msg = (f"Model '{model_name}' is not available on any enabled Antigravity account "
               f"(exact model id required; no aliasing or fallback is performed).")
    elif not ready:
        code, status = 429, "RESOURCE_EXHAUSTED"
        retry_at = min(u for _, u in cooling)
        when = datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
        msg = (f"Model '{model_name}': all accounts that serve this exact model are quota-exhausted "
               f"or cooling down (earliest retry {when}). No fallback to another model is performed.")
    else:
        code, status = 503, "UNAVAILABLE"
        msg = f"Model '{model_name}': no usable credential (token refresh failed or no access token)."
    log.warning(f"[AGY ROUTE] {code} {msg}")
    return Response(
        content=json.dumps({"error": {"code": code, "message": msg, "status": status}}),
        status_code=code,
        media_type="application/json",
    )
