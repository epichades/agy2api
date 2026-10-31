"""
[local patch] 旧客户端兼容：gcli2api 时代的 Antigravity 路由挂在 /antigravity/v1/... 与 /antigravity/v1beta/...，
agy2api 改为直接挂在 /v1/... 。此 ASGI 中间件把 /antigravity/v1* 剥成 /v1*，两套路径同时可用。
"""

_PREFIX = "/antigravity"
_PREFIX_B = _PREFIX.encode("ascii")


class AntigravityCompatPrefixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith(_PREFIX + "/v1"):
                scope = dict(scope)
                scope["path"] = path[len(_PREFIX):]
                raw = scope.get("raw_path")
                if isinstance(raw, bytes) and raw.startswith(_PREFIX_B):
                    scope["raw_path"] = raw[len(_PREFIX_B):]
        await self.app(scope, receive, send)
