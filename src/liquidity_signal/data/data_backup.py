from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DATABASES = (
    "runtime/liquidation_history.db",
    "runtime/training_forward.db",
    "runtime/vision_metrics.db",
    "runtime/onchain_data.db",
    "runtime/cross_venue.db",
)
DEFAULT_ARTIFACTS = (
    "runtime/daily_refresh_status.json",
    "runtime/data_completeness_forward.json",
    "runtime/data_completeness_archive.json",
    "runtime/data_completeness_archive_hyperliquid.json",
    "runtime/data_completeness_archive_cross_venue.json",
    "runtime/data_completeness_archive_cross_venue_hyperliquid.json",
    "runtime/data_operations_health.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _online_sqlite_backup(source_path: Path, destination_path: Path) -> dict[str, Any]:
    source = sqlite3.connect(source_path, timeout=60.0)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination, pages=4096, sleep=0.05)
        check = destination.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {source_path}: {check}")
        tables = destination.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        destination.close()
        source.close()
    return {
        "kind": "sqlite",
        "source": str(source_path),
        "file": destination_path.name,
        "bytes": destination_path.stat().st_size,
        "sha256": _sha256(destination_path),
        "quick_check": "ok",
        "tables": tables,
    }


def prune_backups(output_dir: str, retain: int) -> list[str]:
    """Remove only older archives produced by this module."""
    if retain < 1:
        raise ValueError("retain must be at least one")
    root = Path(output_dir)
    archives = sorted(
        root.glob("data-backup-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    removed = []
    for path in archives[retain:]:
        path.unlink()
        removed.append(str(path))
    return removed


def create_data_backup(
    *,
    database_paths: list[str] | None = None,
    artifact_paths: list[str] | None = None,
    output_dir: str = "runtime/backups",
    retain: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create, verify, and rotate a consistent compressed data backup."""
    started = time.monotonic()
    created_at = now or datetime.now(timezone.utc)
    stamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    final_path = root / f"data-backup-{stamp}.zip"
    if final_path.exists():
        final_path = root / f"data-backup-{stamp}-{created_at.microsecond:06d}.zip"
    temporary_archive = root / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        selected_databases = (
            database_paths if database_paths is not None else list(DEFAULT_DATABASES)
        )
        for raw_path in selected_databases:
            source_path = Path(raw_path)
            if not source_path.exists():
                missing.append(str(source_path))
                continue
            destination = staging / source_path.name
            entries.append(_online_sqlite_backup(source_path, destination))

        selected_artifacts = (
            artifact_paths if artifact_paths is not None else list(DEFAULT_ARTIFACTS)
        )
        for raw_path in selected_artifacts:
            source_path = Path(raw_path)
            if not source_path.exists():
                missing.append(str(source_path))
                continue
            destination = staging / source_path.name
            shutil.copy2(source_path, destination)
            entries.append({
                "kind": "artifact",
                "source": str(source_path),
                "file": destination.name,
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
            })

        if not any(entry["kind"] == "sqlite" for entry in entries):
            raise RuntimeError("No SQLite databases were available to back up.")
        manifest = {
            "version": 1,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "entries": entries,
            "missing_optional_paths": missing,
        }
        manifest_path = staging / "backup_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with zipfile.ZipFile(
            temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(staging.iterdir()):
                archive.write(path, arcname=path.name)
        temporary_archive.replace(final_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if temporary_archive.exists():
            temporary_archive.unlink()

    verification = verify_data_backup(str(final_path))
    if not verification["valid"]:
        raise RuntimeError(f"Backup verification failed: {verification['errors']}")
    removed = prune_backups(output_dir, retain)
    return {
        "status": "complete",
        "archive": str(final_path),
        "archive_bytes": final_path.stat().st_size,
        "created_at": manifest["created_at"],
        "duration_seconds": round(time.monotonic() - started, 3),
        "entries": entries,
        "missing_optional_paths": missing,
        "verification": verification,
        "retained_generations": retain,
        "removed_archives": removed,
    }


def verify_data_backup(archive_path: str) -> dict[str, Any]:
    """Verify checksums and SQLite integrity by restoring into an isolated directory."""
    path = Path(archive_path)
    errors: list[str] = []
    checked: list[dict[str, Any]] = []
    if not path.exists():
        return {"valid": False, "archive": str(path), "checked": [], "errors": ["missing"]}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if "backup_manifest.json" not in names:
                return {
                    "valid": False,
                    "archive": str(path),
                    "checked": [],
                    "errors": ["backup_manifest.json is missing"],
                }
            manifest = json.loads(archive.read("backup_manifest.json"))
            with tempfile.TemporaryDirectory(prefix="abyss-backup-verify-") as temp_dir:
                temp_root = Path(temp_dir)
                for entry in manifest.get("entries", []):
                    name = str(entry.get("file") or "")
                    if not name or Path(name).name != name or name not in names:
                        errors.append(f"Invalid or missing archive member: {name}")
                        continue
                    restored = temp_root / name
                    with archive.open(name) as source, restored.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    actual_hash = _sha256(restored)
                    if actual_hash != entry.get("sha256"):
                        errors.append(f"Checksum mismatch: {name}")
                    check = None
                    if entry.get("kind") == "sqlite":
                        conn = sqlite3.connect(restored)
                        try:
                            check = conn.execute("PRAGMA quick_check").fetchone()[0]
                        finally:
                            conn.close()
                        if check != "ok":
                            errors.append(f"SQLite quick_check failed: {name}: {check}")
                    checked.append({"file": name, "sha256": actual_hash, "quick_check": check})
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        errors.append(f"Unreadable backup: {exc}")
    return {
        "valid": not errors,
        "archive": str(path),
        "checked": checked,
        "errors": errors,
    }


def restore_data_backup(archive_path: str, target_dir: str) -> dict[str, Any]:
    """Restore to a new directory atomically; never overwrite live data."""
    archive_path_obj = Path(archive_path)
    target = Path(target_dir)
    if target.exists():
        raise FileExistsError(f"Restore target already exists: {target}")
    verification = verify_data_backup(archive_path)
    if not verification["valid"]:
        raise RuntimeError(f"Backup verification failed: {verification['errors']}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
    staging.mkdir()
    restored: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(archive_path_obj, "r") as archive:
            manifest_bytes = archive.read("backup_manifest.json")
            manifest = json.loads(manifest_bytes)
            (staging / "backup_manifest.json").write_bytes(manifest_bytes)
            for entry in manifest.get("entries", []):
                name = str(entry["file"])
                if Path(name).name != name:
                    raise RuntimeError(f"Unsafe archive member: {name}")
                destination = staging / name
                with archive.open(name) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                actual_hash = _sha256(destination)
                if actual_hash != entry["sha256"]:
                    raise RuntimeError(f"Checksum mismatch during restore: {name}")
                restored.append({
                    "file": name,
                    "bytes": destination.stat().st_size,
                    "sha256": actual_hash,
                })
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "complete",
        "archive": str(archive_path_obj),
        "target_dir": str(target),
        "restored": restored,
        "verification": verification,
        "note": "Restored to an isolated directory; no live database was overwritten.",
    }
