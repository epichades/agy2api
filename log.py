"""
日志模块 - 无状态函数风格（控制台 + 可选文件）
- LOG_LEVEL 控制级别（debug/info/warning/error/critical，默认 info）
- ENABLE_LOG=0 彻底静默（不打印任何日志）
- [local patch] LOG_FILE=路径 时同时写入文件：追加模式（重启不清空），
  按大小轮转 LOG_FILE_MAX_BYTES（默认 10MB）x LOG_FILE_BACKUPS（默认 2）
- [local patch] 时间戳带时区（LOG_TZ，默认 Asia/Taipei），格式 %Y-%m-%d %H:%M:%S%z
- [local patch] LOG_CONSOLE=0 时不再打印到控制台（避免 stdout 重定向文件无限增长）
- [local patch] 写出前对 Bearer / ya29 / access_token 等敏感值脱敏
- log.debug/info/warning/error/critical(message) 记录日志
- set_log_level 动态调整级别，get_log_level 读取当前级别
"""

import logging
import os
import re
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from urllib.parse import unquote_plus

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# 日志级别定义
LOG_LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

# 缓存配置（模块加载时读取一次）
_cached_log_level: int = LOG_LEVELS["info"]
_log_enabled: bool = True
_console_enabled: bool = True
_tz = None
_file_logger = None
_file_handler = None

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[^\s,;\"'<>]+"), r"\1***"),
    (re.compile(r"ya29\.[A-Za-z0-9._\-]+"), "ya29.***"),
    (re.compile(r"1//[A-Za-z0-9._\-]{20,}"), "1//***"),
    (re.compile(r"(?i)(['\"]?(?:access_token|refresh_token|id_token|client_secret|api_key|password|authorization)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}&#;]+"), r"\1***"),
]

_QUERY_VALUE_RE = re.compile(r"([?&;])([^=?&#\s\"'<>]+)=([^\r\n&#<>]*)")
_PRIVATE_QUERY_NAMES = {
    "key", "apikey", "token", "accesstoken", "refreshtoken", "idtoken",
    "clientsecret", "password", "authorization", "code", "state", "signature",
}


def _redact_query(match):
    separator, key, value = match.groups()
    decoded = key
    for _ in range(3):
        decoded = unquote_plus(decoded)
    normalized = re.sub(r"[^a-z0-9]", "", decoded.casefold())
    if normalized in _PRIVATE_QUERY_NAMES:
        return f"{separator}{key}=[redacted]"
    return match.group(0)


def _redact(message: str) -> str:
    message = _redact_message(message)
    for pat, rep in _SECRET_PATTERNS:
        message = pat.sub(rep, message)
    return _QUERY_VALUE_RE.sub(_redact_query, message)


def _setup_file_logger():
    global _file_logger, _file_handler
    path = os.getenv("LOG_FILE", "").strip()
    if not path:
        return
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        max_bytes = int(os.getenv("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
        backups = int(os.getenv("LOG_FILE_BACKUPS", "2"))
        # mode="a"：追加，重启不清空
        handler = RotatingFileHandler(path, mode="a", maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("agy2api.file")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        _file_logger, _file_handler = logger, handler
    except Exception as e:
        if _log_enabled and _console_enabled:
            warning = _redact(f"Warning: Cannot open log file {path}, file logging disabled: {e}")
            print(warning, file=sys.stderr)

# Targeted privacy filtering for account identifiers and authenticated proxy URLs.
# This does not attempt to identify arbitrary secrets in free-form messages.
_URL_AUTHORITY_RE = re.compile(
    r"\b((?:https?|socks5h?)://)([^/\s?#<>\"']*)", re.IGNORECASE
)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@(?:[a-z0-9-]+\.)+[a-z]{2,}(?![\w-])",
    re.IGNORECASE,
)


def _redact_message(message: str) -> str:
    def redact_authority(match):
        scheme, authority = match.groups()
        if "@" not in authority:
            return match.group(0)
        # The final @ separates the host, even if the password contains an @.
        host = authority.rsplit("@", 1)[1]
        return f"{scheme}[redacted]@{host}"

    message = _URL_AUTHORITY_RE.sub(redact_authority, str(message))
    return _EMAIL_RE.sub("[email]", message)


def _refresh_config():
    """从环境变量刷新缓存配置"""
    global _cached_log_level, _log_enabled, _console_enabled, _tz
    level = os.getenv("LOG_LEVEL", "info").lower()
    _cached_log_level = LOG_LEVELS.get(level, LOG_LEVELS["info"])
    _log_enabled = os.getenv("ENABLE_LOG", "1").strip().lower() not in ("0", "false", "no", "off")
    _console_enabled = os.getenv("LOG_CONSOLE", "1").strip().lower() not in ("0", "false", "no", "off")
    tzname = os.getenv("LOG_TZ", "Asia/Taipei")
    try:
        _tz = ZoneInfo(tzname) if ZoneInfo else None
    except Exception:
        _tz = None


_refresh_config()
_setup_file_logger()


def _now_str() -> str:
    now = datetime.now(_tz) if _tz else datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S%z")


def _log(level: str, message: str):
    # 最快短路：日志整体已禁用时直接返回，零开销
    if not _log_enabled:
        return

    level_val = LOG_LEVELS.get(level.lower())
    if level_val is None:
        return

    if level_val < _cached_log_level:
        return

    entry = f"[{_now_str()}] [{level.upper()}] {_redact(message)}"

    if _console_enabled:
        if level in ("error", "critical"):
            print(entry, file=sys.stderr, flush=True)
        else:
            print(entry, flush=True)

    if _file_logger is not None:
        try:
            _file_logger.info(entry)
        except Exception:
            pass


class _StdlibToFile(logging.Handler):
    """把 uvicorn 等标准 logging 记录也写入 LOG_FILE（同一格式）"""

    def emit(self, record):
        try:
            lvl = record.levelname.lower()
            if not _log_enabled or LOG_LEVELS.get(lvl, 1) < _cached_log_level or _file_logger is None:
                return
            entry = f"[{_now_str()}] [{record.levelname}] [{record.name}] {_redact(record.getMessage())}"
            _file_logger.info(entry)
        except Exception:
            pass


class _PrivacyLogFilter(logging.Filter):
    """Sanitize records before console formatters and preserve structured args."""

    def filter(self, record):
        if not _log_enabled or LOG_LEVELS.get(record.levelname.lower(), 1) < _cached_log_level:
            return False

        def clean(value):
            if isinstance(value, str):
                return _redact(value)
            if isinstance(value, tuple):
                return tuple(clean(v) for v in value)
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value

        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            # Uvicorn's AccessFormatter unpacks these five fields itself.
            record.args = clean(record.args)
        else:
            # Sanitize after interpolation: redacting a '%s' placeholder first
            # would leave unused args and logging's error handler could leak them.
            record.msg = _redact(record.getMessage())
            record.args = ()
        if record.exc_info:
            record.exc_text = _redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        return True


class _ConsoleLogFilter(logging.Filter):
    def filter(self, record):
        return _console_enabled


def attach_stdlib_loggers(names=("uvicorn", "uvicorn.error", "uvicorn.access")):
    """Apply the same privacy boundary to Uvicorn console and file output."""
    for n in names:
        lg = logging.getLogger(n)
        if not any(isinstance(f, _PrivacyLogFilter) for f in lg.filters):
            lg.addFilter(_PrivacyLogFilter())
        for handler in lg.handlers:
            if not any(isinstance(f, _PrivacyLogFilter) for f in handler.filters):
                handler.addFilter(_PrivacyLogFilter())
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                if not any(isinstance(f, _ConsoleLogFilter) for f in handler.filters):
                    handler.addFilter(_ConsoleLogFilter())
        if _file_logger is not None and not any(isinstance(h, _StdlibToFile) for h in lg.handlers):
            lg.addHandler(_StdlibToFile())


def set_log_level(level: str):
    """动态设置日志级别"""
    global _cached_log_level
    level = level.lower()
    if level not in LOG_LEVELS:
        return False
    _cached_log_level = LOG_LEVELS[level]
    return True


def get_log_level() -> str:
    """获取当前日志级别名称"""
    current = _cached_log_level
    for name, value in LOG_LEVELS.items():
        if value == current:
            return name
    return "info"


# 日志接口：log.debug/info/warning/error/critical(message)
log = SimpleNamespace(
    debug=lambda message: _log("debug", message),
    info=lambda message: _log("info", message),
    warning=lambda message: _log("warning", message),
    error=lambda message: _log("error", message),
    critical=lambda message: _log("critical", message),
)


# 导出的公共接口
__all__ = ["log", "set_log_level", "get_log_level", "LOG_LEVELS", "attach_stdlib_loggers"]
