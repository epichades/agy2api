#!/usr/bin/env python3
"""Local, value-free publication gate for new Git history or working candidates.

No network, global Git configuration, dotenv, filters, or application imports are
used. Known-private paths are classified before their contents are opened. The
history scan covers every outgoing tree, including files deleted in later commits.
This bounded pattern check complements review; it cannot certify arbitrary prose.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


MAX_CONTENT_BYTES = 32 * 1024 * 1024
CONTENT_PATTERNS = {
    "private-tailnet-host": re.compile(r"\b[a-z0-9][a-z0-9.-]*\.ts\.net\b", re.I),
    "private-tailnet-address": re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    "personal-home-path": re.compile(r"/(?:Users|home)/([^/\s\"'`<>${}]+)"),
    "private-key-material": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "user-access-token": re.compile(r"\bya29\.[A-Za-z0-9._~+/-]{8,}"),
    "user-refresh-token": re.compile(r"(?<!\w)1//[A-Za-z0-9._~-]{20,}"),
    "user-api-key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{25,})"),
}
TOKEN_LITERAL = re.compile(
    r"\b(?:access_token|refresh_token)[\"']?\s*[:=]\s*([\"'])([^\"'\r\n]{16,})\1", re.I
)
SYNTHETIC = re.compile(r"^(?:synthetic|fixture|fake|dummy|test)(?:$|[_:.\-])", re.I)
ENV_PRIVATE_VALUES = {"API_PASSWORD", "PANEL_PASSWORD", "RESPONSES_STATE_KEY", "LOG_ACCOUNT_TAG_KEY", "POSTGRESQL_URI", "REDIS_URL", "PROXY"}
FIXTURE_SIGNATURE_KEYS = {"signature", "thoughtsignature", "encryptedcontent"}
FIXTURE_ID_KEYS = {"requestid", "responseid", "traceid", "callid", "conversationid"}


class ScanError(Exception):
    """Only a fixed category is safe to expose, never a subprocess diagnostic."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ScanError("invalid-arguments")


def synthetic(value: str) -> bool:
    value = value.strip()
    return (
        not value or bool(SYNTHETIC.match(value))
        or bool(re.fullmatch(r"(?:REPLACE_WITH|YOUR)_[A-Z0-9_]+", value))
        or (value.startswith("<") and value.endswith(">"))
        or value in {"...", "[redacted]", "(已脱敏)", "skip_thought_signature_validator"}
    )


def private_path(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    lowered = [part.casefold() for part in parts]
    name = lowered[-1] if lowered else ""
    if any(p == ".env" or (p.startswith(".env.") and p != ".env.example") for p in lowered):
        return "private-environment-file"
    if any(p in {"creds", "credentials", "data", "request_logs", "backups", "backup"} for p in lowered[:-1]):
        return "private-data-or-backup-path"
    if any(p in {".git", ".aws", ".ssh", ".codex", ".agents", ".claude", ".kube"} for p in lowered[:-1]):
        return "private-tooling-path"
    if re.search(r"\.(?:db|sqlite3?)(?:[.-].*)?$", name):
        return "private-database-file"
    if re.search(r"\.(?:key|pem|p12|pfx|p8|gpg|asc)$", name) or name.startswith(("id_rsa", "id_ed25519")):
        return "private-key-file"
    if re.search(r"\.log(?:\..*)?$", name) or name.startswith("log.txt"):
        return "private-log-file"
    if re.search(r"\.(?:bundle|zip|tar|tgz|tbz2|txz|7z|rar|tar\.gz|tar\.bz2|tar\.xz)$", name):
        return "private-archive-file"
    if name.endswith(".private.md"):
        return "private-operation-note"
    if name in {"credentials.json", "token.json", "tokens.json", "auth.json", ".netrc", ".npmrc", ".pypirc"}:
        return "private-credential-file"
    return None


def text_categories(path: str, content: bytes) -> set[str]:
    text = content.decode("utf-8", errors="replace")
    result = set()
    for category, pattern in CONTENT_PATTERNS.items():
        for match in pattern.finditer(text):
            if category == "personal-home-path" and match.group(1) in {"user", "username", "runner"}:
                continue  # documented placeholders / a standard CI service account
            token = match.group(0).removeprefix("ya29.").removeprefix("1//").removeprefix("sk-")
            if category.startswith("user-") and synthetic(token):
                continue
            result.add(category)
    if any(not synthetic(match.group(2)) for match in TOKEN_LITERAL.finditer(text)):
        result.add("user-token-literal")
    if PurePosixPath(path).name == ".env.example":
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in ENV_PRIVATE_VALUES and not synthetic(value.strip().strip("\"'")):
                result.add("private-value-in-public-env-example")
    if path.casefold().endswith(".sse"):
        result.update(fixture_categories(text))
    return result


def fixture_categories(text: str) -> set[str]:
    result = set()

    def inspect(value, parent=""):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[_-]", "", str(key)).casefold()
                is_id = normalized in FIXTURE_ID_KEYS or (
                    normalized == "id" and (
                        parent.casefold() in {"functioncall", "functionresponse", "response"}
                        or value.get("type") in {"response", "function_call", "custom_tool_call", "message", "reasoning"}
                    )
                )
                if isinstance(child, str) and not synthetic(child):
                    if normalized in FIXTURE_SIGNATURE_KEYS:
                        result.add("fixture-nonsynthetic-signature")
                    if is_id:
                        result.add("fixture-nonsynthetic-identifier")
                inspect(child, normalized)
        elif isinstance(value, list):
            for child in value:
                inspect(child, parent)

    pending = []
    for line in text.splitlines():
        if line.startswith("id:") and not synthetic(line[3:].strip()):
            result.add("fixture-nonsynthetic-identifier")
        if line.startswith("data:"):
            value = line[5:].lstrip()
            if value == "[DONE]":
                continue
            pending.append(value)
            try:
                parsed = json.loads("\n".join(pending))
            except (ValueError, RecursionError):
                continue
            inspect(parsed)
            pending = []
        elif not line.strip() and pending:
            result.add("fixture-unparseable-data")
            pending = []
    if pending:
        result.add("fixture-unparseable-data")
    return result


@dataclass(frozen=True)
class Entry:
    path: str
    oid: str
    mode: str


class Repository:
    def __init__(self, directory: Path):
        self.directory = directory
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("GIT_"):
                self.env.pop(key)
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
        )

    def git(self, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "--no-optional-locks", *args], cwd=self.directory, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise ScanError("git-read-failed")
        return result.stdout

    def tree(self, ref: str) -> list[Entry]:
        entries = []
        for record in self.git("ls-tree", "-rz", ref).split(b"\0"):
            if record:
                header, path = record.split(b"\t", 1)
                mode, _kind, oid = header.decode("ascii").split()
                entries.append(Entry(os.fsdecode(path), oid, mode))
        return entries

    def blob(self, oid: str) -> bytes:
        if int(self.git("cat-file", "-s", oid)) > MAX_CONTENT_BYTES:
            raise ScanError("oversized-content-not-inspected")
        return self.git("cat-file", "blob", oid)


class Scanner:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.findings: set[tuple[str, str]] = set()
        self.scanned = 0
        self.commits = 0

    def report(self, path: str, category: str):
        self.findings.add((path, category))

    def allowed(self, path: str) -> bool:
        category = private_path(path)
        if category:
            self.report(path, category)
        return category is None

    def inspect(self, path: str, data: bytes):
        self.scanned += 1
        for category in text_categories(path, data):
            self.report(path, category)

    def history(self, base: str, ref: str):
        base_oid = self.repository.git("rev-parse", "--verify", base + "^{commit}").strip().decode("ascii")
        ref_oid = self.repository.git("rev-parse", "--verify", ref + "^{commit}").strip().decode("ascii")
        self.repository.git("merge-base", "--is-ancestor", base_oid, ref_oid)
        baseline = {(e.path, e.oid, e.mode) for e in self.repository.tree(base_oid)}
        seen = set(baseline)
        for commit in self.repository.git("rev-list", "--reverse", ref_oid, "--not", base_oid).splitlines():
            self.commits += 1
            for entry in self.repository.tree(commit.decode("ascii")):
                identity = (entry.path, entry.oid, entry.mode)
                if identity in seen:
                    continue
                seen.add(identity)
                if not self.allowed(entry.path):
                    continue  # NEVER open a private-path blob, even when tracked
                if entry.mode not in {"100644", "100755"}:
                    self.report(entry.path, "nonregular-git-entry-not-inspected")
                    continue
                try:
                    self.inspect(entry.path, self.repository.blob(entry.oid))
                except ScanError as exc:
                    self.report(entry.path, str(exc))

    def working_tree(self):
        root = Path(os.fsdecode(self.repository.git("rev-parse", "--show-toplevel").strip()))
        paths = self.repository.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        for raw in sorted(set(paths)):
            if not raw:
                continue
            path = os.fsdecode(raw)
            if not self.allowed(path):
                continue  # ignored private files are never enumerated or opened
            candidate = root / path
            try:
                # lstat every component: a directory symlink must not redirect reads
                for parent in [*reversed(candidate.parents), candidate]:
                    if parent == root or root not in parent.parents:
                        continue
                    if stat.S_ISLNK(parent.lstat().st_mode):
                        raise ScanError("symlink-not-inspected")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(candidate, flags)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise ScanError("nonregular-file-not-inspected")
                    if info.st_size > MAX_CONTENT_BYTES:
                        raise ScanError("oversized-content-not-inspected")
                    with os.fdopen(fd, "rb", closefd=False) as source:
                        content = source.read(MAX_CONTENT_BYTES + 1)
                    if len(content) > MAX_CONTENT_BYTES:
                        raise ScanError("oversized-content-not-inspected")
                    self.inspect(path, content)
                finally:
                    os.close(fd)
            except FileNotFoundError:
                continue  # a tracked deletion has no current content to publish
            except (OSError, ScanError) as exc:
                self.report(path, str(exc) if isinstance(exc, ScanError) else "file-read-failed")


def safe_path(path: str) -> str:
    for pattern in CONTENT_PATTERNS.values():
        path = pattern.sub("[redacted]", path)
    return json.dumps(path, ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/master", help="already-public ancestor; default origin/master")
    parser.add_argument("--ref", default="HEAD", help="outgoing ref to inspect; default HEAD")
    parser.add_argument("--working-tree", action="store_true", help="inspect tracked/nonignored working candidates INSTEAD of history")
    try:
        args = parser.parse_args(argv)
        scanner = Scanner(Repository(Path.cwd()))
        if args.working_tree:
            scanner.working_tree()
        else:
            scanner.history(args.base, args.ref)
        for path, category in sorted(scanner.findings):
            print(f"BLOCK [{category}] {safe_path(path)}")
        status = "BLOCK" if scanner.findings else "PASS"
        print(f"{status} [publication-preflight] files={scanner.scanned} outgoing_commits={scanner.commits} findings={len(scanner.findings)}")
        return 1 if scanner.findings else 0
    except (ScanError, OSError, UnicodeError, ValueError, RecursionError):
        print("ERROR [publication-scan-incomplete]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
