from calendar import monthrange
from datetime import date, timedelta

from django.utils import timezone

from ledger.models import EventSeries, EventSeriesVersion, FinancialEvent, LedgerTransaction, Obligation
from ledger.services.events import post_scheduled_charge, post_scheduled_repayment


def generate_recurring_events_for_month(month_start, obligation=None, through_date=None):
    period_start = month_start.replace(day=1)
    period_end = next_month_start(period_start)
    created_transactions = []

    series_queryset = EventSeries.objects.filter(
        active=True,
        obligation__status=Obligation.Status.OPEN,
        starts_on__lt=period_end,
    ).filter(models_ends_after(period_start))
    if obligation is not None:
        series_queryset = series_queryset.filter(obligation=obligation)

    for series in series_queryset.select_related('obligation'):
        for occurrence in _occurrences_for_month(series, period_start, period_end):
            if through_date is not None and occurrence['date'] > through_date:
                continue
            if occurrence['date'] < series.starts_on:
                continue
            if series.ends_on and occurrence['date'] > series.ends_on:
                continue

            version = _active_version(series, occurrence['date'])
            if version is None:
                continue

            idempotency_key = _idempotency_key(series, occurrence)
            if LedgerTransaction.objects.filter(idempotency_key=idempotency_key).exists():
                continue

            post_function = _post_function_for_series(series)
            created_transactions.append(
                post_function(
                    obligation=series.obligation,
                    amount_units=version.amount_units,
                    event_date=occurrence['date'],
                    memo=version.memo or series.memo,
                    category=series.name,
                    event_series=series,
                    event_series_version=version,
                    period_start=occurrence['period_start'],
                    period_end=occurrence['period_end'],
                    idempotency_key=idempotency_key,
                )
            )

    return created_transactions


def generate_due_recurring_events(obligation=None, through_date=None):
    through_date = through_date or timezone.localdate()
    series_queryset = EventSeries.objects.filter(
        active=True,
        obligation__status=Obligation.Status.OPEN,
        starts_on__lte=through_date,
    )
    if obligation is not None:
        series_queryset = series_queryset.filter(obligation=obligation)

    first_series = series_queryset.order_by('starts_on').first()
    if first_series is None:
        return []

    created_transactions = []
    current_month = first_series.starts_on.replace(day=1)
    final_month = through_date.replace(day=1)
    while current_month <= final_month:
        created_transactions.extend(
            generate_recurring_events_for_month(
                current_month,
                obligation=obligation,
                through_date=through_date,
            )
        )
        current_month = next_month_start(current_month)

    return created_transactions


def next_month_start(month_start):
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def models_ends_after(period_start):
    from django.db.models import Q

    return Q(ends_on__isnull=True) | Q(ends_on__gte=period_start)


def _occurrence_date_for_month(day_of_month, month_start):
    last_day = monthrange(month_start.year, month_start.month)[1]
    return date(month_start.year, month_start.month, min(day_of_month, last_day))


def _occurrences_for_month(series, period_start, period_end):
    if series.frequency == EventSeries.Frequency.MONTHLY:
        occurrence_date = _occurrence_date_for_month(series.day_of_month, period_start)
        return [
            {
                'date': occurrence_date,
                'period_start': period_start,
                'period_end': period_end,
            }
        ]
    if series.frequency == EventSeries.Frequency.WEEKLY:
        return _weekly_occurrences_for_month(series, period_start, period_end, step_days=7)
    if series.frequency == EventSeries.Frequency.BIWEEKLY:
        return _weekly_occurrences_for_month(series, period_start, period_end, step_days=14)
    raise ValueError(f'Unsupported recurring frequency: {series.frequency}')


def _weekly_occurrences_for_month(series, period_start, period_end, step_days):
    first_occurrence = _first_weekday_on_or_after(series.starts_on, int(series.day_of_week))
    while first_occurrence < period_start:
        days_to_skip = ((period_start - first_occurrence).days // step_days) * step_days
        first_occurrence += timedelta(days=days_to_skip)
        if first_occurrence < period_start:
            first_occurrence += timedelta(days=step_days)

    occurrences = []
    occurrence_date = first_occurrence
    while occurrence_date < period_end:
        occurrences.append(
            {
                'date': occurrence_date,
                'period_start': occurrence_date,
                'period_end': occurrence_date + timedelta(days=step_days),
            }
        )
        occurrence_date += timedelta(days=step_days)
    return occurrences


def _first_weekday_on_or_after(start_date, day_of_week):
    days_ahead = (day_of_week - start_date.weekday()) % 7
    return start_date + timedelta(days=days_ahead)


def _idempotency_key(series, occurrence):
    if series.frequency == EventSeries.Frequency.MONTHLY:
        return f"scheduled:{series.pk}:{occurrence['period_start'].isoformat()}"
    return f"scheduled:{series.pk}:{occurrence['date'].isoformat()}"


def _active_version(series, occurrence_date):
    return (
        EventSeriesVersion.objects.filter(
            event_series=series,
            valid_from__lte=occurrence_date,
        )
        .filter(models_version_valid_after(occurrence_date))
        .order_by('-valid_from')
        .first()
    )


def _post_function_for_series(series):
    if series.event_type == FinancialEvent.EventType.SCHEDULED_CHARGE:
        return post_scheduled_charge
    if series.event_type == FinancialEvent.EventType.REPAYMENT:
        return post_scheduled_repayment
    raise ValueError(f'Unsupported recurring event type: {series.event_type}')


def models_version_valid_after(occurrence_date):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=occurrence_date)
