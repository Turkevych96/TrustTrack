from dataclasses import dataclass, field

from django.db.models import Q

from ledger.models import UserProfile
from ledger.services.money import decimal_from_units
from ledger.services.telegram import TelegramLookupError, send_telegram_message


TELEGRAM_MESSAGE_SAFE_LIMIT = 3500


@dataclass
class NotificationSendResult:
    sent: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DueJobNotificationItem:
    obligation: object
    balance_before_units: int
    balance_after_units: int
    recurring_change_units: int
    interest_units: int

    @property
    def balance_change_units(self):
        return self.balance_after_units - self.balance_before_units


def build_due_job_notification_item(obligation, balance_before_units, balance_after_units, created_transactions, posted_runs):
    if balance_before_units == balance_after_units:
        return None
    return DueJobNotificationItem(
        obligation=obligation,
        balance_before_units=balance_before_units,
        balance_after_units=balance_after_units,
        recurring_change_units=_recurring_balance_change_units(created_transactions),
        interest_units=_interest_amount_units(posted_runs),
    )


def send_due_job_notifications(items):
    result = NotificationSendResult()
    if not items:
        return result

    notifications_by_profile = _notification_items_by_profile(items)
    if not notifications_by_profile:
        return result

    for profile, profile_items in notifications_by_profile.items():
        for text in build_due_job_notification_messages(profile, profile_items):
            try:
                send_telegram_message(profile.telegram_id, text)
            except TelegramLookupError as error:
                result.errors.append(f'{profile.user}: {error}')
            else:
                result.sent += 1
    return result


def build_due_job_notification_messages(profile, items, max_message_length=TELEGRAM_MESSAGE_SAFE_LIMIT):
    if profile.telegram_language == UserProfile.TelegramLanguage.RUSSIAN:
        return _digest_messages_ru(items, max_message_length)
    return _digest_messages_en(items, max_message_length)


def _notification_items_by_profile(items):
    items_by_profile_id = {}
    profiles_by_id = {}
    for item in items:
        for profile in _notification_profiles_for_obligation(item.obligation):
            profiles_by_id[profile.pk] = profile
            items_by_profile_id.setdefault(profile.pk, []).append(item)
    return {
        profiles_by_id[profile_id]: profile_items
        for profile_id, profile_items in items_by_profile_id.items()
    }


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


def _digest_messages_en(items, max_message_length):
    header = _digest_header_en(items)
    blocks = [_digest_item_block_en(item) for item in items]
    footer = 'You can turn these notifications off in /settings.'
    return _chunk_digest(header, blocks, footer, max_message_length)


def _digest_messages_ru(items, max_message_length):
    header = _digest_header_ru(items)
    blocks = [_digest_item_block_ru(item) for item in items]
    footer = 'Эти уведомления можно отключить в /settings.'
    return _chunk_digest(header, blocks, footer, max_message_length)


def _digest_header_en(items):
    return '\n'.join([
        'TrustTrack balance updates',
        f'{len(items)} obligation(s) changed',
        f'Total change: {_format_signed_money(_total_change_units(items))}',
    ])


def _digest_header_ru(items):
    return '\n'.join([
        'Обновления баланса TrustTrack',
        f'Изменено обязательств: {len(items)}',
        f'Общее изменение: {_format_signed_money(_total_change_units(items))}',
    ])


def _digest_item_block_en(item):
    lines = [
        item.obligation.title,
        f'Balance: {_format_money(item.balance_before_units)} -> {_format_money(item.balance_after_units)}',
        f'Change: {_format_signed_money(item.balance_change_units)}',
    ]
    if item.recurring_change_units:
        lines.append(f'Scheduled: {_format_signed_money(item.recurring_change_units)}')
    if item.interest_units:
        lines.append(f'Interest: {_format_signed_money(item.interest_units)}')
    lines.append(f'Current: {_format_money(item.balance_after_units)}')
    return '\n'.join(lines)


def _digest_item_block_ru(item):
    lines = [
        item.obligation.title,
        f'Баланс: {_format_money(item.balance_before_units)} -> {_format_money(item.balance_after_units)}',
        f'Изменение: {_format_signed_money(item.balance_change_units)}',
    ]
    if item.recurring_change_units:
        lines.append(f'Плановое: {_format_signed_money(item.recurring_change_units)}')
    if item.interest_units:
        lines.append(f'Проценты: {_format_signed_money(item.interest_units)}')
    lines.append(f'Текущий: {_format_money(item.balance_after_units)}')
    return '\n'.join(lines)


def _chunk_digest(header, blocks, footer, max_message_length):
    chunks = []
    current_blocks = []
    for block in blocks:
        candidate_blocks = [*current_blocks, block]
        candidate = _format_digest_chunk(header, candidate_blocks, footer)
        if current_blocks and len(candidate) > max_message_length:
            chunks.append(_format_digest_chunk(header, current_blocks, footer))
            current_blocks = [block]
        else:
            current_blocks = candidate_blocks
    if current_blocks:
        chunks.append(_format_digest_chunk(header, current_blocks, footer))
    if len(chunks) <= 1:
        return chunks
    return [
        _with_part_label(chunk, index + 1, len(chunks))
        for index, chunk in enumerate(chunks)
    ]


def _format_digest_chunk(header, blocks, footer):
    return '\n\n'.join([header, *blocks, footer])


def _with_part_label(message, part_number, part_count):
    first_line, *remaining_lines = message.splitlines()
    return '\n'.join([f'{first_line} ({part_number}/{part_count})', *remaining_lines])


def _total_change_units(items):
    return sum(item.balance_change_units for item in items)


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
