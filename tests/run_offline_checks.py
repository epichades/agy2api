"""Run regression and golden suites with synthetic state and no upstream access."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    environment = os.environ.copy()
    environment.update(ENABLE_LOG="0", LOG_FILE="", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-o",
                    "asyncio_default_fixture_loop_scope=function"], env=environment, check=True)

    import httpx
    import config
    with tempfile.TemporaryDirectory(prefix="agy2api-golden-state-") as folder:
        for name in (*config.ENV_MAPPINGS, "POSTGRESQL_URI", "REDIS_URL", "RESPONSES_STATE_KEY"):
            os.environ.pop(name, None)
        os.environ.update(CREDENTIALS_DIR=folder, API_PASSWORD="synthetic-golden-api",
                          PANEL_PASSWORD="synthetic-golden-panel", ENABLE_LOG="0", LOG_FILE="",
                          RESPONSES_CLAUDE_THINKING_WITH_TOOLS="1", RESPONSES_ALLOW_HISTORY_FALLBACK="0",
                          RESPONSES_UNKNOWN_PARAMS="reject")
        config._config_cache = {}
        config._config_initialized = True
        config._config_source = None

        async def denied_async(*args, **kwargs):
            raise RuntimeError("offline golden: outbound HTTP disabled")

        with patch("dotenv.load_dotenv", return_value=False), \
             patch.object(httpx.HTTPTransport, "handle_request", side_effect=httpx.ConnectError("offline golden")), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", denied_async):
            runpy.run_path("tests/responses/run_tests.py", run_name="__main__")


if __name__ == "__main__":
    main()
