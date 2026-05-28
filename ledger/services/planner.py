from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from ledger.models import EventSeries, EventSeriesVersion, FinancialEvent, InterestRatePeriod, Obligation
from ledger.services.balances import get_obligation_balance
from ledger.services.recurring import _occurrences_for_month, next_month_start


FIXED_DAYS_IN_YEAR = Decimal('365')


def build_portfolio_projection(obligations, user, months=12, start_date=None):
    start_date = start_date or timezone.localdate()
    obligations = list(obligations)
    obligation_projections = {
        obligation.pk: project_obligation(obligation, months=months, start_date=start_date)
        for obligation in obligations
    }
    points = []
    for index in range(months + 1):
        i_owe_units = 0
        owed_to_me_units = 0
        point_date = start_date
        for obligation in obligations:
            projection = obligation_projections[obligation.pk]
            point = projection['points'][index]
            point_date = point['date']
            balance_units = point['balance_units']
            if obligation.borrower_id == user.id:
                i_owe_units += balance_units
            else:
                owed_to_me_units += balance_units
        points.append(
            {
                'date': point_date,
                'label': point_date.strftime('%b %Y'),
                'i_owe_units': i_owe_units,
                'owed_to_me_units': owed_to_me_units,
                'net_units': owed_to_me_units - i_owe_units,
            }
        )

    rows = []
    for obligation in obligations:
        projection = obligation_projections[obligation.pk]
        rows.append(
            {
                'obligation': obligation,
                'role': 'borrower' if obligation.borrower_id == user.id else 'creditor',
                'current_balance_units': projection['points'][0]['balance_units'],
                'projected_balance_units': projection['points'][-1]['balance_units'],
                'payoff_date': projection['payoff_date'],
                'payoff_label': _payoff_label(projection['payoff_date'], months),
            }
        )

    return {
        'start_date': start_date,
        'months': months,
        'points': points,
        'rows': rows,
        'current_i_owe_units': points[0]['i_owe_units'] if points else 0,
        'current_owed_to_me_units': points[0]['owed_to_me_units'] if points else 0,
        'current_net_units': points[0]['net_units'] if points else 0,
        'projected_i_owe_units': points[-1]['i_owe_units'] if points else 0,
        'projected_owed_to_me_units': points[-1]['owed_to_me_units'] if points else 0,
        'projected_net_units': points[-1]['net_units'] if points else 0,
    }


def simulate_monthly_payment(obligation, monthly_payment_units, payment_day=1, months=60, start_date=None):
    return project_obligation(
        obligation,
        months=months,
        start_date=start_date,
        override_monthly_payment_units=monthly_payment_units,
        override_payment_day=payment_day,
        replace_scheduled_repayments=True,
    )


def project_obligation(
    obligation,
    months=12,
    start_date=None,
    override_monthly_payment_units=None,
    override_payment_day=1,
    replace_scheduled_repayments=False,
):
    start_date = start_date or timezone.localdate()
    balance_units = max(get_obligation_balance(obligation, as_of=start_date), 0)
    points = [{'date': start_date, 'balance_units': balance_units, 'interest_units': 0}]
    payoff_date = start_date if balance_units == 0 else None
    current_day = start_date + timedelta(days=1)
    period_start = start_date.replace(day=1)

    for _ in range(months):
        period_end = next_month_start(period_start)
        interest_units = Decimal('0')
        planned_events = _planned_events_by_date(obligation, period_start, period_end, replace_scheduled_repayments)
        while current_day < period_end:
            if current_day >= start_date:
                previous_balance_units = balance_units
                balance_units = _apply_planned_events(balance_units, planned_events.get(current_day, []))
                if previous_balance_units > 0 and balance_units == 0 and payoff_date is None:
                    payoff_date = current_day
                if _is_override_payment_day(current_day, override_payment_day) and override_monthly_payment_units:
                    balance_units = max(balance_units - override_monthly_payment_units, 0)
                    if balance_units == 0 and payoff_date is None:
                        payoff_date = current_day
                annual_rate_percent = _rate_for_date(obligation, current_day)
                if balance_units > 0 and annual_rate_percent:
                    interest_units += Decimal(balance_units) * (annual_rate_percent / Decimal('100')) / FIXED_DAYS_IN_YEAR
            current_day += timedelta(days=1)

        posted_interest_units = int(interest_units.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if posted_interest_units > 0:
            balance_units += posted_interest_units
        points.append(
            {
                'date': period_end,
                'balance_units': balance_units,
                'interest_units': posted_interest_units,
            }
        )
        period_start = period_end

    return {
        'obligation': obligation,
        'start_date': start_date,
        'months': months,
        'points': points,
        'payoff_date': payoff_date,
    }


def _planned_events_by_date(obligation, period_start, period_end, replace_scheduled_repayments):
    events_by_date = {}
    series_queryset = EventSeries.objects.filter(
        obligation=obligation,
        active=True,
        starts_on__lt=period_end,
    ).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=period_start))
    for series in series_queryset:
        if replace_scheduled_repayments and series.event_type == FinancialEvent.EventType.REPAYMENT:
            continue
        for occurrence in _occurrences_for_month(series, period_start, period_end):
            occurrence_date = occurrence['date']
            if occurrence_date < series.starts_on:
                continue
            if series.ends_on and occurrence_date > series.ends_on:
                continue
            version = _active_version(series, occurrence_date)
            if version is None:
                continue
            events_by_date.setdefault(occurrence_date, []).append(
                {
                    'event_type': series.event_type,
                    'amount_units': version.amount_units,
                }
            )
    return events_by_date


def _apply_planned_events(balance_units, planned_events):
    for event in planned_events:
        if event['event_type'] == FinancialEvent.EventType.SCHEDULED_CHARGE:
            balance_units += event['amount_units']
        elif event['event_type'] == FinancialEvent.EventType.REPAYMENT:
            balance_units = max(balance_units - event['amount_units'], 0)
    return balance_units


def _active_version(series, target_date):
    return (
        EventSeriesVersion.objects.filter(event_series=series, valid_from__lte=target_date)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=target_date))
        .order_by('-valid_from')
        .first()
    )


def _rate_for_date(obligation, target_date):
    rate = (
        InterestRatePeriod.objects.filter(obligation=obligation, effective_from__lte=target_date)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=target_date))
        .order_by('-effective_from')
        .first()
    )
    if rate is None:
        return Decimal('0')
    return rate.annual_rate_percent


def _is_override_payment_day(target_date, payment_day):
    last_day = monthrange(target_date.year, target_date.month)[1]
    return target_date.day == min(payment_day, last_day)


def _payoff_label(payoff_date, months):
    if payoff_date:
        return payoff_date.strftime('%b %Y')
    return f'>{months} months'
