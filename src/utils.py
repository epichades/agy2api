from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Optional

import httpx
import jwt

from config import get_api_password, get_panel_password
from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from log import log

# HTTP Bearer security scheme
security = HTTPBearer()

# ====================== OAuth Configuration ======================

# Antigravity CLI 客户端仿真常量
_DEFAULT_ANTIGRAVITY_CLI_VERSION = "1.4.7"
_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/google-antigravity/antigravity-cli/releases/latest"
)


def _get_latest_antigravity_cli_version() -> str:
    """获取最新 Antigravity CLI 版本；获取失败时返回默认版本。"""
    try:
        response = httpx.get(
            _LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        response.raise_for_status()
        release_data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        log.warning(
            "获取最新 Antigravity CLI 版本失败，使用默认版本 "
            f"{_DEFAULT_ANTIGRAVITY_CLI_VERSION}: {error}"
        )
        return _DEFAULT_ANTIGRAVITY_CLI_VERSION

    tag_name = release_data.get("tag_name") if isinstance(release_data, dict) else None
    if not isinstance(tag_name, str) or not tag_name.strip():
        log.warning(
            "GitHub API 响应中没有有效的 tag_name，使用默认版本 "
            f"{_DEFAULT_ANTIGRAVITY_CLI_VERSION}"
        )
        return _DEFAULT_ANTIGRAVITY_CLI_VERSION

    return tag_name.strip().lstrip("vV")


ANTIGRAVITY_CLI_VERSION = _get_latest_antigravity_cli_version()
ANTIGRAVITY_CLI_PLATFORM = "windows/amd64"
ANTIGRAVITY_USER_AGENT = f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} {ANTIGRAVITY_CLI_PLATFORM}"
log.info(f"当前 Antigravity CLI 版本: {ANTIGRAVITY_CLI_VERSION}")

# Antigravity OAuth Configuration
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs'
]

# 统一的 Token URL（两种模式相同）
TOKEN_URL = "https://oauth2.googleapis.com/token"

# 回调服务器配置
CALLBACK_HOST = "localhost"

# ====================== Model Helper Functions ======================

# 抗截断模型前缀
_ANTI_TRUNCATION_PREFIX = "抗截断-"


def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith(_ANTI_TRUNCATION_PREFIX)


def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    if model_name.startswith(_ANTI_TRUNCATION_PREFIX):
        return model_name[len(_ANTI_TRUNCATION_PREFIX) :]
    return model_name


# ====================== Authentication Functions ======================

async def authenticate_flexible(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    access_token: Optional[str] = Header(None, alias="access_token"),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    x_anthropic_auth_token: Optional[str] = Header(None, alias="x-anthropic-auth-token"),
    anthropic_auth_token: Optional[str] = Header(None, alias="anthropic-auth-token"),
    key: Optional[str] = Query(None)
) -> str:
    """
    统一的灵活认证函数，支持多种认证方式

    此函数可以直接用作 FastAPI 的 Depends 依赖

    支持的认证方式:
        - URL 参数: key
        - HTTP 头部: Authorization (Bearer token)
        - HTTP 头部: x-api-key
        - HTTP 头部: access_token
        - HTTP 头部: x-goog-api-key
        - HTTP 头部: x-anthropic-auth-token
        - HTTP 头部: anthropic-auth-token

    Args:
        request: FastAPI Request 对象
        authorization: Authorization 头部值（自动注入）
        x_api_key: x-api-key 头部值（自动注入）
        access_token: access_token 头部值（自动注入）
        x_goog_api_key: x-goog-api-key 头部值（自动注入）
        x_anthropic_auth_token: x-anthropic-auth-token 头部值（自动注入）
        anthropic_auth_token: anthropic-auth-token 头部值（自动注入）
        key: URL 参数 key（自动注入）

    Returns:
        验证通过的token

    Raises:
        HTTPException: 认证失败时抛出异常

    使用示例:
        @router.post("/endpoint")
        async def endpoint(token: str = Depends(authenticate_flexible)):
            # token 已验证通过
            pass
    """
    password = await get_api_password()
    token = None
    auth_method = None

    # 1. 尝试从 URL 参数 key 获取（Google 官方标准方式）
    if key:
        token = key
        auth_method = "URL parameter 'key'"

    # 2. 尝试从 x-goog-api-key 头部获取（Google API 标准方式）
    elif x_goog_api_key:
        token = x_goog_api_key
        auth_method = "x-goog-api-key header"

    # 3. 尝试从 x-anthropic-auth-token 头部获取（Anthropic 标准方式）
    elif x_anthropic_auth_token:
        token = x_anthropic_auth_token
        auth_method = "x-anthropic-auth-token header"

    # 4. 尝试从 anthropic-auth-token 头部获取（Anthropic 替代方式）
    elif anthropic_auth_token:
        token = anthropic_auth_token
        auth_method = "anthropic-auth-token header"

    # 5. 尝试从 x-api-key 头部获取
    elif x_api_key:
        token = x_api_key
        auth_method = "x-api-key header"

    # 6. 尝试从 access_token 头部获取
    elif access_token:
        token = access_token
        auth_method = "access_token header"

    # 7. 尝试从 Authorization 头部获取
    elif authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[7:]  # 移除 "Bearer " 前缀
        auth_method = "Authorization Bearer header"

    # 检查是否提供了任何认证凭据
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials. Use 'key' URL parameter, 'x-goog-api-key', 'x-anthropic-auth-token', 'anthropic-auth-token', 'x-api-key', 'access_token' header, or 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 验证 token
    if token != password:
        log.debug(f"Authentication failed using {auth_method}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="密码错误"
        )
    
    log.debug(f"Authentication successful using {auth_method}")
    return token


# 为了保持向后兼容，保留旧函数名作为别名
authenticate_bearer = authenticate_flexible
authenticate_gemini_flexible = authenticate_flexible


# ====================== Panel Authentication Functions ======================

def _panel_signing_key(panel_password: str) -> bytes:
    return hashlib.sha256(b"agy2api-panel-session-v1\0" + panel_password.encode()).digest()


def create_panel_token(panel_password: str) -> str:
    """Issue a short-lived panel session; the browser never stores the password."""
    if not panel_password:
        raise ValueError("Panel password is not configured")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "panel",
            "iat": now,
            "exp": now + timedelta(hours=8),
            "jti": secrets.token_urlsafe(16),
        },
        _panel_signing_key(panel_password),
        algorithm="HS256",
    )


async def verify_panel_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    验证有效期内的控制面板会话令牌。

    Args:
        credentials: HTTPAuthorizationCredentials 自动注入

    Returns:
        验证通过的token

    Raises:
        HTTPException: 密码错误时抛出401异常
    """

    password = await get_panel_password()
    if not password:
        raise HTTPException(status_code=401, detail="密码错误")
    try:
        claims = jwt.decode(
            credentials.credentials,
            _panel_signing_key(password),
            algorithms=["HS256"],
            options={"require": ["sub", "iat", "exp", "jti"]},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="会话无效或已过期")
    if claims.get("sub") != "panel":
        raise HTTPException(status_code=401, detail="会话无效或已过期")
    return credentials.credentials
