"""Exercise the publication gate on synthetic repositories, never user secrets."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_publication.py"
spec = importlib.util.spec_from_file_location("publication_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.check_output(["git", *args], cwd=repo, env=env, stderr=subprocess.PIPE)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Synthetic Test")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    (repo / "README.md").write_text("Public source\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "synthetic base")
    git(repo, "branch", "base")
    return repo


def commit(repo):
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "synthetic change")


def run(repo, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, text=True, capture_output=True)
    assert result.stderr == ""
    return result


def test_pre_push_hook_blocks_private_outgoing_commit(repository):
    import shutil
    root = SCRIPT.parents[1]
    (repository / 'scripts').mkdir()
    (repository / '.githooks').mkdir()
    shutil.copyfile(SCRIPT, repository / 'scripts' / SCRIPT.name)
    hook = repository / '.githooks' / 'pre-push'
    shutil.copyfile(root / '.githooks' / 'pre-push', hook)
    commit(repository)
    git(repository, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    (repository / '.env').write_text('synthetic-private-content')
    commit(repository)
    sha = git(repository, 'rev-parse', 'HEAD').decode().strip()
    result = subprocess.run(['bash', str(hook)], cwd=repository,
                            input=f'refs/heads/test {sha} refs/heads/test ' + '0' * 40 + '\n',
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert 'private-environment-file' in result.stdout
    assert 'synthetic-private-content' not in result.stdout + result.stderr


def test_deleted_ancestor_leaks_still_block_and_values_never_print(repository):
    token = "ya29." + "aB3X" * 12
    host = "private-host." + "tail-synthetic" + ".ts" + ".net"
    note = repository / "notes.md"
    note.write_text(f"Host {host}\nCredential {token}\n")
    commit(repository)
    note.unlink()
    commit(repository)
    result = run(repository, "--base", "base", "--ref", "HEAD")
    assert result.returncode == 1
    assert '"notes.md"' in result.stdout
    assert "private-tailnet-host" in result.stdout and "user-access-token" in result.stdout
    assert "outgoing_commits=2" in result.stdout
    assert token not in result.stdout and host not in result.stdout


@pytest.mark.parametrize("path", [
    ".env", ".env.production", "creds/credentials.db", "state.sqlite3-wal",
    "responses-state.key", "backups/notes.md", "private.bundle", "app.log.1",
])
def test_private_paths_are_blocked_before_blob_read(repository, monkeypatch, path):
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic-private-content-that-must-not-be-read")
    commit(repository)
    scanner = gate.Scanner(gate.Repository(repository))
    monkeypatch.setattr(scanner.repository, "blob", lambda _oid: pytest.fail("opened private blob"))
    scanner.history("base", "HEAD")
    assert (path, gate.private_path(path)) in scanner.findings


def test_public_oauth_client_constants_and_synthetic_fixture_allowed(repository):
    (repository / "public.py").write_text(
        'ANTIGRAVITY_CLIENT_ID = "public-client.apps.googleusercontent.com"\n'
        'ANTIGRAVITY_CLIENT_SECRET = "GOCSPX-public-cli-client-constant"\n'
    )
    (repository / ".env.example").write_text("API_PASSWORD=\nPANEL_PASSWORD=\nRESPONSES_STATE_KEY=\nLOG_ACCOUNT_TAG_KEY=\n")
    fixture = repository / "tests" / "fixtures" / "synthetic.sse"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("data: " + json.dumps({"response": {
        "responseId": "SYNTHETIC_RESPONSE_ID", "requestId": "synthetic-request",
        "parts": [{"thoughtSignature": "SYNTHETIC_SIGNATURE", "text": "synthetic thought"}],
        "functionCall": {"id": "SYNTHETIC_CALL_ID"},
    }}) + "\n\n")
    commit(repository)
    result = run(repository, "--base", "base")
    assert result.returncode == 0, result.stdout
    assert result.stdout.startswith("PASS")


def test_account_tag_key_must_not_be_filled_in_public_example(repository):
    value = "private-config-value-not-for-publication"
    (repository / ".env.example").write_text("LOG_ACCOUNT_TAG_KEY=" + value + "\n")
    commit(repository)
    result = run(repository, "--base", "base")
    assert result.returncode == 1
    assert '.env.example' in result.stdout
    assert value not in result.stdout


@pytest.mark.parametrize("field,category", [
    ("thoughtSignature", "fixture-nonsynthetic-signature"),
    ("responseId", "fixture-nonsynthetic-identifier"),
    ("traceId", "fixture-nonsynthetic-identifier"),
])
def test_captured_fixture_metadata_blocks_without_printing_value(repository, field, category):
    secret = "captured-resource-" + "xY7" * 17
    fixture = repository / "captured.sse"
    fixture.write_text("data: " + json.dumps({field: secret}) + "\n\n")
    commit(repository)
    result = run(repository, "--base", "base")
    assert result.returncode == 1
    assert category in result.stdout and '"captured.sse"' in result.stdout
    assert secret not in result.stdout


def test_working_scan_skips_ignored_secrets_and_never_follows_symlink(repository, monkeypatch):
    (repository / ".gitignore").write_text(".env\ncreds/\n")
    ignored = repository / ".env"
    ignored.write_text("synthetic-ignored-secret")
    (repository / "public-link.py").symlink_to(ignored)
    scanner = gate.Scanner(gate.Repository(repository))
    original_open = os.open

    def protected_open(path, *args, **kwargs):
        assert Path(path) not in {ignored, repository / "public-link.py"}
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", protected_open)
    scanner.working_tree()
    assert ("public-link.py", "symlink-not-inspected") in scanner.findings
    assert not any(path == ".env" for path, _ in scanner.findings)


def test_working_scan_includes_untracked_candidates_without_requiring_base(repository):
    secret = "1//" + "qR8" * 15
    (repository / "untracked.md").write_text(secret)
    result = run(repository, "--working-tree", "--base", "missing-reference")
    assert result.returncode == 1
    assert "user-refresh-token" in result.stdout and "untracked.md" in result.stdout
    assert secret not in result.stdout


def test_private_key_and_personal_home_categories_do_not_echo_contents(repository):
    home = "/Users/" + "synthetic-person/private-project"
    key = "-----BEGIN " + "PRIVATE KEY-----\nsynthetic-key-material"
    (repository / "notes.md").write_text(home + "\n" + key)
    result = run(repository, "--working-tree")
    assert result.returncode == 1
    assert "personal-home-path" in result.stdout and "private-key-material" in result.stdout
    assert home not in result.stdout and key not in result.stdout


def test_invalid_reference_fails_closed_without_git_diagnostic(repository):
    secret_ref = "synthetic-private-ref-that-must-not-print"
    result = run(repository, "--base", secret_ref)
    assert result.returncode == 2
    assert result.stdout == "ERROR [publication-scan-incomplete]\n"
    assert secret_ref not in result.stdout


def test_filled_public_environment_example_blocks_without_echo(repository):
    secret = "unexpected-live-password-" + "n9" * 12
    (repository / ".env.example").write_text("API_PASSWORD=" + secret + "\n")
    result = run(repository, "--working-tree")
    assert result.returncode == 1
    assert "private-value-in-public-env-example" in result.stdout
    assert secret not in result.stdout


def test_placeholder_environment_values_are_allowed(repository):
    (repository / ".env.example").write_text("API_PASSWORD=REPLACE_WITH_RANDOM_SECRET\nPANEL_PASSWORD=<private-value>\n")
    result = run(repository, "--working-tree")
    assert result.returncode == 0, result.stdout
