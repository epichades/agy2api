"""Console privacy filters preserve useful diagnostics and log controls."""

import pytest

import log as log_module


@pytest.fixture(autouse=True)
def enabled_debug_logs(monkeypatch):
    # Register original values for automatic restoration after _refresh_config.
    monkeypatch.setattr(log_module, "_cached_log_level", log_module._cached_log_level)
    monkeypatch.setattr(log_module, "_log_enabled", log_module._log_enabled)
    monkeypatch.setenv("ENABLE_LOG", "1")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    log_module._refresh_config()


def test_account_emails_are_redacted_in_stdout_and_stderr(capsys):
    log_module.log.info("Selected credential sample.user+tag@example.invalid; status=200")
    log_module.log.error("Refresh failed for another@example.invalid: status=401")

    output = capsys.readouterr()
    assert "Selected credential [email]; status=200" in output.out
    assert "Refresh failed for [email]: status=401" in output.err
    assert "@example.invalid" not in output.out + output.err
    assert "[INFO]" in output.out
    assert "[ERROR]" in output.err


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
@pytest.mark.parametrize("password", ["private-value", "private@value", "private%40value"])
def test_url_userinfo_is_redacted_with_host_and_path_preserved(capsys, scheme, password):
    log_module.log.warning(
        f"Proxy failed: {scheme}://synthetic-user:{password}@proxy.invalid:8080/path?retry=1"
    )

    output = capsys.readouterr().out
    assert f"{scheme}://[redacted]@proxy.invalid:8080/path?retry=1" in output
    assert "synthetic-user" not in output
    assert password not in output


def test_urls_without_userinfo_and_normal_messages_are_preserved(capsys):
    log_module.log.info("Request completed: https://api.invalid/v1/models; status=200")

    output = capsys.readouterr()
    assert "Request completed: https://api.invalid/v1/models; status=200" in output.out
    assert output.err == ""


def test_disabled_logs_emit_nothing(monkeypatch, capsys):
    monkeypatch.setenv("ENABLE_LOG", "0")
    log_module._refresh_config()
    log_module.log.info("ordinary info")
    log_module.log.error("ordinary error")

    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_severity_filter_and_dynamic_level_still_apply(monkeypatch, capsys):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    log_module._refresh_config()
    log_module.log.debug("hidden debug")
    log_module.log.info("hidden info")
    log_module.log.warning("visible warning")
    assert log_module.set_log_level("error")
    log_module.log.warning("hidden warning")
    log_module.log.error("visible error")

    output = capsys.readouterr()
    assert "visible warning" in output.out
    assert "visible error" in output.err
    assert "hidden" not in output.out + output.err
    assert log_module.get_log_level() == "error"
    assert not log_module.set_log_level("invalid")
