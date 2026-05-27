from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from ledger.models import FinancialEvent, InterestAccrualRun, InterestRatePeriod, LedgerTransaction
from ledger.services.balances import get_obligation_balance
from ledger.services.events import _post_debt_increase
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
    idempotency_key = f'interest:{obligation.pk}:{period_start.isoformat()}'

    with db_transaction.atomic():
        existing = InterestAccrualRun.objects.filter(
            obligation=obligation,
            period_start=period_start,
            period_end=period_end,
            status=InterestAccrualRun.Status.POSTED,
        ).first()
        if existing:
            return existing

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
