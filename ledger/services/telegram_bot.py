from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import secrets
import shlex

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ledger.models import (
    EventSeries,
    EventSeriesVersion,
    FinancialEvent,
    InterestRatePeriod,
    LedgerTransaction,
    Obligation,
    UserProfile,
)
from ledger.services.balances import get_obligation_balance
from ledger.services.events import post_principal_advance, post_repayment
from ledger.services.money import decimal_from_units, units_from_decimal


QUICK_REPAYMENT_AMOUNTS = (Decimal('25'), Decimal('50'), Decimal('100'))
PENDING_REPAYMENT_OBLIGATIONS = {}
PENDING_OBLIGATION_CREATIONS = {}
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
ROLE_LENT = 'lent'
ROLE_BORROWED = 'borrowed'
PAYMENT_MODE_ONE_TIME = 'one_time'
PAYMENT_MODE_RECURRING = 'recurring'


@dataclass(frozen=True)
class TelegramOutgoingMessage:
    chat_id: int
    text: str
    reply_markup: dict | None = None
    message_id: int | None = None
    replace_existing: bool = False


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

    if not text.startswith('/') and telegram_user_id in PENDING_OBLIGATION_CREATIONS:
        return _pending_obligation_creation_text(
            profile.user,
            telegram_user_id,
            chat_id,
            text,
            today,
        )

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
        _clear_pending_context(telegram_user_id)
        return _single_message(chat_id, _start_text(profile.user, today), reply_markup=_main_menu_markup())
    if command == '/balance':
        return _single_message(chat_id, _balance_text(profile.user), reply_markup=_main_menu_markup())
    if command in ('/new', '/newobligation'):
        return _single_message(chat_id, _new_obligation_role_text(), reply_markup=_new_obligation_role_markup())
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
    message_id = message.get('message_id')
    data = callback_query.get('data') or ''

    if not callback_query_id or not chat_id or not telegram_user_id:
        return TelegramBotResult()

    access_message = _access_error_message(chat, telegram_user_id)
    if access_message:
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, access_message)],
            callback_query_id=callback_query_id,
            callback_text='Access denied',
        )

    profile = _get_profile_for_telegram_id(telegram_user_id)
    if not profile:
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, 'Access is not configured for this Telegram ID.')],
            callback_query_id=callback_query_id,
            callback_text='Access denied',
        )

    if data == 'noop:cancel':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, 'Cancelled.', _main_menu_markup(include_home=True))],
            callback_query_id=callback_query_id,
            callback_text='Cancelled',
        )
    if data == 'menu:home':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _start_text(profile.user, today), _main_menu_markup())],
            callback_query_id=callback_query_id,
            callback_text='Home',
        )
    if data == 'menu:balance':
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _balance_text(profile.user), _main_menu_markup(include_home=True))],
            callback_query_id=callback_query_id,
            callback_text='Balance',
        )
    if data == 'menu:obligations':
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _obligations_menu_text(profile.user, today),
                    _obligations_menu_markup(profile.user),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text='Obligations',
        )
    if data == 'menu:recent':
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _recent_transactions_text(profile.user),
                    _main_menu_markup(include_home=True),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text='Recent',
        )
    if data == 'menu:new_obligation':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _new_obligation_role_text(), _new_obligation_role_markup())],
            callback_query_id=callback_query_id,
            callback_text='New obligation',
        )
    if data.startswith('ob:'):
        text, reply_markup = _obligation_callback_response(profile.user, data)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Obligation',
        )
    if data.startswith('repaymenu:'):
        text, reply_markup = _repayment_menu_callback_response(profile.user, data, today)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Repayment',
        )
    if data.startswith('customrepay:'):
        text, reply_markup = _custom_repayment_callback_response(
            profile.user,
            telegram_user_id,
            data,
            chat_id,
            message_id,
        )
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Custom amount',
        )
    if data.startswith('newob:'):
        text, reply_markup = _new_obligation_callback_response(
            profile.user,
            telegram_user_id,
            data,
            chat_id,
            message_id,
            today,
        )
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='New obligation',
        )
    if data.startswith('repayamt:'):
        text, reply_markup = _repayment_amount_callback_response(profile.user, data, today, nonce_factory)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text='Confirm repayment',
        )
    if data.startswith('repay:'):
        text = _confirm_repayment(profile.user, data, today)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, _main_menu_markup(include_home=True))],
            callback_query_id=callback_query_id,
            callback_text='Processed',
        )

    return TelegramBotResult(
        messages=[_panel_message(chat_id, message_id, 'This button is no longer supported.', _main_menu_markup(include_home=True))],
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
    i_owe_units, owed_to_me_units, net_units = _portfolio_totals(user, obligations)
    return '\n'.join([
        'TrustTrack',
        f'User: {_user_label(user)}',
        '',
        f'I owe: {_format_money(i_owe_units)}',
        f'Owed to me: {_format_money(owed_to_me_units)}',
        f'Net: {_format_signed_money(net_units)}',
        '',
        'Choose an action below.',
    ])


def _help_text(user):
    return '\n'.join([
        f'TrustTrack access confirmed for {_user_label(user)}.',
        '',
        'Use the buttons below to check balance, open an obligation, or record a repayment.',
    ])


def _balance_text(user):
    obligations = list(_open_obligations_for_user(user))
    i_owe_units, owed_to_me_units, net_units = _portfolio_totals(user, obligations)
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


def _recent_transactions_text(user):
    obligations = list(_related_obligations_for_user(user))
    if not obligations:
        return 'No transactions yet.'

    transactions = (
        LedgerTransaction.objects
        .filter(obligation__in=obligations, status=LedgerTransaction.Status.POSTED)
        .select_related('obligation', 'financial_event')
        .order_by('-transaction_date', '-created_at')[:10]
    )
    if not transactions:
        return 'No transactions yet.'

    lines = ['Recent transactions:', '']
    for transaction_item in transactions:
        event = transaction_item.financial_event
        lines.append(
            ' - '.join([
                transaction_item.transaction_date.isoformat(),
                transaction_item.obligation.title,
                event.get_event_type_display(),
                _format_signed_money(_event_user_net_impact_units(user, event, transaction_item.obligation)),
            ])
        )
    return '\n'.join(lines)


def _portfolio_totals(user, obligations):
    i_owe_units = 0
    owed_to_me_units = 0
    for obligation in obligations:
        balance_units = get_obligation_balance(obligation)
        if obligation.borrower_id == user.id:
            i_owe_units += balance_units
        if obligation.creditor_id == user.id:
            owed_to_me_units += balance_units

    net_units = owed_to_me_units - i_owe_units
    return i_owe_units, owed_to_me_units, net_units


def _event_user_net_impact_units(user, event, obligation):
    if event.direction == FinancialEvent.Direction.INCREASES_DEBT:
        return event.amount_units if obligation.creditor_id == user.id else -event.amount_units
    return -event.amount_units if obligation.creditor_id == user.id else event.amount_units


def _obligation_text(user, args_text):
    obligation = _find_obligation_from_code(user, args_text.strip())
    if not obligation:
        return 'Choose an obligation from the buttons in /start.'

    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        obligation.title,
        f'Role: {role}',
        f'Counterparty: {_user_label(counterparty)}',
        f'Current balance: {_format_money(balance_units)}',
        '',
        'Use the repayment button below.',
    ])


def _repayment_preview(user, chat_id, args_text, today, nonce_factory):
    parsed = _parse_repayment_args(args_text, today)
    if 'error' in parsed:
        return _single_message(chat_id, parsed['error'])

    obligation = _find_obligation_from_code(user, parsed['code'])
    if not obligation:
        return _single_message(chat_id, 'Unknown obligation. Use /start and choose it from the buttons.')

    return _repayment_preview_for_obligation(
        chat_id=chat_id,
        obligation=obligation,
        amount_units=parsed['amount_units'],
        event_date=parsed['event_date'],
        today=today,
        nonce_factory=nonce_factory,
    )


def _pending_repayment_preview(user, telegram_user_id, chat_id, amount_text, today, nonce_factory):
    pending_context = PENDING_REPAYMENT_OBLIGATIONS.get(telegram_user_id)
    if isinstance(pending_context, dict):
        obligation_id = pending_context.get('obligation_id')
        panel_chat_id = pending_context.get('chat_id') or chat_id
        panel_message_id = pending_context.get('message_id')
    else:
        obligation_id = pending_context
        panel_chat_id = chat_id
        panel_message_id = None

    obligation = _get_related_open_obligation(user, obligation_id)
    if not obligation:
        PENDING_REPAYMENT_OBLIGATIONS.pop(telegram_user_id, None)
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            'This obligation is not available anymore.',
            reply_markup=_main_menu_markup(),
        )

    amount_units, error = _parse_amount_units(amount_text)
    if error:
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            f'{error}\nSend only the amount, for example: 37.50',
            reply_markup=_repayment_amount_markup(obligation, get_obligation_balance(obligation, as_of=today)),
        )

    PENDING_REPAYMENT_OBLIGATIONS.pop(telegram_user_id, None)
    return _repayment_preview_for_obligation(
        chat_id=panel_chat_id,
        obligation=obligation,
        amount_units=amount_units,
        event_date=today,
        today=today,
        nonce_factory=nonce_factory,
        message_id=panel_message_id,
    )


def _repayment_preview_for_obligation(chat_id, obligation, amount_units, event_date, today, nonce_factory, message_id=None):
    balance_units = get_obligation_balance(obligation, as_of=event_date)
    if amount_units > balance_units:
        return _panel_result(
            chat_id,
            message_id,
            f'Repayment cannot exceed the balance on {event_date.isoformat()}.\n'
            f'Balance: {_format_money(balance_units)}',
            reply_markup=_obligation_detail_markup(obligation),
        )

    nonce = nonce_factory()
    callback_data = f'repay:{obligation.pk}:{amount_units}:{event_date.isoformat()}:{nonce}'
    text = '\n'.join([
        'Confirm repayment',
        f'Obligation: {obligation.title}',
        f'Amount: {_format_money(amount_units)}',
        f'Date: {event_date.isoformat()}',
    ])
    return _panel_result(
        chat_id,
        message_id,
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
            f'Record repayment for {obligation.title}',
            f'Current balance: {_format_money(balance_units)}',
            '',
            'Choose an amount:',
        ]),
        _repayment_amount_markup(obligation, balance_units),
    )


def _custom_repayment_callback_response(user, telegram_user_id, data, chat_id, message_id):
    obligation = _get_obligation_from_callback(user, data, 'customrepay')
    if not obligation:
        return 'This obligation is not available anymore.', _main_menu_markup()

    PENDING_REPAYMENT_OBLIGATIONS[telegram_user_id] = {
        'obligation_id': obligation.pk,
        'chat_id': chat_id,
        'message_id': message_id,
    }
    return (
        '\n'.join([
            f'Custom repayment for {obligation.title}',
            'Send only the amount, for example: 37.50',
        ]),
        _obligation_detail_markup(obligation),
    )


def _new_obligation_callback_response(user, telegram_user_id, data, chat_id, message_id, today):
    parts = data.split(':')
    if len(parts) < 2:
        return 'This action is not available anymore.', _main_menu_markup(include_home=True)

    action = parts[1]
    if action == 'role' and len(parts) == 3:
        role = parts[2]
        if role not in (ROLE_LENT, ROLE_BORROWED):
            return 'Unsupported role.', _new_obligation_role_markup()
        PENDING_OBLIGATION_CREATIONS[telegram_user_id] = {
            'chat_id': chat_id,
            'message_id': message_id,
            'role': role,
            'step': 'counterparty',
        }
        return _new_obligation_counterparty_text(role), _new_obligation_counterparty_markup(user)

    context = PENDING_OBLIGATION_CREATIONS.get(telegram_user_id)
    if not context:
        return _new_obligation_role_text(), _new_obligation_role_markup()
    context['chat_id'] = chat_id
    context['message_id'] = message_id

    if action == 'cancel':
        PENDING_OBLIGATION_CREATIONS.pop(telegram_user_id, None)
        return 'New obligation was cancelled.', _main_menu_markup(include_home=True)

    if action == 'cp' and len(parts) == 3:
        counterparty = _get_counterparty(user, parts[2])
        if not counterparty:
            return 'Choose an active counterparty from the list.', _new_obligation_counterparty_markup(user)
        context['counterparty_id'] = counterparty.pk
        context['step'] = 'title'
        return _new_obligation_title_text(context, counterparty), _new_obligation_cancel_markup()

    if action == 'date' and len(parts) == 3:
        if parts[2] == 'today':
            context['opened_on'] = today
            context['step'] = 'schedule'
            return _new_obligation_schedule_text(context), _new_obligation_schedule_markup()
        if parts[2] == 'custom':
            context['step'] = 'opened_on'
            return 'Send opened date as YYYY-MM-DD.', _new_obligation_cancel_markup()
        return 'Choose a date option.', _new_obligation_date_markup()

    if action == 'schedule' and len(parts) == 3:
        return _new_obligation_schedule_callback(context, parts[2])

    if action == 'dom' and len(parts) == 3:
        day_of_month, error = _parse_day_of_month(parts[2])
        if error:
            return error, _new_obligation_month_day_markup(context)
        context['recurring_day_of_month'] = day_of_month
        context['step'] = 'interest'
        return _new_obligation_interest_text(context), _new_obligation_interest_markup()

    if action == 'interest' and len(parts) == 3:
        if parts[2] == 'no':
            context['has_interest'] = False
            context['annual_rate_percent'] = None
            context['step'] = 'confirm'
            return _new_obligation_confirm_text(user, context), _new_obligation_confirm_markup()
        if parts[2] == 'yes':
            context['has_interest'] = True
            context['step'] = 'interest_rate'
            return 'Send annual interest rate, for example: 3.5', _new_obligation_cancel_markup()
        return 'Choose an interest option.', _new_obligation_interest_markup()

    if action == 'create':
        try:
            obligation = _create_obligation_from_context(user, context)
        except (ValidationError, ValueError) as error:
            return _validation_error_text(error), _new_obligation_confirm_markup()
        PENDING_OBLIGATION_CREATIONS.pop(telegram_user_id, None)
        return _new_obligation_created_text(obligation), _main_menu_markup(include_home=True)

    return 'This action is not available anymore.', _main_menu_markup(include_home=True)


def _pending_obligation_creation_text(user, telegram_user_id, chat_id, text, today):
    context = PENDING_OBLIGATION_CREATIONS.get(telegram_user_id)
    if not context:
        return _single_message(chat_id, _unknown_command_text(user), reply_markup=_main_menu_markup())

    panel_chat_id = context.get('chat_id') or chat_id
    panel_message_id = context.get('message_id')
    step = context.get('step')

    if step == 'counterparty':
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            _new_obligation_counterparty_text(context.get('role')),
            _new_obligation_counterparty_markup(user),
        )

    if step == 'title':
        title = text.strip()
        if not title or len(title) > 160:
            return _panel_result(panel_chat_id, panel_message_id, 'Send a title up to 160 characters.', _new_obligation_cancel_markup())
        context['title'] = title
        context['step'] = 'amount'
        return _panel_result(panel_chat_id, panel_message_id, 'Send initial amount, for example: 625.00', _new_obligation_cancel_markup())

    if step == 'amount':
        amount_units, error = _parse_amount_units(text)
        if error:
            return _panel_result(panel_chat_id, panel_message_id, error, _new_obligation_cancel_markup())
        context['amount_units'] = amount_units
        context['step'] = 'opened_on_choice'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_date_text(), _new_obligation_date_markup())

    if step == 'opened_on':
        try:
            context['opened_on'] = date.fromisoformat(text.strip())
        except ValueError:
            return _panel_result(panel_chat_id, panel_message_id, 'Opened date must use YYYY-MM-DD.', _new_obligation_cancel_markup())
        context['step'] = 'schedule'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_schedule_text(context), _new_obligation_schedule_markup())

    if step == 'opened_on_choice':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_date_text(), _new_obligation_date_markup())

    if step == 'schedule':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_schedule_text(context), _new_obligation_schedule_markup())

    if step == 'day_of_month':
        day_of_month, error = _parse_day_of_month(text)
        if error:
            return _panel_result(panel_chat_id, panel_message_id, error, _new_obligation_month_day_markup(context))
        context['recurring_day_of_month'] = day_of_month
        context['step'] = 'interest'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_interest_text(context), _new_obligation_interest_markup())

    if step == 'interest_rate':
        try:
            annual_rate_percent = Decimal(text.strip())
        except InvalidOperation:
            return _panel_result(panel_chat_id, panel_message_id, 'Interest rate must be a number, for example: 3.5', _new_obligation_cancel_markup())
        if annual_rate_percent <= 0:
            return _panel_result(panel_chat_id, panel_message_id, 'Interest rate must be greater than zero.', _new_obligation_cancel_markup())
        context['annual_rate_percent'] = annual_rate_percent
        context['step'] = 'confirm'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_confirm_text(user, context), _new_obligation_confirm_markup())

    if step == 'interest':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_interest_text(context), _new_obligation_interest_markup())

    return _panel_result(panel_chat_id, panel_message_id, _new_obligation_confirm_text(user, context), _new_obligation_confirm_markup())


def _new_obligation_schedule_callback(context, schedule):
    opened_on = context.get('opened_on')
    if not opened_on:
        return _new_obligation_date_text(), _new_obligation_date_markup()

    if schedule == PAYMENT_MODE_ONE_TIME:
        context['payment_mode'] = PAYMENT_MODE_ONE_TIME
        context['step'] = 'interest'
        return _new_obligation_interest_text(context), _new_obligation_interest_markup()

    if schedule == EventSeries.Frequency.MONTHLY:
        context['payment_mode'] = PAYMENT_MODE_RECURRING
        context['recurring_frequency'] = EventSeries.Frequency.MONTHLY
        context['recurring_starts_on'] = opened_on
        context['step'] = 'day_of_month'
        return _new_obligation_month_day_text(opened_on), _new_obligation_month_day_markup(context)

    if schedule in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY):
        context['payment_mode'] = PAYMENT_MODE_RECURRING
        context['recurring_frequency'] = schedule
        context['recurring_day_of_week'] = opened_on.weekday()
        context['recurring_starts_on'] = opened_on
        context['step'] = 'interest'
        return _new_obligation_interest_text(context), _new_obligation_interest_markup()

    return 'Choose a schedule option.', _new_obligation_schedule_markup()


def _create_obligation_from_context(user, context):
    required = ('role', 'counterparty_id', 'title', 'amount_units', 'opened_on', 'payment_mode')
    if any(context.get(field_name) in (None, '') for field_name in required):
        raise ValueError('New obligation is incomplete.')

    counterparty = _get_counterparty(user, context['counterparty_id'])
    if not counterparty:
        raise ValueError('Counterparty is not available anymore.')

    if context['role'] == ROLE_LENT:
        creditor = user
        borrower = counterparty
    else:
        creditor = counterparty
        borrower = user

    with transaction.atomic():
        obligation = Obligation(
            creditor=creditor,
            borrower=borrower,
            title=context['title'],
            opened_on=context['opened_on'],
        )
        obligation.full_clean()
        obligation.save()
        post_principal_advance(
            obligation,
            amount_units=context['amount_units'],
            event_date=context['opened_on'],
            memo=f'Telegram obligation created by {_user_label(user)}',
            category='telegram',
        )
        _create_recurring_series_from_context(obligation, context)
        _create_interest_rate_from_context(obligation, context)
    return obligation


def _create_recurring_series_from_context(obligation, context):
    if context.get('payment_mode') != PAYMENT_MODE_RECURRING:
        return None

    series = EventSeries(
        obligation=obligation,
        name=context['title'],
        event_type=FinancialEvent.EventType.SCHEDULED_CHARGE,
        frequency=context['recurring_frequency'],
        day_of_month=context.get('recurring_day_of_month'),
        day_of_week=context.get('recurring_day_of_week'),
        starts_on=context.get('recurring_starts_on') or context['opened_on'],
        memo='Created from Telegram.',
    )
    series.full_clean()
    series.save()

    version = EventSeriesVersion(
        event_series=series,
        amount_units=context['amount_units'],
        valid_from=series.starts_on,
        memo='Created from Telegram.',
    )
    version.full_clean()
    version.save()
    return series


def _create_interest_rate_from_context(obligation, context):
    if not context.get('has_interest'):
        return None

    rate = InterestRatePeriod(
        obligation=obligation,
        annual_rate_percent=context['annual_rate_percent'],
        effective_from=context['opened_on'],
        memo='Created from Telegram.',
    )
    rate.full_clean()
    rate.save()
    return rate


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
        f'Obligation: {obligation.title}',
        f'Amount: {_format_money(amount_units)}',
        f'Date: {event_date.isoformat()}',
        f'Current balance: {_format_money(balance_units)}',
    ])


def _parse_repayment_args(args_text, today):
    try:
        tokens = shlex.split(args_text)
    except ValueError:
        return {'error': 'Could not parse repayment command. Use /start and choose repayment from the buttons.'}

    if len(tokens) < 2:
        return {'error': 'Use /start and choose repayment from the buttons.'}

    code = tokens[0].upper()
    amount_units, error = _parse_amount_units(tokens[1])
    if error:
        return {'error': error}

    event_date = today
    if len(tokens) >= 3:
        try:
            event_date = date.fromisoformat(tokens[2])
        except ValueError:
            return {'error': 'Repayment date must use YYYY-MM-DD.'}

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
        f'{obligation.title} - {role} {_user_label(counterparty)} - {_format_money(balance_units)}'
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


def _parse_day_of_month(value):
    try:
        day_of_month = int(str(value).strip())
    except (TypeError, ValueError):
        return None, 'Day of month must be a number from 1 to 31.'
    if day_of_month < 1 or day_of_month > 31:
        return None, 'Day of month must be from 1 to 31.'
    return day_of_month, ''


def _available_counterparties(user):
    return (
        get_user_model()
        .objects
        .filter(is_active=True)
        .exclude(pk=user.pk)
        .order_by('username')
    )


def _get_counterparty(user, raw_user_id):
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return None
    return _available_counterparties(user).filter(pk=user_id).first()


def _clear_pending_context(telegram_user_id):
    PENDING_REPAYMENT_OBLIGATIONS.pop(telegram_user_id, None)
    PENDING_OBLIGATION_CREATIONS.pop(telegram_user_id, None)


def _get_related_open_obligation(user, obligation_id):
    return _open_obligations_for_user(user).filter(pk=obligation_id).first()


def _related_obligations_for_user(user):
    return (
        Obligation.objects
        .filter(Q(creditor=user) | Q(borrower=user))
        .select_related('borrower', 'creditor')
        .order_by('title', 'pk')
    )


def _open_obligations_for_user(user):
    return (
        _related_obligations_for_user(user)
        .filter(status=Obligation.Status.OPEN)
    )


def _obligation_detail_text(user, obligation):
    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        obligation.title,
        f'Role: {role}',
        f'Counterparty: {_user_label(counterparty)}',
        f'Current balance: {_format_money(balance_units)}',
    ])


def _main_menu_markup(include_home=False):
    rows = []
    if include_home:
        rows.append([{'text': 'Home', 'callback_data': 'menu:home'}])
    rows.extend([
        [{'text': 'Balance', 'callback_data': 'menu:balance'}],
        [{'text': 'Open obligations', 'callback_data': 'menu:obligations'}],
        [{'text': 'Recent transactions', 'callback_data': 'menu:recent'}],
    ])
    return {'inline_keyboard': rows}


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
    rows.append([{'text': 'New obligation', 'callback_data': 'menu:new_obligation'}])
    rows.append([
        {'text': 'Home', 'callback_data': 'menu:home'},
        {'text': 'Balance', 'callback_data': 'menu:balance'},
    ])
    return {'inline_keyboard': rows}


def _obligation_detail_markup(obligation):
    return {
        'inline_keyboard': [
            [{'text': 'Add repayment', 'callback_data': f'repaymenu:{obligation.pk}'}],
            [{'text': 'Back to obligations', 'callback_data': 'menu:obligations'}],
            [
                {'text': 'Home', 'callback_data': 'menu:home'},
                {'text': 'Balance', 'callback_data': 'menu:balance'},
            ],
        ],
    }


def _new_obligation_role_text():
    return '\n'.join([
        'New obligation',
        '',
        'Who are you in this obligation?',
    ])


def _new_obligation_role_markup():
    return {
        'inline_keyboard': [
            [{'text': 'I lent money', 'callback_data': f'newob:role:{ROLE_LENT}'}],
            [{'text': 'I borrowed money', 'callback_data': f'newob:role:{ROLE_BORROWED}'}],
            [{'text': 'Home', 'callback_data': 'menu:home'}],
        ],
    }


def _new_obligation_counterparty_text(role):
    direction = 'Who borrowed from you?' if role == ROLE_LENT else 'Who lent money to you?'
    return '\n'.join([
        'New obligation',
        '',
        direction,
    ])


def _new_obligation_counterparty_markup(user):
    rows = []
    for counterparty in _available_counterparties(user):
        rows.append([{'text': _user_label(counterparty), 'callback_data': f'newob:cp:{counterparty.pk}'}])
    rows.append([{'text': 'Cancel', 'callback_data': 'newob:cancel'}])
    return {'inline_keyboard': rows}


def _new_obligation_title_text(context, counterparty):
    return '\n'.join([
        'New obligation',
        f'Role: {_role_label(context["role"])}',
        f'Counterparty: {_user_label(counterparty)}',
        '',
        'Send title.',
    ])


def _new_obligation_cancel_markup():
    return {
        'inline_keyboard': [
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
            [{'text': 'Home', 'callback_data': 'menu:home'}],
        ],
    }


def _new_obligation_date_text():
    return '\n'.join([
        'New obligation',
        '',
        'Choose opened date.',
    ])


def _new_obligation_date_markup():
    return {
        'inline_keyboard': [
            [{'text': 'Today', 'callback_data': 'newob:date:today'}],
            [{'text': 'Custom date', 'callback_data': 'newob:date:custom'}],
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
        ],
    }


def _new_obligation_schedule_text(context):
    return '\n'.join([
        'New obligation',
        f'Title: {context.get("title", "")}',
        f'Amount: {_format_money(context.get("amount_units", 0))}',
        f'Opened: {context["opened_on"].isoformat()}',
        '',
        'Choose payment schedule.',
    ])


def _new_obligation_schedule_markup():
    return {
        'inline_keyboard': [
            [{'text': 'One-time payment', 'callback_data': f'newob:schedule:{PAYMENT_MODE_ONE_TIME}'}],
            [{'text': 'Monthly recurring', 'callback_data': f'newob:schedule:{EventSeries.Frequency.MONTHLY}'}],
            [{'text': 'Weekly recurring', 'callback_data': f'newob:schedule:{EventSeries.Frequency.WEEKLY}'}],
            [{'text': 'Every 2 weeks', 'callback_data': f'newob:schedule:{EventSeries.Frequency.BIWEEKLY}'}],
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
        ],
    }


def _new_obligation_month_day_text(opened_on):
    return '\n'.join([
        'Monthly recurring',
        '',
        'Send day of month from 1 to 31, or use the opened-date day.',
    ])


def _new_obligation_month_day_markup(context):
    opened_on = context.get('opened_on') or timezone.localdate()
    return {
        'inline_keyboard': [
            [{'text': f'Use day {opened_on.day}', 'callback_data': f'newob:dom:{opened_on.day}'}],
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
        ],
    }


def _new_obligation_interest_text(context):
    return '\n'.join([
        'New obligation',
        f'Title: {context.get("title", "")}',
        f'Schedule: {_new_obligation_schedule_label(context)}',
        '',
        'Add interest?',
    ])


def _new_obligation_interest_markup():
    return {
        'inline_keyboard': [
            [{'text': 'No interest', 'callback_data': 'newob:interest:no'}],
            [{'text': 'With interest', 'callback_data': 'newob:interest:yes'}],
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
        ],
    }


def _new_obligation_confirm_text(user, context):
    counterparty = get_user_model().objects.filter(pk=context.get('counterparty_id')).first()
    counterparty_label = _user_label(counterparty) if counterparty else 'Unknown'
    interest_label = 'No'
    if context.get('has_interest'):
        interest_label = f'{context["annual_rate_percent"]}% APR'
    return '\n'.join([
        'Create obligation?',
        f'Title: {context.get("title", "")}',
        f'Role: {_role_label(context.get("role"))}',
        f'Counterparty: {counterparty_label}',
        f'Amount: {_format_money(context.get("amount_units", 0))}',
        f'Opened: {context.get("opened_on").isoformat()}',
        f'Schedule: {_new_obligation_schedule_label(context)}',
        f'Interest: {interest_label}',
    ])


def _new_obligation_confirm_markup():
    return {
        'inline_keyboard': [
            [{'text': 'Create obligation', 'callback_data': 'newob:create'}],
            [{'text': 'Cancel', 'callback_data': 'newob:cancel'}],
        ],
    }


def _new_obligation_created_text(obligation):
    return '\n'.join([
        'Obligation created.',
        f'Title: {obligation.title}',
        f'Current balance: {_format_money(get_obligation_balance(obligation))}',
    ])


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


def _role_label(role):
    if role == ROLE_LENT:
        return 'I lent money'
    if role == ROLE_BORROWED:
        return 'I borrowed money'
    return 'Unknown'


def _new_obligation_schedule_label(context):
    if context.get('payment_mode') == PAYMENT_MODE_ONE_TIME:
        return 'One-time payment'

    frequency = context.get('recurring_frequency')
    if frequency == EventSeries.Frequency.MONTHLY:
        return f'Monthly, day {context.get("recurring_day_of_month")}'
    if frequency == EventSeries.Frequency.WEEKLY:
        day_name = DAY_NAMES[context.get('recurring_day_of_week') or 0]
        return f'Weekly on {day_name}'
    if frequency == EventSeries.Frequency.BIWEEKLY:
        day_name = DAY_NAMES[context.get('recurring_day_of_week') or 0]
        return f'Every 2 weeks on {day_name}'
    return 'Recurring'


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


def _panel_result(chat_id, message_id, text, reply_markup=None):
    return TelegramBotResult(messages=[_panel_message(chat_id, message_id, text, reply_markup)])


def _panel_message(chat_id, message_id, text, reply_markup=None):
    return TelegramOutgoingMessage(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        message_id=message_id,
        replace_existing=message_id is not None,
    )
