from django.db import transaction as db_transaction
from django.utils import timezone

from ledger.services.interest import recalculate_interest_from
from ledger.services.recurring import recalculate_due_recurring_events


def recalculate_obligation(obligation, from_date=None, through_date=None):
    from_date = from_date or obligation.opened_on
    through_date = through_date or timezone.localdate()

    with db_transaction.atomic():
        recurring_result = recalculate_due_recurring_events(
            obligation,
            from_date=from_date,
            through_date=through_date,
        )
        interest_result = recalculate_interest_from(
            obligation,
            from_date=from_date,
            through_date=through_date,
        )

    return {
        'from_date': from_date,
        'through_date': through_date,
        'recurring': recurring_result,
        'interest': interest_result,
    }
