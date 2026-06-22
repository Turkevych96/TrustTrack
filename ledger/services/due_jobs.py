from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.utils import timezone

from ledger.models import Obligation
from ledger.services.balances import get_obligation_balance
from ledger.services.interest import generate_due_interest
from ledger.services.notifications import build_due_job_notification_item, send_due_job_notifications
from ledger.services.recurring import generate_due_recurring_events


@dataclass
class DueJobObligationResult:
    obligation_id: int
    title: str
    recurring_created: int = 0
    interest_posted: int = 0
    notifications_sent: int = 0
    errors: list[str] = field(default_factory=list)
    notification_errors: list[str] = field(default_factory=list)


@dataclass
class DueJobResult:
    through_date: object
    obligation_count: int = 0
    recurring_created: int = 0
    interest_posted: int = 0
    notifications_sent: int = 0
    notification_errors: list[str] = field(default_factory=list)
    obligation_results: list[DueJobObligationResult] = field(default_factory=list)

    @property
    def error_count(self):
        return sum(len(item.errors) for item in self.obligation_results)

    @property
    def notification_error_count(self):
        return len(self.notification_errors) + sum(len(item.notification_errors) for item in self.obligation_results)


def run_due_jobs(through_date=None):
    through_date = through_date or timezone.localdate()
    result = DueJobResult(through_date=through_date)
    obligations = Obligation.objects.filter(status=Obligation.Status.OPEN).order_by('id')
    notification_items = []

    for obligation in obligations:
        result.obligation_count += 1
        obligation_result = DueJobObligationResult(
            obligation_id=obligation.pk,
            title=obligation.title,
        )
        balance_before_units = get_obligation_balance(obligation)
        created_transactions = []
        posted_runs = []

        try:
            created_transactions = generate_due_recurring_events(
                obligation=obligation,
                through_date=through_date,
            )
        except ValidationError as error:
            obligation_result.errors.append(_validation_error_message(error))
        else:
            obligation_result.recurring_created = len(created_transactions)
            result.recurring_created += obligation_result.recurring_created

        try:
            posted_runs = generate_due_interest(
                obligation,
                through_date=through_date,
            )
        except ValidationError as error:
            obligation_result.errors.append(_validation_error_message(error))
        else:
            obligation_result.interest_posted = len(posted_runs)
            result.interest_posted += obligation_result.interest_posted

        balance_after_units = get_obligation_balance(obligation)
        if balance_after_units != balance_before_units and (created_transactions or posted_runs):
            notification_item = build_due_job_notification_item(
                obligation,
                balance_before_units,
                balance_after_units,
                created_transactions,
                posted_runs,
            )
            if notification_item is not None:
                notification_items.append(notification_item)

        result.obligation_results.append(obligation_result)

    notification_result = send_due_job_notifications(notification_items)
    result.notifications_sent = notification_result.sent
    result.notification_errors.extend(notification_result.errors)

    return result


def _validation_error_message(error):
    if hasattr(error, 'message'):
        return error.message
    return '; '.join(error.messages)
