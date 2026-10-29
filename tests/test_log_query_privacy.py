"""Credential queries are redacted at every common logger output boundary."""
import io
import logging

import pytest

import log as logger


@pytest.fixture(autouse=True)
def enable_logger(monkeypatch):
    monkeypatch.setattr(logger, "_log_enabled", True)
    monkeypatch.setattr(logger, "_console_enabled", True)
    monkeypatch.setattr(logger, "_cached_log_level", 0)
    monkeypatch.setattr(logger, "_file_logger", None)


@pytest.mark.parametrize("name", [
    "key", "KEY", "api_key", "access%5Ftoken", "access%255ftoken",
    "refresh_token", "id_token", "client_secret", "password", "code", "state",
])
@pytest.mark.parametrize("prefix", ["https://example.invalid/path", "/path"])
def test_query_names_are_redacted_and_safe_parameters_preserved(name, prefix):
    value = "synthetic-private-query"
    output = logger._redact(f"GET {prefix}?{name}={value}&retry=1&{name}=x")
    assert value not in output
    assert f"{name}=x" not in output
    assert "retry=1" in output
    assert output.count("[redacted]") == 2


def test_wrapper_console_and_file_use_same_filter(monkeypatch, capsys):
    entries = []
    class FileSink:
        def info(self, entry):
            entries.append(entry)
    monkeypatch.setattr(logger, "_file_logger", FileSink())
    logger.log.error("GET /v1/models?key=synthetic-secret&safe=1")
    assert "synthetic-secret" not in capsys.readouterr().err
    assert "synthetic-secret" not in entries[0]
    assert "safe=1" in entries[0]


@pytest.mark.parametrize("value", ["'SYNTHETIC_SECRET", '"SYNTHETIC_SECRET', "prefix'SYNTHETIC_SECRET", "SYNTHETIC_SECRET with spaces"])
def test_quotes_and_decoded_spaces_do_not_expose_the_rest_of_a_query_secret(value):
    output = logger._redact(f"/models?key={value}&safe=1")
    assert "SYNTHETIC_SECRET" not in output
    assert "safe=1" in output


def test_stdlib_interpolation_and_traceback_are_sanitized(monkeypatch):
    target = logging.getLogger("audit.query.privacy")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    monkeypatch.setattr(target, "handlers", [handler])
    monkeypatch.setattr(target, "filters", [])
    monkeypatch.setattr(target, "propagate", False)
    target.setLevel(logging.INFO)
    logger.attach_stdlib_loggers((target.name,))
    secret = "synthetic-oauth-secret"
    target.info("GET /callback?code=%s&safe=1", secret)
    try:
        raise ValueError("/callback?state=" + secret)
    except ValueError:
        target.exception("Callback failed")
    assert secret not in output.getvalue()
    assert "safe=1" in output.getvalue()
    assert "ValueError" in output.getvalue()


def test_uvicorn_structured_access_formatter_remains_valid(monkeypatch):
    from uvicorn.logging import AccessFormatter
    target = logging.getLogger("uvicorn.access")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter('%(client_addr)s %(request_line)s %(status_code)s', use_colors=False))
    monkeypatch.setattr(target, "handlers", [handler])
    monkeypatch.setattr(target, "filters", [])
    monkeypatch.setattr(target, "propagate", False)
    target.setLevel(logging.INFO)
    logger.attach_stdlib_loggers()
    target.info('%s - "%s %s HTTP/%s" %d', '127.0.0.1:1234', 'GET', '/models?key=synthetic-private', '1.1', 200)
    assert "synthetic-private" not in output.getvalue()
    assert "GET /models?key=[redacted] HTTP/1.1" in output.getvalue()
    assert "200" in output.getvalue()


def test_disabled_logger_also_mutes_stdlib_console(monkeypatch):
    target = logging.getLogger("audit.query.disabled")
    output = io.StringIO()
    monkeypatch.setattr(target, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(target, "filters", [])
    monkeypatch.setattr(target, "propagate", False)
    target.setLevel(logging.INFO)
    logger.attach_stdlib_loggers((target.name,))
    monkeypatch.setattr(logger, "_log_enabled", False)
    target.info("must remain silent")
    assert output.getvalue() == ""


def test_stdlib_console_can_be_muted_without_disabling_private_file_sink(monkeypatch):
    target = logging.getLogger("audit.query.console-policy")
    output, entries = io.StringIO(), []
    class FileSink:
        def info(self, entry):
            entries.append(entry)
    monkeypatch.setattr(logger, "_file_logger", FileSink())
    monkeypatch.setattr(logger, "_console_enabled", False)
    monkeypatch.setattr(target, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(target, "filters", [])
    monkeypatch.setattr(target, "propagate", False)
    target.setLevel(logging.INFO)
    logger.attach_stdlib_loggers((target.name,))
    target.info("GET /models?key=synthetic-secret&safe=1")
    assert output.getvalue() == ""
    assert len(entries) == 1 and "synthetic-secret" not in entries[0]
