"""Private, stable account correlation without plaintext email logging."""
import io
import logging
import re

import pytest

import log as logger


@pytest.mark.parametrize("key", ["", "short-key"])
def test_missing_or_short_key_keeps_generic_email_mask(monkeypatch, key):
    monkeypatch.setenv("LOG_ACCOUNT_TAG_KEY", key)
    assert logger._redact("account=sample@example.invalid") == "account=[email]"


def test_tags_are_stable_distinct_and_change_when_private_key_rotates(monkeypatch):
    monkeypatch.setenv("LOG_ACCOUNT_TAG_KEY", "synthetic-account-tag-key-0000000001")
    first = logger._redact("SAMPLE@example.invalid")
    assert re.fullmatch(r"\[account:[0-9a-f]{16}\]", first)
    assert first == logger._redact("sample@example.invalid")
    assert "account=" + first == logger._redact("account=sample@example.invalid")
    assert "email=" + first == logger._redact("email=sample@example.invalid")
    assert first != logger._redact("another@example.invalid")
    monkeypatch.setenv("LOG_ACCOUNT_TAG_KEY", "synthetic-account-tag-key-0000000002")
    assert first != logger._redact("sample@example.invalid")


def test_console_file_and_standard_logging_share_account_tags(monkeypatch, capsys):
    key = "synthetic-account-tag-key-0000000001"
    monkeypatch.setenv("LOG_ACCOUNT_TAG_KEY", key)
    monkeypatch.setattr(logger, "_log_enabled", True)
    monkeypatch.setattr(logger, "_console_enabled", True)
    monkeypatch.setattr(logger, "_cached_log_level", 0)
    entries = []
    class FileSink:
        def info(self, value):
            entries.append(value)
    monkeypatch.setattr(logger, "_file_logger", FileSink())
    message = "Selected sample@example.invalid status=200"
    expected = logger._redact(message)
    logger.log.info(message)
    stdout = capsys.readouterr().out
    target = logging.getLogger("audit.account-tags")
    output = io.StringIO()
    monkeypatch.setattr(target, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(target, "filters", [logger._PrivacyLogFilter()])
    monkeypatch.setattr(target, "propagate", False)
    monkeypatch.setattr(target, "level", logging.INFO)
    target.info("Selected %s status=200", "sample@example.invalid")
    for value in (stdout, entries[0], output.getvalue()):
        assert expected in value
        assert "sample@example.invalid" not in value and key not in value


@pytest.mark.parametrize("field", ["LOG_ACCOUNT_TAG_KEY", "RESPONSES_STATE_KEY"])
def test_private_key_configuration_is_redacted_in_diagnostics(field):
    value = "synthetic-private-configuration-value"
    assert value not in logger._redact(f"{field}={value}")
    assert value not in logger._redact(f"/endpoint?{field}={value}&safe=1")
