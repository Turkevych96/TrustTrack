from dataclasses import dataclass, field

from django.db.models import Q

from ledger.models import UserProfile
from ledger.services.balances import get_obligation_balance
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


def send_obligation_created_notification(obligation, initial_amount_units, recalculation_result=None):
    result = NotificationSendResult()
    profiles = _notification_profiles_for_obligation(obligation)
    if not profiles:
        return result

    current_balance_units = get_obligation_balance(obligation)
    recurring_change_units = _recurring_change_from_recalculation(recalculation_result)
    interest_units = _interest_from_recalculation(recalculation_result)
    for profile in profiles:
        text = _obligation_created_notification_text(
            profile,
            obligation,
            initial_amount_units,
            current_balance_units,
            recurring_change_units,
            interest_units,
        )
        try:
            send_telegram_message(profile.telegram_id, text)
        except TelegramLookupError as error:
            result.errors.append(f'{profile.user}: {error}')
        else:
            result.sent += 1
    return result


def build_due_job_notification_messages(profile, items, max_message_length=TELEGRAM_MESSAGE_SAFE_LIMIT):
    if profile.telegram_language == UserProfile.TelegramLanguage.RUSSIAN:
        return _digest_messages_ru(profile, items, max_message_length)
    return _digest_messages_en(profile, items, max_message_length)


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


def _obligation_created_notification_text(
    profile,
    obligation,
    initial_amount_units,
    current_balance_units,
    recurring_change_units,
    interest_units,
):
    if profile.telegram_language == UserProfile.TelegramLanguage.RUSSIAN:
        return _obligation_created_notification_text_ru(
            obligation,
            initial_amount_units,
            current_balance_units,
            recurring_change_units,
            interest_units,
        )
    return _obligation_created_notification_text_en(
        obligation,
        initial_amount_units,
        current_balance_units,
        recurring_change_units,
        interest_units,
    )


def _obligation_created_notification_text_en(
    obligation,
    initial_amount_units,
    current_balance_units,
    recurring_change_units,
    interest_units,
):
    lines = [
        'New TrustTrack obligation',
        obligation.title,
        f'Borrower: {_user_label(obligation.borrower)}',
        f'Creditor: {_user_label(obligation.creditor)}',
        f'Opened: {obligation.opened_on:%m/%d/%Y}',
        f'Initial amount: {_format_money(initial_amount_units)}',
    ]
    if recurring_change_units:
        lines.append(f'Generated scheduled: {_format_signed_money(recurring_change_units)}')
    if interest_units:
        lines.append(f'Generated interest: {_format_signed_money(interest_units)}')
    lines.extend([
        f'Current balance: {_format_money(current_balance_units)}',
        '',
        'You can turn these notifications off in /settings.',
    ])
    return '\n'.join(lines)


def _obligation_created_notification_text_ru(
    obligation,
    initial_amount_units,
    current_balance_units,
    recurring_change_units,
    interest_units,
):
    lines = [
        'Новое обязательство TrustTrack',
        obligation.title,
        f'Заёмщик: {_user_label(obligation.borrower)}',
        f'Кредитор: {_user_label(obligation.creditor)}',
        f'Открыто: {obligation.opened_on:%m/%d/%Y}',
        f'Начальная сумма: {_format_money(initial_amount_units)}',
    ]
    if recurring_change_units:
        lines.append(f'Сгенерировано плановое: {_format_signed_money(recurring_change_units)}')
    if interest_units:
        lines.append(f'Сгенерированы проценты: {_format_signed_money(interest_units)}')
    lines.extend([
        f'Текущий баланс: {_format_money(current_balance_units)}',
        '',
        'Эти уведомления можно отключить в /settings.',
    ])
    return '\n'.join(lines)


def _digest_messages_en(profile, items, max_message_length):
    header = _digest_header_en(profile, items)
    blocks = [_digest_item_block_en(profile, item) for item in items]
    footer = 'You can turn these notifications off in /settings.'
    return _chunk_digest(header, blocks, footer, max_message_length)


def _digest_messages_ru(profile, items, max_message_length):
    header = _digest_header_ru(profile, items)
    blocks = [_digest_item_block_ru(profile, item) for item in items]
    footer = 'Эти уведомления можно отключить в /settings.'
    return _chunk_digest(header, blocks, footer, max_message_length)


def _digest_header_en(profile, items):
    lines = [
        'TrustTrack balance updates',
        f'{len(items)} obligation(s) changed',
    ]
    borrower_change_units = _role_change_units(profile, items, 'borrower')
    creditor_change_units = _role_change_units(profile, items, 'creditor')
    if borrower_change_units:
        lines.append(f'You owe change: {_format_signed_money(borrower_change_units)}')
    if creditor_change_units:
        lines.append(f'Owed to you change: {_format_signed_money(creditor_change_units)}')
    if not borrower_change_units and not creditor_change_units:
        lines.append('No balance change')
    return '\n'.join(lines)


def _digest_header_ru(profile, items):
    lines = [
        'Обновления баланса TrustTrack',
        f'Изменено обязательств: {len(items)}',
    ]
    borrower_change_units = _role_change_units(profile, items, 'borrower')
    creditor_change_units = _role_change_units(profile, items, 'creditor')
    if borrower_change_units:
        lines.append(f'Изменение вашего долга: {_format_signed_money(borrower_change_units)}')
    if creditor_change_units:
        lines.append(f'Изменение суммы, которую должны вам: {_format_signed_money(creditor_change_units)}')
    if not borrower_change_units and not creditor_change_units:
        lines.append('Изменений баланса нет')
    return '\n'.join(lines)


def _digest_item_block_en(profile, item):
    role = _item_role(profile, item)
    if role == 'borrower':
        type_label = 'You owe'
        balance_label = 'You owe'
        change_label = 'You owe change'
        current_label = 'You owe now'
    else:
        type_label = 'Owed to you'
        balance_label = 'Owed to you'
        change_label = 'Owed to you change'
        current_label = 'Owed to you now'
    lines = [
        item.obligation.title,
        f'Type: {type_label}',
        f'{balance_label}: {_format_money(item.balance_before_units)} -> {_format_money(item.balance_after_units)}',
        f'{change_label}: {_format_signed_money(item.balance_change_units)}',
    ]
    if item.recurring_change_units:
        lines.append(f'Scheduled: {_format_signed_money(item.recurring_change_units)}')
    if item.interest_units:
        lines.append(f'Interest: {_format_signed_money(item.interest_units)}')
    lines.append(f'{current_label}: {_format_money(item.balance_after_units)}')
    return '\n'.join(lines)


def _digest_item_block_ru(profile, item):
    role = _item_role(profile, item)
    if role == 'borrower':
        type_label = 'Вы должны'
        balance_label = 'Ваш долг'
        change_label = 'Изменение вашего долга'
        current_label = 'Ваш долг сейчас'
    else:
        type_label = 'Вам должны'
        balance_label = 'Вам должны'
        change_label = 'Изменение суммы, которую должны вам'
        current_label = 'Вам должны сейчас'
    lines = [
        item.obligation.title,
        f'Тип: {type_label}',
        f'{balance_label}: {_format_money(item.balance_before_units)} -> {_format_money(item.balance_after_units)}',
        f'{change_label}: {_format_signed_money(item.balance_change_units)}',
    ]
    if item.recurring_change_units:
        lines.append(f'Плановое: {_format_signed_money(item.recurring_change_units)}')
    if item.interest_units:
        lines.append(f'Проценты: {_format_signed_money(item.interest_units)}')
    lines.append(f'{current_label}: {_format_money(item.balance_after_units)}')
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


def _role_change_units(profile, items, role):
    return sum(
        item.balance_change_units
        for item in items
        if _item_role(profile, item) == role
    )


def _item_role(profile, item):
    if item.obligation.borrower_id == profile.user_id:
        return 'borrower'
    return 'creditor'


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


def _recurring_change_from_recalculation(recalculation_result):
    if not recalculation_result:
        return 0
    recurring_result = recalculation_result.get('recurring') or {}
    return _recurring_balance_change_units(recurring_result.get('created_transactions') or [])


def _interest_from_recalculation(recalculation_result):
    if not recalculation_result:
        return 0
    interest_result = recalculation_result.get('interest') or {}
    return _interest_amount_units(interest_result.get('posted_runs') or [])


def _user_label(user):
    return user.get_full_name() or user.get_username()


def _format_money(amount_units):
    return f'${decimal_from_units(amount_units):,.2f}'


def _format_signed_money(amount_units):
    sign = '+' if amount_units >= 0 else '-'
    return f'{sign}{_format_money(abs(amount_units))}'
