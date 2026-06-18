import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from ledger.services.due_jobs import run_due_jobs


class Command(BaseCommand):
    help = 'Generate due recurring events and interest postings.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Run due jobs once and exit.')
        parser.add_argument('--interval', type=int, default=3600, help='Seconds between scheduler runs.')
        parser.add_argument('--date', help='Run through this date in YYYY-MM-DD format. Defaults to today.')

    def handle(self, *args, **options):
        interval = options['interval']
        if interval <= 0:
            raise CommandError('Interval must be greater than zero.')

        fixed_date = None
        if options['date']:
            fixed_date = parse_date(options['date'])
            if fixed_date is None:
                raise CommandError('Date must use YYYY-MM-DD format.')

        if options['once']:
            self._run_once(fixed_date)
            return

        self.stdout.write(self.style.SUCCESS('TrustTrack due job scheduler started. Press Ctrl+C to stop.'))
        try:
            while True:
                self._run_once(fixed_date)
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write('Stopping TrustTrack due job scheduler...')

    def _run_once(self, fixed_date):
        through_date = fixed_date or timezone.localdate()
        result = run_due_jobs(through_date=through_date)
        self.stdout.write(
            (
                f'Due jobs through {result.through_date}: checked {result.obligation_count} open obligation(s), '
                f'generated {result.recurring_created} recurring event(s), '
                f'posted {result.interest_posted} interest month(s), '
                f'with {result.error_count} error(s).'
            )
        )
        for obligation_result in result.obligation_results:
            for error in obligation_result.errors:
                self.stderr.write(
                    f'Obligation #{obligation_result.obligation_id} ({obligation_result.title}): {error}'
                )
