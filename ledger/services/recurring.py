from calendar import monthrange
from datetime import date

from ledger.models import EventSeries, EventSeriesVersion, LedgerTransaction
from ledger.services.events import post_scheduled_charge


def generate_recurring_events_for_month(month_start):
    period_start = month_start.replace(day=1)
    period_end = next_month_start(period_start)
    created_transactions = []

    series_queryset = EventSeries.objects.filter(
        active=True,
        frequency=EventSeries.Frequency.MONTHLY,
        starts_on__lt=period_end,
    ).filter(models_ends_after(period_start))

    for series in series_queryset.select_related('obligation'):
        occurrence_date = _occurrence_date_for_month(series.day_of_month, period_start)
        if occurrence_date < series.starts_on:
            continue
        if series.ends_on and occurrence_date > series.ends_on:
            continue

        version = _active_version(series, occurrence_date)
        if version is None:
            continue

        idempotency_key = f'scheduled:{series.pk}:{period_start.isoformat()}'
        if LedgerTransaction.objects.filter(idempotency_key=idempotency_key).exists():
            continue

        created_transactions.append(
            post_scheduled_charge(
                obligation=series.obligation,
                amount_units=version.amount_units,
                event_date=occurrence_date,
                memo=version.memo or series.memo,
                category=series.name,
                event_series=series,
                event_series_version=version,
                period_start=period_start,
                period_end=period_end,
                idempotency_key=idempotency_key,
            )
        )

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


def models_version_valid_after(occurrence_date):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=occurrence_date)
