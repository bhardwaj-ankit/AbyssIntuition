from __future__ import annotations

import sqlite3
import zipfile
from datetime import datetime, timezone

import pytest

from liquidity_signal.data.data_backup import (
    create_data_backup,
    prune_backups,
    restore_data_backup,
    verify_data_backup,
)


def _database(path, value: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples(value TEXT)")
    conn.execute("INSERT INTO samples VALUES(?)", (value,))
    conn.commit()
    conn.close()


def test_online_backup_is_checksummed_and_restorable(tmp_path) -> None:
    source = tmp_path / "live.db"
    artifact = tmp_path / "health.json"
    output = tmp_path / "backups"
    _database(source, "irreplaceable")
    artifact.write_text('{"healthy":true}', encoding="utf-8")

    result = create_data_backup(
        database_paths=[str(source)],
        artifact_paths=[str(artifact)],
        output_dir=str(output),
        now=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )

    assert result["status"] == "complete"
    assert result["verification"]["valid"] is True
    assert verify_data_backup(result["archive"])["valid"] is True
    with zipfile.ZipFile(result["archive"]) as archive:
        assert {"live.db", "health.json", "backup_manifest.json"} <= set(archive.namelist())

    restored = restore_data_backup(result["archive"], str(tmp_path / "restored"))
    restored_db = sqlite3.connect(tmp_path / "restored" / "live.db")
    try:
        assert restored_db.execute("SELECT value FROM samples").fetchone()[0] == "irreplaceable"
    finally:
        restored_db.close()
    assert restored["status"] == "complete"


def test_restore_refuses_to_overwrite_existing_directory(tmp_path) -> None:
    source = tmp_path / "live.db"
    _database(source, "safe")
    backup = create_data_backup(
        database_paths=[str(source)],
        artifact_paths=[],
        output_dir=str(tmp_path / "backups"),
    )
    target = tmp_path / "existing"
    target.mkdir()

    with pytest.raises(FileExistsError):
        restore_data_backup(backup["archive"], str(target))

    assert list(target.iterdir()) == []


def test_backup_rotation_only_removes_matching_old_generations(tmp_path) -> None:
    for index in range(4):
        path = tmp_path / f"data-backup-2026080{index}T000000Z.zip"
        path.write_bytes(b"backup")
        path.touch()
    unrelated = tmp_path / "keep-me.zip"
    unrelated.write_bytes(b"unrelated")

    removed = prune_backups(str(tmp_path), retain=2)

    assert len(removed) == 2
    assert unrelated.exists()
    assert len(list(tmp_path.glob("data-backup-*.zip"))) == 2
