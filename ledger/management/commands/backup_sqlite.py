import time
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ledger.services.backups import create_sqlite_backup


class Command(BaseCommand):
    help = 'Create dated SQLite backups for TrustTrack.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Create one backup and exit.')
        parser.add_argument('--interval', type=int, default=86400, help='Seconds between automatic backups.')
        parser.add_argument('--keep', type=int, default=30, help='Number of newest backups to keep.')
        parser.add_argument('--output-dir', help='Backup directory. Defaults to BASE_DIR/backups.')
        parser.add_argument('--database-path', help='SQLite database path. Defaults to the Django default database.')

    def handle(self, *args, **options):
        interval = options['interval']
        keep = options['keep']
        if interval <= 0:
            raise CommandError('Interval must be greater than zero.')
        if keep <= 0:
            raise CommandError('Keep must be greater than zero.')

        if options['once']:
            self._run_once(options)
            return

        self.stdout.write(self.style.SUCCESS('TrustTrack SQLite backup scheduler started. Press Ctrl+C to stop.'))
        try:
            while True:
                try:
                    self._run_once(options)
                except Exception as error:
                    self.stderr.write(f'SQLite backup failed: {error}')
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write('Stopping TrustTrack SQLite backup scheduler...')

    def _run_once(self, options):
        source_path = self._database_path(options)
        output_dir = Path(
            options['output_dir']
            or os.environ.get('TRUSTTRACK_BACKUP_DIR')
            or Path(settings.BASE_DIR) / 'backups'
        )
        result = create_sqlite_backup(
            source_path=source_path,
            output_dir=output_dir,
            keep=options['keep'],
        )
        self.stdout.write(
            (
                f'Backup created: {result.backup_path} '
                f'({result.size_bytes} bytes, integrity: {result.integrity_check}). '
                f'Pruned {len(result.deleted_paths)} old backup(s).'
            )
        )

    def _database_path(self, options):
        if options['database_path']:
            return Path(options['database_path'])

        database_settings = settings.DATABASES['default']
        if database_settings['ENGINE'] != 'django.db.backends.sqlite3':
            raise CommandError('The default database is not SQLite.')
        return Path(database_settings['NAME'])
