#!/usr/bin/env python3
"""Private, verified rollback snapshots. Stop the actual service before create.

Restore only stages a snapshot in a new directory; it never overwrites a live
checkout, credentials directory, or database. Activate it after stopping the
new version and preserving its writes. No environment values are printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tarfile


class OperationError(RuntimeError):
    """An operator-safe diagnostic that never contains source values or paths."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def private_new_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    path.chmod(0o700)


def create(args) -> None:
    if not args.service_stopped:
        raise OperationError("Stop the actual supervisor and all writers before creating a snapshot")
    project = args.project.resolve(strict=True)
    credentials = args.credentials_dir.resolve(strict=True)
    output = args.output.absolute()
    sources = {"project": project}
    if not credentials.is_relative_to(project):
        sources["credentials"] = credentials
    for index, name in enumerate(args.env_file):
        path = name.resolve(strict=True)
        if not path.is_file():
            raise OperationError("Environment sources must be regular files")
        if not path.is_relative_to(project) and not path.is_relative_to(credentials):
            sources[f"environment-{index}"] = path
    if args.database == "postgresql":
        if args.external_database_backup is None:
            raise OperationError("PostgreSQL requires a separately verified external database backup")
        sources["external-database"] = args.external_database_backup.resolve(strict=True)
        if not sources["external-database"].is_file():
            raise OperationError("External database backup must be a file")
    elif args.external_database_backup is not None:
        raise OperationError("External database backup is only valid for PostgreSQL mode")
    for path in sources.values():
        if output.resolve().is_relative_to(path):
            raise OperationError("Snapshot destination must be outside every source")
    private_new_directory(output)
    manifest = {
        "format": 1, "database": args.database,
        "service_stopped_asserted": True,
        "sources": {name: str(path) for name, path in sources.items()},
        "credentials_path": str(credentials),
        "environment_paths": [str(path.resolve()) for path in args.env_file],
        "restoration": "stage_only; stop service and preserve new writes before activation",
    }
    # SQLite backup API makes a consistent standalone DB even if a prior clean
    # shutdown left WAL files. The original complete directory is also archived.
    if args.database == "sqlite":
        database = credentials / "credentials.db"
        if not database.is_file():
            raise OperationError("Expected SQLite credentials.db was not found")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(output / "credentials.sqlite") as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise OperationError("SQLite snapshot integrity check failed")
        (output / "credentials.sqlite").chmod(0o600)
        manifest["sqlite_sha256"] = digest(output / "credentials.sqlite")

    archive_path = output / "snapshot.tar.gz"
    def archive_filter(info):
        if info.isfile() or info.isdir() or info.issym() or info.islnk():
            return info
        # Private supervisor sockets and similar transient IPC are not state.
        return None
    with tarfile.open(archive_path, "w:gz", dereference=False) as archive:
        for name, path in sources.items():
            archive.add(path, arcname=name, filter=archive_filter)
    archive_path.chmod(0o600)
    manifest["archive_sha256"] = digest(archive_path)
    target = output / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    target.chmod(0o600)
    verify(output)
    print(json.dumps({"snapshot_complete": True, "database": args.database, "source_count": len(sources)}))


def verify(folder: Path) -> dict:
    manifest_path = folder / "manifest.json"
    if not folder.is_dir() or not manifest_path.is_file() or folder.is_symlink() or manifest_path.is_symlink():
        raise OperationError("Snapshot and manifest must not be symlinks")
    if os.name == "posix" and any(path.stat().st_mode & 0o077 for path in (folder, manifest_path)):
        raise OperationError("Snapshot directory and manifest must be private")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != 1:
        raise OperationError("Unsupported snapshot format")
    paths = [(folder / "snapshot.tar.gz", "archive_sha256")]
    if manifest.get("database") == "sqlite":
        paths.append((folder / "credentials.sqlite", "sqlite_sha256"))
    for path, key in paths:
        if not path.is_file() or path.is_symlink() or digest(path) != manifest.get(key):
            raise OperationError("Snapshot checksum mismatch")
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise OperationError("Snapshot files must be private")
    return manifest


def restore(folder: Path, target: Path) -> None:
    manifest = verify(folder)
    private_new_directory(target)
    with tarfile.open(folder / "snapshot.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        links = {member.name for member in members if member.issym() or member.islnk()}
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in manifest["sources"]:
                raise OperationError("Unexpected archive path")
            if any(str(parent) in links for parent in path.parents):
                raise OperationError("Archive traverses a symlink")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise OperationError("Unsupported archive entry")
        # Extract files before creating symlinks (venv interpreter links may
        # legitimately be absolute); no write can follow an extracted symlink.
        for member in members:
            if member.isfile() or member.isdir():
                archive.extract(member, target, filter="data")
                (target / member.name).chmod(0o700 if member.isdir() else 0o600 | (member.mode & 0o100))
        for member in members:
            path = target / member.name
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if member.issym():
                path.symlink_to(member.linkname)
            elif member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts or str(link) in links or any(str(parent) in links for parent in link.parents):
                    raise OperationError("Unsafe archive hardlink")
                os.link(target / str(link), path)
    if manifest["database"] == "sqlite":
        import shutil
        shutil.copyfile(folder / "credentials.sqlite", target / "credentials.sqlite")
        (target / "credentials.sqlite").chmod(0o600)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (target / "manifest.json").chmod(0o600)
    print(json.dumps({"restored_to_staging": True, "live_files_changed": False}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("create")
    new.add_argument("--project", type=Path, required=True)
    new.add_argument("--credentials-dir", type=Path, required=True)
    new.add_argument("--env-file", type=Path, action="append", default=[])
    new.add_argument("--database", choices=("sqlite", "postgresql"), required=True)
    new.add_argument("--external-database-backup", type=Path)
    new.add_argument("--output", type=Path, required=True)
    new.add_argument("--service-stopped", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("--snapshot", type=Path, required=True)
    old = commands.add_parser("restore")
    old.add_argument("--snapshot", type=Path, required=True)
    old.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "create":
            create(args)
        elif args.command == "restore":
            restore(args.snapshot, args.destination)
        else:
            verify(args.snapshot)
            print(json.dumps({"snapshot_verified": True}))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError) as exc:
        print("Snapshot operation failed: " + (str(exc) if isinstance(exc, OperationError) else type(exc).__name__))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
