from dataclasses import dataclass, field
from datetime import datetime
from contextlib import closing
from pathlib import Path
import sqlite3


@dataclass
class SqliteBackupResult:
    source_path: Path
    backup_path: Path
    integrity_check: str
    size_bytes: int
    deleted_paths: list[Path] = field(default_factory=list)


def create_sqlite_backup(source_path, output_dir, prefix='trusttrack', keep=None, now=None):
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    if not source_path.exists():
        raise FileNotFoundError(f'SQLite database does not exist: {source_path}')

    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_path(output_dir, prefix, now=now)
    temporary_path = backup_path.with_name(f'.{backup_path.name}.tmp')
    if temporary_path.exists():
        temporary_path.unlink()

    try:
        source_uri = source_path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(temporary_path)) as backup_connection:
                source_connection.backup(backup_connection)

        integrity_check = verify_sqlite_database(temporary_path)
        if integrity_check != 'ok':
            raise RuntimeError(f'Backup integrity check failed: {integrity_check}')

        temporary_path.replace(backup_path)
        deleted_paths = prune_sqlite_backups(output_dir, prefix=prefix, keep=keep)
        return SqliteBackupResult(
            source_path=source_path,
            backup_path=backup_path,
            integrity_check=integrity_check,
            size_bytes=backup_path.stat().st_size,
            deleted_paths=deleted_paths,
        )
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def verify_sqlite_database(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute('PRAGMA integrity_check').fetchone()
    return row[0] if row else 'missing integrity check result'


def prune_sqlite_backups(output_dir, prefix='trusttrack', keep=None):
    if keep is None:
        return []

    backups = sorted(Path(output_dir).glob(f'{prefix}-*.sqlite3'), reverse=True)
    deleted_paths = []
    for backup_path in backups[keep:]:
        backup_path.unlink()
        deleted_paths.append(backup_path)
    return deleted_paths


def _backup_path(output_dir, prefix, now=None):
    now = now or datetime.now().astimezone()
    timestamp = now.strftime('%Y%m%d-%H%M%S')
    backup_path = output_dir / f'{prefix}-{timestamp}.sqlite3'
    counter = 2
    while backup_path.exists():
        backup_path = output_dir / f'{prefix}-{timestamp}-{counter}.sqlite3'
        counter += 1
    return backup_path
