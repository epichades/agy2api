"""Ensure future personal files cannot silently join Git or Docker uploads."""

from fnmatch import fnmatchcase
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _docker_allowed(path: str) -> bool:
    """Check the narrow allowlist policy, without relying on a Docker daemon."""
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert "**" in lines, "Docker context must begin from deny-all"
    allowed = [line[1:] for line in lines if line.startswith("!")]
    return any(fnmatchcase(path, rule) for rule in allowed if not rule.endswith("/"))


def test_git_ignores_private_tooling_and_notes():
    private_paths = [
        ".aws/credentials", ".ssh/id_ed25519", ".npmrc", ".pypirc",
        ".netrc", ".agents/skills/private/SKILL.md", ".codex/config.toml",
        ".env.production", ".mcp.json", "src/private-note.txt",
        "docs/private-note.txt", "dist/private-note.txt",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT, input="\n".join(private_paths) + "\n", text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == set(private_paths)


def test_docker_context_uses_explicit_runtime_sources_and_public_assets():
    allowed_paths = [
        "Dockerfile", "requirements.txt", "web.py", "config.py", "log.py",
        "src/auth.py", "src/api/antigravity.py", "src/panel/config_routes.py",
        "dist/index.html", "dist/assets/index-review.js",
        "dist/assets/index-review.css", "docs/README_EN.md",
    ]
    denied_paths = [
        ".git/config", ".env", ".env.example", ".aws/credentials",
        "src/private-note.txt", "src/panel/private-settings.json",
        "docs/private-note.txt", "docs/private-receipt.jpg",
        "dist/private-note.txt", "dist/assets/credentials.json",
        "frontend/.npmrc", "data/request_logs/private.json",
    ]
    assert all(_docker_allowed(path) for path in allowed_paths)
    assert all(not _docker_allowed(path) for path in denied_paths)

    # Adding an application module requires an explicit Docker context review.
    current_sources = [p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py")]
    assert current_sources
    assert all(_docker_allowed(path) for path in current_sources)
