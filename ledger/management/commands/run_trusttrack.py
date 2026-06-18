from dataclasses import dataclass, field
from pathlib import Path
import os
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    cwd: Path
    process: subprocess.Popen | None = None
    restart_times: list[float] = field(default_factory=list)

    def start(self):
        self.process = subprocess.Popen(self.command, cwd=self.cwd)

    def poll(self):
        if self.process is None:
            return None
        return self.process.poll()

    def stop(self, timeout=10):
        if self.process is None or self.process.poll() is not None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)


class Command(BaseCommand):
    help = 'Run the local TrustTrack site, Telegram bot, due job scheduler, and backups together.'

    def add_arguments(self, parser):
        parser.add_argument('--host', default='127.0.0.1', help='Local address for the Django site.')
        parser.add_argument('--port', type=int, default=8005, help='Local port for the Django site.')
        parser.add_argument(
            '--site-runner',
            choices=['runserver', 'gunicorn'],
            default=os.environ.get('TRUSTTRACK_SITE_RUNNER', 'runserver'),
            help='Process used for the Django site.',
        )
        parser.add_argument('--gunicorn-workers', type=int, default=2, help='Gunicorn worker count.')
        parser.add_argument('--no-site', action='store_true', help='Do not start the Django development site.')
        parser.add_argument('--no-bot', action='store_true', help='Do not start the Telegram bot.')
        parser.add_argument('--no-scheduler', action='store_true', help='Do not start the due job scheduler.')
        parser.add_argument('--no-backups', action='store_true', help='Do not start the SQLite backup scheduler.')
        parser.add_argument('--scheduler-interval', type=int, default=3600, help='Seconds between due job scheduler runs.')
        parser.add_argument('--backup-interval', type=int, default=86400, help='Seconds between SQLite backups.')
        parser.add_argument('--backup-keep', type=int, default=30, help='Number of newest SQLite backups to keep.')
        parser.add_argument('--backup-dir', help='Directory for SQLite backups. Defaults to BASE_DIR/backups.')
        parser.add_argument('--restart-delay', type=float, default=5.0, help='Seconds to wait before restarting a crashed child process.')
        parser.add_argument('--max-restarts', type=int, default=5, help='Maximum restarts inside the restart window.')
        parser.add_argument('--restart-window', type=float, default=60.0, help='Restart window in seconds.')

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        manage_py = base_dir / 'manage.py'
        services = []

        if not options['no_site']:
            site_command = self._site_command(manage_py, options)
            services.append(
                ManagedProcess(
                    name='site',
                    command=site_command,
                    cwd=base_dir,
                )
            )

        if not options['no_bot']:
            services.append(
                ManagedProcess(
                    name='telegram bot',
                    command=[sys.executable, str(manage_py), 'telegram_bot'],
                    cwd=base_dir,
                )
            )

        if not options['no_scheduler']:
            services.append(
                ManagedProcess(
                    name='due job scheduler',
                    command=[
                        sys.executable,
                        str(manage_py),
                        'run_due_jobs',
                        '--interval',
                        str(options['scheduler_interval']),
                    ],
                    cwd=base_dir,
                )
            )

        if not options['no_backups']:
            command = [
                sys.executable,
                str(manage_py),
                'backup_sqlite',
                '--interval',
                str(options['backup_interval']),
                '--keep',
                str(options['backup_keep']),
            ]
            if options['backup_dir']:
                command.extend(['--output-dir', options['backup_dir']])
            services.append(
                ManagedProcess(
                    name='SQLite backup scheduler',
                    command=command,
                    cwd=base_dir,
                )
            )

        if not services:
            raise CommandError('Nothing to run. Remove --no-site, --no-bot, --no-scheduler, or --no-backups.')

        for service in services:
            self.stdout.write(f'Starting {service.name}...')
            service.start()

        self.stdout.write(self.style.SUCCESS('TrustTrack local stack is running. Press Ctrl+C to stop.'))
        self.stdout.write(f'Site URL: http://{options["host"]}:{options["port"]}/')

        try:
            while True:
                for service in services:
                    return_code = service.poll()
                    if return_code is None:
                        continue
                    self._restart_service(service, return_code, options)
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write('Stopping TrustTrack local stack...')
        finally:
            for service in reversed(services):
                service.stop()

    def _site_command(self, manage_py, options):
        if options['site_runner'] == 'gunicorn':
            return [
                sys.executable,
                '-m',
                'gunicorn',
                'trusttrack.wsgi:application',
                '--bind',
                f'{options["host"]}:{options["port"]}',
                '--workers',
                str(options['gunicorn_workers']),
                '--access-logfile',
                '-',
                '--error-logfile',
                '-',
            ]
        return [
            sys.executable,
            str(manage_py),
            'runserver',
            f'{options["host"]}:{options["port"]}',
            '--noreload',
        ]

    def _restart_service(self, service, return_code, options):
        now = time.monotonic()
        service.restart_times = [
            restart_time
            for restart_time in service.restart_times
            if now - restart_time <= options['restart_window']
        ]
        if len(service.restart_times) >= options['max_restarts']:
            raise CommandError(
                f'{service.name} exited too often. Last return code: {return_code}. '
                'Stopping local stack.'
            )

        service.restart_times.append(now)
        self.stderr.write(
            f'{service.name} exited with code {return_code}. '
            f'Restarting in {options["restart_delay"]} seconds...'
        )
        time.sleep(options['restart_delay'])
        service.start()
