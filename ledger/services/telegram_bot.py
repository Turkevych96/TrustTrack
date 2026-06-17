from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import secrets
import shlex

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from ledger.models import LedgerTransaction, Obligation, UserProfile
from ledger.services.balances import get_obligation_balance
from ledger.services.events import post_repayment
from ledger.services.money import decimal_from_units, units_from_decimal


QUICK_REPAYMENT_AMOUNTS = (Decimal('25'), Decimal('50'), Decimal('100'))
PENDING_REPAYMENT_OBLIGATIONS = {}


@dataclass(frozen=True)
class TelegramOutgoingMessage:
    chat_id: int
    text: str
    reply_markup: dict | None = None


@dataclass(frozen=True)
class TelegramBotResult:
    messages: list[TelegramOutgoingMessage] = field(default_factory=list)
    callback_query_id: str = ''
    callback_text: str = ''


def process_telegram_update(update, today=None, nonce_factory=None):
    today = today or timezone.localdate()
    nonce_factory = nonce_factory or _confirmation_nonce

    if 'callback_query' in update:
        return _process_callback_query(update['callback_query'], today=today, nonce_factory=nonce_factory)
    if 'message' not in update:
        return TelegramBotResult()
    return _process_message(update['message'], today=today, nonce_factory=nonce_factory)


def _process_message(message, today, nonce_factory):
    chat = message.get('chat') or {}
    from_user = message.get('from') or {}
    chat_id = chat.get('id')
    telegram_user_id = from_user.get('id')

    if not chat_id or not telegram_user_id:
        return TelegramBotResult()

    text = (message.get('text') or '').strip()
    access_message = _access_error_message(chat, telegram_user_id)
    if access_message:
        return _single_message(chat_id, access_message)

    profile = _get_profile_for_telegram_id(telegram_user_id)
    if not profile:
        return _single_message(
            chat_id,
            'Access is not configured for this Telegram ID.\n'
            f'Telegram ID: {telegram_user_id}\n\n'
            'Add this ID in your TrustTrack Profile first.',
        )

    if not text:
        return _single_message(chat_id, _help_text(profile.user), reply_markup=_main_menu_markup())

    if not text.startswith('/') and telegram_user_id in PENDING_REPAYMENT_OBLIGATIONS:
        return _pending_repayment_preview(
            profile.user,
            telegram_user_id,
            chat_id,
            text,
            today,
            nonce_factory,
        )

    command, *raw_args = text.split(maxsplit=1)
    command = command.split('@', maxsplit=1)[0].lower()
    args_text = raw_args[0] if raw_args else ''

    if command in ('/start', '/help'):
        return _single_message(chat_id, _start_text(profile.user, today), reply_markup=_main_menu_markup())
    if command == '/balance':
        return _single_message(chat_id, _balance_text(profile.user), reply_markup=_main_menu_markup())
    if command in ('/debt', '/obligation'):
        obligation = _find_obligation_from_code(profile.user, args_text.strip())
        if not obligation:
            return _single_message(
                chat_id,
                'Choose an obligation below.',
                reply_markup=_obligations_menu_markup(profile.user),
            )
        return _single_message(
            chat_id,
            _obligation_detail_text(profile.user, obligation),
            reply_markup=_obligation_detail_markup(obligation),
        )
    if command in ('/repay', '/payment'):
        return _repayment_preview(profile.user, chat_id, args_text, today, nonce_factory)

    return _single_message(chat_id, _unknown_command_text(profile.user), reply_markup=_main_menu_markup())


def _process_callback_query(callback_query, today, nonce_factory):
    callback_query_id = callback_query.get('id') or ''
    from_user = callback_query.get('from') or {}
    telegram_user_id = from_user.get('id')
    message = callback_query.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    data = callback_query.get('data') or ''

    if not callback_query_id or not chat_id or not telegram_user_id:
        return TelegramBotResult()

    access_message = _access_error_message(chat, telegram_user_id)
    if access_message:
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, access_message)],
            callback_query_id=callback_query_id,
            callback_text='Access denied',
        )

    profile = _get_profile_for_telegram_id(telegram_user_id)
    if not profile:
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, 'Access is not configured for this Telegram ID.')],
            callback_query_id=callback_query_id,
            callback_text='Access denied',
        )

    if data == 'noop:cancel':
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, 'Cancelled.')],
            callback_query_id=callback_query_id,
            callback_text='Cancelled',
        )
    if data == 'menu:balance':
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, _balance_text(profile.user), _main_menu_markup())],
            callback_query_id=callback_query_id,
            callback_text='Balance',
        )
    if data == 'menu:obligations':
        return TelegramBotResult(
            messages=[
                TelegramOutgoingMessage(
                    chat_id,
                    _obligations_menu_text(profile.user, today),
                    _obligations_menu_markup(profile.user),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text='Obligations',
        )
    if data.startswith('ob:'):
        text, reply_markup = _obligation_callback_response(profile.user, data)
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Obligation',
        )
    if data.startswith('repaymenu:'):
        text, reply_markup = _repayment_menu_callback_response(profile.user, data, today)
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Repayment',
        )
    if data.startswith('customrepay:'):
        text, reply_markup = _custom_repayment_callback_response(profile.user, telegram_user_id, data)
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Custom amount',
        )
    if data.startswith('repayamt:'):
        text, reply_markup = _repayment_amount_callback_response(profile.user, data, today, nonce_factory)
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Confirm repayment',
        )
    if data.startswith('repay:'):
        text = _confirm_repayment(profile.user, data, today)
        return TelegramBotResult(
            messages=[TelegramOutgoingMessage(chat_id, text, _main_menu_markup())],
            callback_query_id=callback_query_id,
            callback_text='Processed',
        )

    return TelegramBotResult(
        messages=[TelegramOutgoingMessage(chat_id, 'This button is no longer supported.')],
        callback_query_id=callback_query_id,
        callback_text='Unsupported action',
    )


def _access_error_message(chat, telegram_user_id):
    if (chat.get('type') or '') != 'private':
        return 'Please use TrustTrack bot in a private chat.'
    if not telegram_user_id:
        return 'Telegram user ID was not provided.'
    return ''


def _get_profile_for_telegram_id(telegram_user_id):
    return UserProfile.objects.select_related('user').filter(telegram_id=telegram_user_id).first()


def _start_text(user, today):
    obligations = list(_open_obligations_for_user(user))
    lines = [
        f'TrustTrack access confirmed for {_user_label(user)}.',
        '',
        'Choose an action below.',
        '',
        'Open obligations:',
    ]
    if not obligations:
        lines.append('No open obligations yet.')
    else:
        lines.extend(_obligation_summary_line(user, obligation, today=today) for obligation in obligations)
    return '\n'.join(lines)


def _help_text(user):
    return '\n'.join([
        f'TrustTrack access confirmed for {_user_label(user)}.',
        '',
        'Use the buttons below to check balance, open an obligation, or record a repayment.',
    ])


def _balance_text(user):
    obligations = list(_open_obligations_for_user(user))
    i_owe_units = 0
    owed_to_me_units = 0
    for obligation in obligations:
        balance_units = get_obligation_balance(obligation)
        if obligation.borrower_id == user.id:
            i_owe_units += balance_units
        if obligation.creditor_id == user.id:
            owed_to_me_units += balance_units

    net_units = owed_to_me_units - i_owe_units
    lines = [
        'TrustTrack balance',
        f'I owe: {_format_money(i_owe_units)}',
        f'Owed to me: {_format_money(owed_to_me_units)}',
        f'Net: {_format_signed_money(net_units)}',
    ]
    if obligations:
        lines.extend(['', 'Open obligations:'])
        lines.extend(_obligation_summary_line(user, obligation) for obligation in obligations)
    return '\n'.join(lines)


def _obligation_text(user, args_text):
    obligation = _find_obligation_from_code(user, args_text.strip())
    if not obligation:
        return 'Use an obligation code from /start, for example: /debt O12'

    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        f'{obligation.title} ({_obligation_code(obligation)})',
        f'Role: {role}',
        f'Counterparty: {_user_label(counterparty)}',
        f'Current balance: {_format_money(balance_units)}',
        '',
        'Use the repayment button below, or type a command such as:',
        f'/repay {_obligation_code(obligation)} 25',
    ])


def _repayment_preview(user, chat_id, args_text, today, nonce_factory):
    parsed = _parse_repayment_args(args_text, today)
    if 'error' in parsed:
        return _single_message(chat_id, parsed['error'])

    obligation = _find_obligation_from_code(user, parsed['code'])
    if not obligation:
        return _single_message(chat_id, 'Unknown obligation code. Use /start to see available codes.')

    return _repayment_preview_for_obligation(
        chat_id=chat_id,
        obligation=obligation,
        amount_units=parsed['amount_units'],
        event_date=parsed['event_date'],
        today=today,
        nonce_factory=nonce_factory,
    )


def _pending_repayment_preview(user, telegram_user_id, chat_id, amount_text, today, nonce_factory):
    obligation_id = PENDING_REPAYMENT_OBLIGATIONS.get(telegram_user_id)
    obligation = _get_related_open_obligation(user, obligation_id)
    if not obligation:
        PENDING_REPAYMENT_OBLIGATIONS.pop(telegram_user_id, None)
        return _single_message(
            chat_id,
            'This obligation is not available anymore.',
            reply_markup=_main_menu_markup(),
        )

    amount_units, error = _parse_amount_units(amount_text)
    if error:
        return _single_message(
            chat_id,
            f'{error}\nSend only the amount, for example: 37.50',
            reply_markup=_repayment_amount_markup(obligation, get_obligation_balance(obligation, as_of=today)),
        )

    PENDING_REPAYMENT_OBLIGATIONS.pop(telegram_user_id, None)
    return _repayment_preview_for_obligation(
        chat_id=chat_id,
        obligation=obligation,
        amount_units=amount_units,
        event_date=today,
        today=today,
        nonce_factory=nonce_factory,
    )


def _repayment_preview_for_obligation(chat_id, obligation, amount_units, event_date, today, nonce_factory):
    balance_units = get_obligation_balance(obligation, as_of=event_date)
    if amount_units > balance_units:
        return _single_message(
            chat_id,
            f'Repayment cannot exceed the balance on {event_date.isoformat()}.\n'
            f'Balance: {_format_money(balance_units)}',
            reply_markup=_obligation_detail_markup(obligation),
        )

    nonce = nonce_factory()
    callback_data = f'repay:{obligation.pk}:{amount_units}:{event_date.isoformat()}:{nonce}'
    text = '\n'.join([
        'Confirm repayment',
        f'Obligation: {obligation.title} ({_obligation_code(obligation)})',
        f'Amount: {_format_money(amount_units)}',
        f'Date: {event_date.isoformat()}',
    ])
    return _single_message(
        chat_id,
        text,
        reply_markup={
            'inline_keyboard': [
                [{'text': 'Confirm repayment', 'callback_data': callback_data}],
                [{'text': 'Cancel', 'callback_data': 'noop:cancel'}],
            ],
        },
    )


def _obligations_menu_text(user, today):
    obligations = list(_open_obligations_for_user(user))
    if not obligations:
        return 'No open obligations yet.'
    lines = ['Open obligations:', '']
    lines.extend(_obligation_summary_line(user, obligation, today=today) for obligation in obligations)
    return '\n'.join(lines)


def _obligation_callback_response(user, data):
    obligation = _get_obligation_from_callback(user, data, 'ob')
    if not obligation:
        return 'This obligation is not available anymore.', _main_menu_markup()
    return _obligation_detail_text(user, obligation), _obligation_detail_markup(obligation)


def _repayment_menu_callback_response(user, data, today):
    obligation = _get_obligation_from_callback(user, data, 'repaymenu')
    if not obligation:
        return 'This obligation is not available anymore.', _main_menu_markup()

    balance_units = get_obligation_balance(obligation, as_of=today)
    if balance_units <= 0:
        return (
            f'{obligation.title} has no balance to repay.',
            _obligation_detail_markup(obligation),
        )

    return (
        '\n'.join([
            f'Record repayment for {obligation.title} ({_obligation_code(obligation)})',
            f'Current balance: {_format_money(balance_units)}',
            '',
            'Choose an amount:',
        ]),
        _repayment_amount_markup(obligation, balance_units),
    )


def _custom_repayment_callback_response(user, telegram_user_id, data):
    obligation = _get_obligation_from_callback(user, data, 'customrepay')
    if not obligation:
        return 'This obligation is not available anymore.', _main_menu_markup()

    PENDING_REPAYMENT_OBLIGATIONS[telegram_user_id] = obligation.pk
    return (
        '\n'.join([
            f'Custom repayment for {obligation.title} ({_obligation_code(obligation)})',
            'Send only the amount, for example: 37.50',
        ]),
        _obligation_detail_markup(obligation),
    )


def _repayment_amount_callback_response(user, data, today, nonce_factory):
    parts = data.split(':')
    if len(parts) != 3:
        return 'Invalid repayment amount.', _main_menu_markup()
    try:
        obligation_id = int(parts[1])
        amount_units = int(parts[2])
    except ValueError:
        return 'Invalid repayment amount.', _main_menu_markup()

    obligation = _get_related_open_obligation(user, obligation_id)
    if not obligation:
        return 'This obligation is not available anymore.', _main_menu_markup()

    result = _repayment_preview_for_obligation(
        chat_id=0,
        obligation=obligation,
        amount_units=amount_units,
        event_date=today,
        today=today,
        nonce_factory=nonce_factory,
    )
    if not result.messages:
        return 'Invalid repayment amount.', _obligation_detail_markup(obligation)
    message = result.messages[0]
    return message.text, message.reply_markup


def _confirm_repayment(user, callback_data, today):
    parts = callback_data.split(':')
    if len(parts) != 5:
        return 'Invalid repayment confirmation.'

    _, obligation_id, amount_units_text, event_date_text, nonce = parts
    try:
        obligation = _get_related_open_obligation(user, int(obligation_id))
        amount_units = int(amount_units_text)
        event_date = date.fromisoformat(event_date_text)
    except (TypeError, ValueError):
        return 'Invalid repayment confirmation.'

    if not obligation or amount_units <= 0:
        return 'Invalid repayment confirmation.'
    if event_date > today:
        return 'Repayment date cannot be in the future.'

    idempotency_key = f'telegram-repayment:{user.pk}:{obligation.pk}:{nonce}'
    existing = LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return _repayment_recorded_text(obligation, amount_units, event_date, already_recorded=True)

    try:
        post_repayment(
            obligation,
            amount_units=amount_units,
            event_date=event_date,
            memo=f'Telegram repayment by {_user_label(user)}',
            category='telegram',
            idempotency_key=idempotency_key,
        )
    except ValidationError as error:
        return _validation_error_text(error)

    return _repayment_recorded_text(obligation, amount_units, event_date)


def _repayment_recorded_text(obligation, amount_units, event_date, already_recorded=False):
    balance_units = get_obligation_balance(obligation)
    heading = 'Repayment was already recorded.' if already_recorded else 'Repayment recorded.'
    return '\n'.join([
        heading,
        f'Obligation: {obligation.title} ({_obligation_code(obligation)})',
        f'Amount: {_format_money(amount_units)}',
        f'Date: {event_date.isoformat()}',
        f'Current balance: {_format_money(balance_units)}',
    ])


def _parse_repayment_args(args_text, today):
    try:
        tokens = shlex.split(args_text)
    except ValueError:
        return {'error': 'Could not parse repayment command. Example: /repay O12 25'}

    if len(tokens) < 2:
        return {'error': 'Use: /repay O12 25 or /repay O12 25 2026-06-17'}

    code = tokens[0].upper()
    amount_units, error = _parse_amount_units(tokens[1])
    if error:
        return {'error': f'{error} Example: /repay O12 25'}

    event_date = today
    if len(tokens) >= 3:
        try:
            event_date = date.fromisoformat(tokens[2])
        except ValueError:
            return {'error': 'Repayment date must use YYYY-MM-DD. Example: /repay O12 25 2026-06-17'}

    if event_date > today:
        return {'error': 'Repayment date cannot be in the future.'}

    return {
        'code': code,
        'amount_units': amount_units,
        'event_date': event_date,
    }


def _unknown_command_text(user):
    return '\n'.join([
        'Unknown command.',
        '',
        f'Use /start to open the TrustTrack menu for {_user_label(user)}.',
    ])


def _obligation_summary_line(user, obligation, today=None):
    balance_units = get_obligation_balance(obligation, as_of=today)
    role = 'you owe' if obligation.borrower_id == user.id else 'owed to you'
    counterparty = obligation.creditor if obligation.borrower_id == user.id else obligation.borrower
    return (
        f'{_obligation_code(obligation)} - {obligation.title} - '
        f'{role} {_user_label(counterparty)} - {_format_money(balance_units)}'
    )


def _find_obligation_from_code(user, code):
    code = code.strip().upper()
    if not code.startswith('O'):
        return None
    try:
        obligation_id = int(code[1:])
    except ValueError:
        return None
    return _get_related_open_obligation(user, obligation_id)


def _get_obligation_from_callback(user, data, prefix):
    parts = data.split(':')
    if len(parts) != 2 or parts[0] != prefix:
        return None
    try:
        obligation_id = int(parts[1])
    except ValueError:
        return None
    return _get_related_open_obligation(user, obligation_id)


def _parse_amount_units(amount_text):
    try:
        amount_units = units_from_decimal(Decimal(amount_text))
    except (InvalidOperation, ValueError):
        return None, 'Repayment amount must be a number.'

    if amount_units <= 0:
        return None, 'Repayment amount must be greater than zero.'
    return amount_units, ''


def _get_related_open_obligation(user, obligation_id):
    return _open_obligations_for_user(user).filter(pk=obligation_id).first()


def _open_obligations_for_user(user):
    return (
        Obligation.objects
        .filter(Q(creditor=user) | Q(borrower=user), status=Obligation.Status.OPEN)
        .select_related('borrower', 'creditor')
        .order_by('title', 'pk')
    )


def _obligation_code(obligation):
    return f'O{obligation.pk}'


def _obligation_detail_text(user, obligation):
    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        f'{obligation.title} ({_obligation_code(obligation)})',
        f'Role: {role}',
        f'Counterparty: {_user_label(counterparty)}',
        f'Current balance: {_format_money(balance_units)}',
    ])


def _main_menu_markup():
    return {
        'inline_keyboard': [
            [{'text': 'Balance', 'callback_data': 'menu:balance'}],
            [{'text': 'Open obligations', 'callback_data': 'menu:obligations'}],
        ],
    }


def _obligations_menu_markup(user):
    rows = []
    for obligation in _open_obligations_for_user(user):
        balance_units = get_obligation_balance(obligation)
        rows.append([
            {
                'text': f'{obligation.title} - {_format_money(balance_units)}',
                'callback_data': f'ob:{obligation.pk}',
            }
        ])
    rows.append([{'text': 'Balance', 'callback_data': 'menu:balance'}])
    return {'inline_keyboard': rows}


def _obligation_detail_markup(obligation):
    return {
        'inline_keyboard': [
            [{'text': 'Add repayment', 'callback_data': f'repaymenu:{obligation.pk}'}],
            [{'text': 'Back to obligations', 'callback_data': 'menu:obligations'}],
            [{'text': 'Balance', 'callback_data': 'menu:balance'}],
        ],
    }


def _repayment_amount_markup(obligation, balance_units):
    rows = []
    used_amounts = set()
    quick_buttons = []
    for amount in QUICK_REPAYMENT_AMOUNTS:
        amount_units = units_from_decimal(amount)
        if amount_units <= balance_units:
            used_amounts.add(amount_units)
            quick_buttons.append({
                'text': f'Pay {_format_money(amount_units)}',
                'callback_data': f'repayamt:{obligation.pk}:{amount_units}',
            })
    for index in range(0, len(quick_buttons), 2):
        rows.append(quick_buttons[index:index + 2])

    if balance_units not in used_amounts:
        rows.append([
            {
                'text': f'Pay full balance {_format_money(balance_units)}',
                'callback_data': f'repayamt:{obligation.pk}:{balance_units}',
            }
        ])

    rows.append([{'text': 'Custom amount', 'callback_data': f'customrepay:{obligation.pk}'}])
    rows.append([{'text': 'Back', 'callback_data': f'ob:{obligation.pk}'}])
    return {'inline_keyboard': rows}


def _format_money(amount_units):
    return f'${decimal_from_units(amount_units):,.2f}'


def _format_signed_money(amount_units):
    sign = '+' if amount_units >= 0 else '-'
    return f'{sign}{_format_money(abs(amount_units))}'


def _validation_error_text(error):
    if hasattr(error, 'messages'):
        return ' '.join(error.messages)
    return str(error)


def _user_label(user):
    return user.get_full_name() or user.get_username()


def _confirmation_nonce():
    return secrets.token_urlsafe(6)


def _single_message(chat_id, text, reply_markup=None):
    return TelegramBotResult(messages=[TelegramOutgoingMessage(chat_id, text, reply_markup)])
