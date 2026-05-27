from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import Max
from django.utils import timezone

from ledger.models import FinancialEvent, InterestAccrualRun, InterestRatePeriod, LedgerTransaction, Obligation
from ledger.services.balances import get_obligation_balance
from ledger.services.events import _post_debt_increase, post_interest_reversal
from ledger.services.recurring import next_month_start


FIXED_DAYS_IN_YEAR = Decimal('365')


def calculate_monthly_interest(obligation, period_start):
    period_start = period_start.replace(day=1)
    period_end = next_month_start(period_start)
    total_interest_units = Decimal('0')
    daily_details = []

    current_date = period_start
    while current_date < period_end:
        balance_units = get_obligation_balance(obligation, as_of=current_date)
        annual_rate_percent = _rate_for_date(obligation, current_date)
        daily_interest_units = (
            Decimal(balance_units)
            * (annual_rate_percent / Decimal('100'))
            / FIXED_DAYS_IN_YEAR
        )
        total_interest_units += daily_interest_units
        daily_details.append(
            {
                'date': current_date.isoformat(),
                'balance_units': balance_units,
                'annual_rate_percent': str(annual_rate_percent),
                'interest_units': str(daily_interest_units),
            }
        )
        current_date += timedelta(days=1)

    amount_units = int(total_interest_units.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return {
        'period_start': period_start,
        'period_end': period_end,
        'amount_units': amount_units,
        'daily_details': daily_details,
    }


def post_monthly_interest(obligation, period_start):
    calculation = calculate_monthly_interest(obligation, period_start)
    amount_units = calculation['amount_units']
    if amount_units <= 0:
        raise ValidationError('Calculated interest must be greater than zero to post.')

    period_start = calculation['period_start']
    period_end = calculation['period_end']

    with db_transaction.atomic():
        existing = InterestAccrualRun.objects.filter(
            obligation=obligation,
            period_start=period_start,
            period_end=period_end,
            status=InterestAccrualRun.Status.POSTED,
        ).first()
        if existing:
            return existing

        revision = _next_interest_revision(obligation, period_start, period_end)
        idempotency_key = f'interest:{obligation.pk}:{period_start.isoformat()}:v{revision}'
        ledger_transaction = _post_debt_increase(
            obligation=obligation,
            amount_units=amount_units,
            event_date=period_end,
            event_type=FinancialEvent.EventType.INTEREST_POSTING,
            source=FinancialEvent.Source.SYSTEM,
            memo=f'Interest for {period_start.isoformat()} to {period_end.isoformat()}',
            period_start=period_start,
            period_end=period_end,
            idempotency_key=idempotency_key,
        )
        run = InterestAccrualRun(
            obligation=obligation,
            period_start=period_start,
            period_end=period_end,
            posted_on=period_end,
            revision=revision,
            calculated_interest_amount_units=amount_units,
            ledger_transaction=ledger_transaction,
            status=InterestAccrualRun.Status.POSTED,
            calculation_payload={
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat(),
                'amount_units': amount_units,
                'daily_details': calculation['daily_details'],
            },
        )
        run.full_clean()
        run.save()
        return run


def generate_due_interest(obligation, through_date=None, from_date=None):
    through_date = through_date or timezone.localdate()
    if obligation.status != Obligation.Status.OPEN:
        return []

    first_period_start = (from_date or obligation.opened_on).replace(day=1)
    last_period_start = previous_month_start(through_date)
    if first_period_start > last_period_start:
        return []

    posted_runs = []
    for period_start in iter_month_starts(first_period_start, last_period_start):
        if _posted_interest_run(obligation, period_start):
            continue
        calculation = calculate_monthly_interest(obligation, period_start)
        if calculation['amount_units'] <= 0:
            continue
        posted_runs.append(post_monthly_interest(obligation, period_start))
    return posted_runs


def recalculate_interest_from(obligation, from_date, through_date=None):
    through_date = through_date or timezone.localdate()
    first_period_start = from_date.replace(day=1)
    last_period_start = previous_month_start(through_date)
    result = {
        'reversed_runs': [],
        'reversal_transactions': [],
        'posted_runs': [],
    }
    if first_period_start > last_period_start:
        return result

    with db_transaction.atomic():
        runs_to_reverse = list(
            InterestAccrualRun.objects.select_for_update()
            .filter(
                obligation=obligation,
                period_start__gte=first_period_start,
                period_start__lte=last_period_start,
                status=InterestAccrualRun.Status.POSTED,
            )
            .order_by('period_start', 'revision')
        )
        for run in runs_to_reverse:
            reversal_transaction = reverse_interest_run(run)
            result['reversed_runs'].append(run)
            result['reversal_transactions'].append(reversal_transaction)

        for period_start in iter_month_starts(first_period_start, last_period_start):
            calculation = calculate_monthly_interest(obligation, period_start)
            if calculation['amount_units'] <= 0:
                continue
            result['posted_runs'].append(post_monthly_interest(obligation, period_start))

    return result


def reverse_interest_run(run):
    if run.status != InterestAccrualRun.Status.POSTED:
        raise ValidationError('Only posted interest runs can be reversed.')

    with db_transaction.atomic():
        reversal_transaction = post_interest_reversal(
            obligation=run.obligation,
            amount_units=run.calculated_interest_amount_units,
            event_date=run.posted_on,
            memo=f'Reverse interest for {run.period_start.isoformat()} to {run.period_end.isoformat()}',
            period_start=run.period_start,
            period_end=run.period_end,
            idempotency_key=f'interest-reversal:{run.pk}',
        )
        payload = dict(run.calculation_payload or {})
        payload['voided_by_transaction_id'] = reversal_transaction.pk
        payload['voided_reason'] = 'interest_recalculation'
        run.status = InterestAccrualRun.Status.VOIDED
        run.calculation_payload = payload
        run.save(update_fields=['status', 'calculation_payload', 'updated_at'])
        return reversal_transaction


def iter_month_starts(first_period_start, last_period_start):
    current = first_period_start.replace(day=1)
    last_period_start = last_period_start.replace(day=1)
    while current <= last_period_start:
        yield current
        current = next_month_start(current)


def previous_month_start(target_date):
    first_day_this_month = target_date.replace(day=1)
    return (first_day_this_month - timedelta(days=1)).replace(day=1)


def _rate_for_date(obligation, target_date):
    rate = (
        InterestRatePeriod.objects.filter(
            obligation=obligation,
            effective_from__lte=target_date,
        )
        .filter(_rate_valid_after(target_date))
        .order_by('-effective_from')
        .first()
    )
    if rate is None:
        return Decimal('0')
    return rate.annual_rate_percent


def _rate_valid_after(target_date):
    from django.db.models import Q

    return Q(effective_to__isnull=True) | Q(effective_to__gte=target_date)


def _posted_interest_run(obligation, period_start):
    return InterestAccrualRun.objects.filter(
        obligation=obligation,
        period_start=period_start.replace(day=1),
        status=InterestAccrualRun.Status.POSTED,
    ).first()


def _next_interest_revision(obligation, period_start, period_end):
    max_revision = (
        InterestAccrualRun.objects.filter(
            obligation=obligation,
            period_start=period_start,
            period_end=period_end,
        )
        .aggregate(max_revision=Max('revision'))
        .get('max_revision')
    )
    return (max_revision or 0) + 1
