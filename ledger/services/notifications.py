from dataclasses import dataclass, field

from django.db.models import Q

from ledger.models import UserProfile
from ledger.services.money import decimal_from_units
from ledger.services.telegram import TelegramLookupError, send_telegram_message


@dataclass
class NotificationSendResult:
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def send_due_job_notifications(obligation, balance_before_units, balance_after_units, created_transactions, posted_runs):
    result = NotificationSendResult()
    if balance_before_units == balance_after_units:
        return result

    profiles = _notification_profiles_for_obligation(obligation)
    if not profiles:
        return result

    for profile in profiles:
        text = _due_job_notification_text(
            profile,
            obligation,
            balance_before_units,
            balance_after_units,
            _recurring_balance_change_units(created_transactions),
            _interest_amount_units(posted_runs),
        )
        try:
            send_telegram_message(profile.telegram_id, text)
        except TelegramLookupError as error:
            result.errors.append(f'{profile.user}: {error}')
        else:
            result.sent += 1
    return result


def _notification_profiles_for_obligation(obligation):
    return list(
        UserProfile.objects.select_related('user')
        .filter(
            user__in=[obligation.creditor, obligation.borrower],
            user__is_active=True,
            telegram_id__isnull=False,
            payment_due_notifications=True,
        )
        .filter(Q(telegram_chat_type='') | Q(telegram_chat_type='private'))
        .order_by('user_id')
    )


def _due_job_notification_text(
    profile,
    obligation,
    balance_before_units,
    balance_after_units,
    recurring_change_units,
    interest_units,
):
    if profile.telegram_language == UserProfile.TelegramLanguage.RUSSIAN:
        return _due_job_notification_text_ru(
            obligation,
            balance_before_units,
            balance_after_units,
            recurring_change_units,
            interest_units,
        )
    return _due_job_notification_text_en(
        obligation,
        balance_before_units,
        balance_after_units,
        recurring_change_units,
        interest_units,
    )


def _due_job_notification_text_en(obligation, balance_before_units, balance_after_units, recurring_change_units, interest_units):
    return '\n'.join([
        'TrustTrack balance update',
        obligation.title,
        f'Balance changed: {_format_money(balance_before_units)} -> {_format_money(balance_after_units)}',
        f'Change: {_format_signed_money(balance_after_units - balance_before_units)}',
        f'Scheduled amount: {_format_signed_money(recurring_change_units)}',
        f'Interest amount: {_format_signed_money(interest_units)}',
        f'Current balance: {_format_money(balance_after_units)}',
        '',
        'You can turn these notifications off in /settings.',
    ])


def _due_job_notification_text_ru(obligation, balance_before_units, balance_after_units, recurring_change_units, interest_units):
    return '\n'.join([
        'Обновление баланса TrustTrack',
        obligation.title,
        f'Баланс изменился: {_format_money(balance_before_units)} -> {_format_money(balance_after_units)}',
        f'Изменение: {_format_signed_money(balance_after_units - balance_before_units)}',
        f'Плановые начисления: {_format_signed_money(recurring_change_units)}',
        f'Проценты: {_format_signed_money(interest_units)}',
        f'Текущий баланс: {_format_money(balance_after_units)}',
        '',
        'Эти уведомления можно отключить в /settings.',
    ])


def _recurring_balance_change_units(created_transactions):
    total_units = 0
    for transaction in created_transactions:
        event = transaction.financial_event
        if event.direction == event.Direction.INCREASES_DEBT:
            total_units += event.amount_units
        else:
            total_units -= event.amount_units
    return total_units


def _interest_amount_units(posted_runs):
    return sum(run.calculated_interest_amount_units for run in posted_runs)


def _format_money(amount_units):
    return f'${decimal_from_units(amount_units):,.2f}'


def _format_signed_money(amount_units):
    sign = '+' if amount_units >= 0 else '-'
    return f'{sign}{_format_money(abs(amount_units))}'
