from calendar import monthrange
from datetime import timedelta

from django.utils import timezone

from ledger.services.balances import get_obligation_balance


def build_balance_history(obligations, user, months=12, today=None):
    today = today or timezone.localdate()
    month_ends = _month_end_points(today, months)
    obligations = list(obligations)
    points = []

    for point_date in month_ends:
        i_owe_units = 0
        owed_to_me_units = 0
        for obligation in obligations:
            balance_units = get_obligation_balance(obligation, as_of=point_date)
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

    latest_point = points[-1] if points else None
    return {
        'points': points,
        'latest_date': latest_point['date'] if latest_point else None,
        'latest_i_owe_units': latest_point['i_owe_units'] if latest_point else 0,
        'latest_owed_to_me_units': latest_point['owed_to_me_units'] if latest_point else 0,
        'latest_net_units': latest_point['net_units'] if latest_point else 0,
    }


def _month_end_points(today, months):
    if months <= 0:
        return []

    current = _latest_completed_month_end(today)
    points = []
    for _ in range(months):
        points.append(current)
        current = current.replace(day=1) - timedelta(days=1)
    return list(reversed(points))


def _latest_completed_month_end(today):
    last_day = monthrange(today.year, today.month)[1]
    if today.day == last_day:
        return today
    return today.replace(day=1) - timedelta(days=1)
