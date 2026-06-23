from dataclasses import dataclass, field
from datetime import date, datetime
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
from ledger.services.notifications import send_obligation_created_notification
from ledger.services.recalculation import recalculate_obligation
from ledger.services.telegram_login import (
    confirm_challenge_by_code,
    confirm_challenge_by_start_payload,
)


QUICK_REPAYMENT_AMOUNTS = (Decimal('25'), Decimal('50'), Decimal('100'))
PENDING_MANUAL_TRANSFERS = {}
PENDING_REPAYMENT_OBLIGATIONS = PENDING_MANUAL_TRANSFERS
PENDING_OBLIGATION_CREATIONS = {}
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
ROLE_LENT = 'lent'
ROLE_BORROWED = 'borrowed'
PAYMENT_MODE_ONE_TIME = 'one_time'
PAYMENT_MODE_RECURRING = 'recurring'
LANG_EN = UserProfile.TelegramLanguage.ENGLISH
LANG_RU = UserProfile.TelegramLanguage.RUSSIAN
SUPPORTED_TELEGRAM_LANGUAGES = {LANG_EN, LANG_RU}
BUTTON_STYLE_PRIMARY = 'primary'
BUTTON_STYLE_SUCCESS = 'success'
BUTTON_STYLE_DANGER = 'danger'


TELEGRAM_TEXT = {
    'access_private_chat': {
        LANG_EN: 'Please use TrustTrack bot in a private chat.',
        LANG_RU: 'Пожалуйста, используйте бота TrustTrack в личном чате.',
    },
    'access_missing_user_id': {
        LANG_EN: 'Telegram user ID was not provided.',
        LANG_RU: 'Telegram ID пользователя не передан.',
    },
    'access_not_configured': {
        LANG_EN: 'Access is not configured for this Telegram ID.',
        LANG_RU: 'Доступ для этого Telegram ID не настроен.',
    },
    'access_add_profile': {
        LANG_EN: 'Add this ID in your TrustTrack Profile first.',
        LANG_RU: 'Сначала добавьте этот ID в профиле TrustTrack.',
    },
    'access_denied': {LANG_EN: 'Access denied', LANG_RU: 'Доступ запрещён'},
    'cancelled': {LANG_EN: 'Cancelled.', LANG_RU: 'Отменено.'},
    'cancel': {LANG_EN: 'Cancel', LANG_RU: 'Отмена'},
    'home': {LANG_EN: 'Home', LANG_RU: 'Домой'},
    'balance': {LANG_EN: 'Balance', LANG_RU: 'Баланс'},
    'open_obligations': {LANG_EN: 'Open obligations', LANG_RU: 'Открытые обязательства'},
    'recent_transactions': {LANG_EN: 'Recent transactions', LANG_RU: 'Последние операции'},
    'user': {LANG_EN: 'User', LANG_RU: 'Пользователь'},
    'i_owe': {LANG_EN: 'I owe', LANG_RU: 'Я должен'},
    'owed_to_me': {LANG_EN: 'Owed to me', LANG_RU: 'Мне должны'},
    'net': {LANG_EN: 'Net', LANG_RU: 'Итого'},
    'choose_action': {LANG_EN: 'Choose an action below.', LANG_RU: 'Выберите действие ниже.'},
    'help_access': {
        LANG_EN: 'TrustTrack access confirmed for {user}.',
        LANG_RU: 'Доступ к TrustTrack подтверждён для {user}.',
    },
    'help_buttons': {
        LANG_EN: 'Use the buttons below to check balance, open an obligation, or record a manual transfer.',
        LANG_RU: 'Используйте кнопки ниже, чтобы проверить баланс, открыть обязательство или внести ручную операцию.',
    },
    'balance_title': {LANG_EN: 'TrustTrack balance', LANG_RU: 'Баланс TrustTrack'},
    'no_transactions': {LANG_EN: 'No transactions yet.', LANG_RU: 'Операций пока нет.'},
    'no_open_obligations': {LANG_EN: 'No open obligations yet.', LANG_RU: 'Открытых обязательств пока нет.'},
    'recent_transactions_title': {LANG_EN: 'Recent transactions:', LANG_RU: 'Последние операции:'},
    'choose_obligation_below': {
        LANG_EN: 'Choose an obligation below.',
        LANG_RU: 'Выберите обязательство ниже.',
    },
    'choose_obligation_start': {
        LANG_EN: 'Choose an obligation from the buttons in /start.',
        LANG_RU: 'Выберите обязательство кнопками в /start.',
    },
    'unknown_obligation': {
        LANG_EN: 'Unknown obligation. Use /start and choose it from the buttons.',
        LANG_RU: 'Обязательство не найдено. Используйте /start и выберите его кнопками.',
    },
    'unavailable_obligation': {
        LANG_EN: 'This obligation is not available anymore.',
        LANG_RU: 'Это обязательство больше недоступно.',
    },
    'unsupported_button': {
        LANG_EN: 'This button is no longer supported.',
        LANG_RU: 'Эта кнопка больше не поддерживается.',
    },
    'unsupported_action': {
        LANG_EN: 'This action is not available anymore.',
        LANG_RU: 'Это действие больше недоступно.',
    },
    'settings_title': {LANG_EN: 'Settings', LANG_RU: 'Настройки'},
    'settings_language': {LANG_EN: 'Language', LANG_RU: 'Язык'},
    'settings_current_language': {
        LANG_EN: 'Current language: {language}',
        LANG_RU: 'Текущий язык: {language}',
    },
    'settings_choose_language': {
        LANG_EN: 'Choose bot language.',
        LANG_RU: 'Выберите язык бота.',
    },
    'settings_language_updated': {
        LANG_EN: 'Language updated.',
        LANG_RU: 'Язык обновлён.',
    },
    'settings_notifications': {
        LANG_EN: 'Balance notifications',
        LANG_RU: 'Уведомления о балансе',
    },
    'settings_notifications_current': {
        LANG_EN: 'Balance notifications: {status}',
        LANG_RU: 'Уведомления о балансе: {status}',
    },
    'settings_notifications_hint': {
        LANG_EN: 'You will be notified when obligations are opened or due jobs change balances.',
        LANG_RU: 'Вы получите уведомление, когда обязательства создаются или плановые задачи меняют баланс.',
    },
    'settings_notifications_updated': {
        LANG_EN: 'Notification settings updated.',
        LANG_RU: 'Настройки уведомлений обновлены.',
    },
    'settings_notifications_enable': {
        LANG_EN: 'Turn notifications on',
        LANG_RU: 'Включить уведомления',
    },
    'settings_notifications_disable': {
        LANG_EN: 'Turn notifications off',
        LANG_RU: 'Отключить уведомления',
    },
    'settings_on': {LANG_EN: 'on', LANG_RU: 'включены'},
    'settings_off': {LANG_EN: 'off', LANG_RU: 'отключены'},
    'processed': {LANG_EN: 'Processed', LANG_RU: 'Готово'},
    'telegram_login_confirmed': {
        LANG_EN: 'Web login confirmed for {user}. Return to the browser.',
        LANG_RU: 'Вход на сайте подтверждён для {user}. Вернитесь в браузер.',
    },
    'telegram_login_not_found': {
        LANG_EN: 'Login code was not found. Create a new code from the TrustTrack login page.',
        LANG_RU: 'Код входа не найден. Создайте новый код на странице входа TrustTrack.',
    },
    'telegram_login_expired': {
        LANG_EN: 'Login code expired. Create a new code from the TrustTrack login page.',
        LANG_RU: 'Код входа истёк. Создайте новый код на странице входа TrustTrack.',
    },
    'telegram_login_consumed': {
        LANG_EN: 'Login code was already used. Create a new code from the TrustTrack login page.',
        LANG_RU: 'Код входа уже использован. Создайте новый код на странице входа TrustTrack.',
    },
    'telegram_login_access_denied': {
        LANG_EN: 'This Telegram ID is not allowed to confirm that login.',
        LANG_RU: 'Этот Telegram ID не может подтвердить этот вход.',
    },
    'language_english': {LANG_EN: 'English', LANG_RU: 'английский'},
    'language_russian': {LANG_EN: 'Russian', LANG_RU: 'русский'},
    'unknown_command': {LANG_EN: 'Unknown command.', LANG_RU: 'Неизвестная команда.'},
    'unknown_command_hint': {
        LANG_EN: 'Use /start to open the TrustTrack menu for {user}.',
        LANG_RU: 'Используйте /start, чтобы открыть меню TrustTrack для {user}.',
    },
    'role': {LANG_EN: 'Your role', LANG_RU: 'Ваша роль'},
    'counterparty': {LANG_EN: 'Counterparty', LANG_RU: 'Вторая сторона'},
    'current_balance': {LANG_EN: 'Current balance', LANG_RU: 'Текущий баланс'},
    'borrower': {LANG_EN: 'borrower', LANG_RU: 'заёмщик'},
    'creditor': {LANG_EN: 'creditor', LANG_RU: 'кредитор'},
    'use_repayment_button': {
        LANG_EN: 'Use the manual transfer button below.',
        LANG_RU: 'Используйте кнопку ручной операции ниже.',
    },
    'repayment_parse_error': {
        LANG_EN: 'Could not parse repayment command. Use /start and choose repayment from the buttons.',
        LANG_RU: 'Не удалось разобрать команду погашения. Используйте /start и выберите погашение кнопками.',
    },
    'repayment_choose_buttons': {
        LANG_EN: 'Use /start and choose repayment from the buttons.',
        LANG_RU: 'Используйте /start и выберите погашение кнопками.',
    },
    'repayment_date_format': {
        LANG_EN: 'Repayment date must use MM/DD/YYYY.',
        LANG_RU: 'Дата погашения должна быть в формате MM/DD/YYYY.',
    },
    'repayment_future_date': {
        LANG_EN: 'Repayment date cannot be in the future.',
        LANG_RU: 'Дата погашения не может быть в будущем.',
    },
    'amount_number_error': {
        LANG_EN: 'Amount must be a number.',
        LANG_RU: 'Сумма должна быть числом.',
    },
    'amount_positive_error': {
        LANG_EN: 'Amount must be greater than zero.',
        LANG_RU: 'Сумма должна быть больше нуля.',
    },
    'amount_example': {
        LANG_EN: 'Send only the amount, for example: 37.50 or 37,50',
        LANG_RU: 'Отправьте только сумму, например: 37.50 или 37,50',
    },
    'repayment_exceeds_balance': {
        LANG_EN: 'Repayment cannot exceed the balance on {date}.\nBalance: {balance}',
        LANG_RU: 'Погашение не может превышать баланс на {date}.\nБаланс: {balance}',
    },
    'confirm_repayment': {LANG_EN: 'Confirm repayment', LANG_RU: 'Подтвердить погашение'},
    'confirm_debt_increase': {
        LANG_EN: 'Confirm debt increase',
        LANG_RU: 'Подтвердить увеличение долга',
    },
    'obligation': {LANG_EN: 'Obligation', LANG_RU: 'Обязательство'},
    'action': {LANG_EN: 'Action', LANG_RU: 'Действие'},
    'amount': {LANG_EN: 'Amount', LANG_RU: 'Сумма'},
    'date': {LANG_EN: 'Date', LANG_RU: 'Дата'},
    'manual_transfer': {LANG_EN: 'Manual transfer', LANG_RU: 'Ручная операция'},
    'choose_transfer_action': {
        LANG_EN: 'What do you want to record?',
        LANG_RU: 'Что нужно записать?',
    },
    'transfer_repayment': {LANG_EN: 'Repayment', LANG_RU: 'Погашение'},
    'transfer_debt_increase': {
        LANG_EN: 'Debt increase',
        LANG_RU: 'Увеличение долга',
    },
    'record_repayment': {
        LANG_EN: 'Record repayment for {title}',
        LANG_RU: 'Внести погашение для {title}',
    },
    'choose_amount': {LANG_EN: 'Choose an amount:', LANG_RU: 'Выберите сумму:'},
    'custom_repayment': {
        LANG_EN: 'Custom repayment for {title}',
        LANG_RU: 'Своя сумма погашения для {title}',
    },
    'custom_debt_increase': {
        LANG_EN: 'Debt increase for {title}',
        LANG_RU: 'Увеличение долга для {title}',
    },
    'no_balance_to_repay': {
        LANG_EN: '{title} has no balance to repay.',
        LANG_RU: 'У {title} нет баланса для погашения.',
    },
    'invalid_repayment_amount': {
        LANG_EN: 'Invalid repayment amount.',
        LANG_RU: 'Некорректная сумма погашения.',
    },
    'invalid_repayment_confirmation': {
        LANG_EN: 'Invalid repayment confirmation.',
        LANG_RU: 'Некорректное подтверждение погашения.',
    },
    'repayment_already_recorded': {
        LANG_EN: 'Repayment was already recorded.',
        LANG_RU: 'Погашение уже было записано.',
    },
    'repayment_recorded': {LANG_EN: 'Repayment recorded.', LANG_RU: 'Погашение записано.'},
    'debt_increase_already_recorded': {
        LANG_EN: 'Debt increase was already recorded.',
        LANG_RU: 'Увеличение долга уже было записано.',
    },
    'debt_increase_recorded': {
        LANG_EN: 'Debt increase recorded.',
        LANG_RU: 'Увеличение долга записано.',
    },
    'new_obligation': {LANG_EN: 'New obligation', LANG_RU: 'Новое обязательство'},
    'new_obligation_cancelled': {
        LANG_EN: 'New obligation was cancelled.',
        LANG_RU: 'Создание обязательства отменено.',
    },
    'new_obligation_incomplete': {
        LANG_EN: 'New obligation is incomplete.',
        LANG_RU: 'Новое обязательство заполнено не полностью.',
    },
    'counterparty_unavailable': {
        LANG_EN: 'Counterparty is not available anymore.',
        LANG_RU: 'Вторая сторона больше недоступна.',
    },
    'who_are_you': {
        LANG_EN: 'Choose your role in this obligation.',
        LANG_RU: 'Выберите вашу роль в этом обязательстве.',
    },
    'i_lent_money': {LANG_EN: 'Creditor', LANG_RU: 'Кредитор'},
    'i_borrowed_money': {LANG_EN: 'Borrower', LANG_RU: 'Заёмщик'},
    'who_borrowed_from_you': {
        LANG_EN: 'Who borrowed from you?',
        LANG_RU: 'Кто занял у вас?',
    },
    'who_lent_to_you': {
        LANG_EN: 'Who lent money to you?',
        LANG_RU: 'Кто одолжил вам деньги?',
    },
    'choose_counterparty': {
        LANG_EN: 'Choose an active counterparty from the list.',
        LANG_RU: 'Выберите активную вторую сторону из списка.',
    },
    'send_title': {LANG_EN: 'Send title.', LANG_RU: 'Отправьте название.'},
    'title_error': {
        LANG_EN: 'Send a title up to 160 characters.',
        LANG_RU: 'Отправьте название до 160 символов.',
    },
    'send_initial_amount': {
        LANG_EN: 'Send initial amount, for example: 625.00',
        LANG_RU: 'Отправьте начальную сумму, например: 625.00',
    },
    'choose_opened_date': {LANG_EN: 'Choose opened date.', LANG_RU: 'Выберите дату открытия.'},
    'today': {LANG_EN: 'Today', LANG_RU: 'Сегодня'},
    'custom_date': {LANG_EN: 'Custom date', LANG_RU: 'Своя дата'},
    'send_opened_date': {
        LANG_EN: 'Send opened date as MM/DD/YYYY.',
        LANG_RU: 'Отправьте дату открытия в формате MM/DD/YYYY.',
    },
    'opened_date_error': {
        LANG_EN: 'Opened date must use MM/DD/YYYY. Example: {date}',
        LANG_RU: 'Дата открытия должна быть в формате MM/DD/YYYY. Пример: {date}',
    },
    'choose_date_option': {
        LANG_EN: 'Choose a date option.',
        LANG_RU: 'Выберите вариант даты.',
    },
    'title': {LANG_EN: 'Title', LANG_RU: 'Название'},
    'opened': {LANG_EN: 'Opened', LANG_RU: 'Открыто'},
    'schedule': {LANG_EN: 'Schedule', LANG_RU: 'График'},
    'choose_payment_schedule': {
        LANG_EN: 'Choose payment schedule.',
        LANG_RU: 'Выберите график платежей.',
    },
    'one_time_payment': {LANG_EN: 'One-time payment', LANG_RU: 'Одноразовый платёж'},
    'monthly_recurring': {LANG_EN: 'Monthly recurring', LANG_RU: 'Ежемесячно'},
    'weekly_recurring': {LANG_EN: 'Weekly recurring', LANG_RU: 'Еженедельно'},
    'biweekly_recurring': {LANG_EN: 'Every 2 weeks', LANG_RU: 'Каждые 2 недели'},
    'monthly_day_prompt': {
        LANG_EN: 'Send day of month from 1 to 31, or use the opened-date day.',
        LANG_RU: 'Отправьте день месяца от 1 до 31 или используйте день даты открытия.',
    },
    'use_day': {LANG_EN: 'Use day {day}', LANG_RU: 'Использовать день {day}'},
    'day_number_error': {
        LANG_EN: 'Day of month must be a number from 1 to 31.',
        LANG_RU: 'День месяца должен быть числом от 1 до 31.',
    },
    'day_range_error': {
        LANG_EN: 'Day of month must be from 1 to 31.',
        LANG_RU: 'День месяца должен быть от 1 до 31.',
    },
    'add_interest': {LANG_EN: 'Add interest?', LANG_RU: 'Добавить проценты?'},
    'no_interest': {LANG_EN: 'No interest', LANG_RU: 'Без процентов'},
    'with_interest': {LANG_EN: 'With interest', LANG_RU: 'С процентами'},
    'send_interest_rate': {
        LANG_EN: 'Send annual interest rate (APY), for example: 3.5 or 3,5',
        LANG_RU: 'Отправьте годовую процентную ставку (APY), например: 3.5 или 3,5',
    },
    'interest_option_error': {
        LANG_EN: 'Choose an interest option.',
        LANG_RU: 'Выберите вариант процентов.',
    },
    'interest_number_error': {
        LANG_EN: 'Interest rate must be a number, for example: 3.5',
        LANG_RU: 'Процентная ставка должна быть числом, например: 3.5',
    },
    'interest_positive_error': {
        LANG_EN: 'Interest rate must be greater than zero.',
        LANG_RU: 'Процентная ставка должна быть больше нуля.',
    },
    'create_obligation_question': {
        LANG_EN: 'Create obligation?',
        LANG_RU: 'Создать обязательство?',
    },
    'create_obligation': {LANG_EN: 'Create obligation', LANG_RU: 'Создать обязательство'},
    'obligation_created': {LANG_EN: 'Obligation created.', LANG_RU: 'Обязательство создано.'},
    'interest': {LANG_EN: 'Interest', LANG_RU: 'Проценты'},
    'yes_no_no': {LANG_EN: 'No', LANG_RU: 'Нет'},
    'unknown': {LANG_EN: 'Unknown', LANG_RU: 'Неизвестно'},
    'unsupported_role': {LANG_EN: 'Unsupported role.', LANG_RU: 'Неподдерживаемая роль.'},
    'choose_schedule_option': {
        LANG_EN: 'Choose a schedule option.',
        LANG_RU: 'Выберите вариант графика.',
    },
    'pay': {LANG_EN: 'Pay {amount}', LANG_RU: 'Оплатить {amount}'},
    'pay_full_balance': {
        LANG_EN: 'Pay full balance {amount}',
        LANG_RU: 'Оплатить весь баланс {amount}',
    },
    'custom_amount': {LANG_EN: 'Custom amount', LANG_RU: 'Своя сумма'},
    'back': {LANG_EN: 'Back', LANG_RU: 'Назад'},
    'add_manual_transfer': {LANG_EN: 'Manual transfer', LANG_RU: 'Ручная операция'},
    'back_to_obligations': {
        LANG_EN: 'Back to obligations',
        LANG_RU: 'Назад к обязательствам',
    },
    'you_owe': {LANG_EN: 'you owe', LANG_RU: 'вы должны'},
    'owed_to_you': {LANG_EN: 'owed to you', LANG_RU: 'должны вам'},
    'role_lent': {LANG_EN: 'Creditor', LANG_RU: 'Кредитор'},
    'role_borrowed': {LANG_EN: 'Borrower', LANG_RU: 'Заёмщик'},
    'schedule_one_time': {LANG_EN: 'One-time payment', LANG_RU: 'Одноразовый платёж'},
    'schedule_monthly': {LANG_EN: 'Monthly, day {day}', LANG_RU: 'Ежемесячно, день {day}'},
    'schedule_weekly': {LANG_EN: 'Weekly on {day}', LANG_RU: 'Еженедельно, {day}'},
    'schedule_biweekly': {LANG_EN: 'Every 2 weeks on {day}', LANG_RU: 'Каждые 2 недели, {day}'},
    'schedule_recurring': {LANG_EN: 'Recurring', LANG_RU: 'Повторяющийся'},
    'event_principal_advance': {LANG_EN: 'Principal advance', LANG_RU: 'Пополнение долга'},
    'event_repayment': {LANG_EN: 'Repayment', LANG_RU: 'Погашение'},
    'event_scheduled_charge': {LANG_EN: 'Scheduled charge', LANG_RU: 'Плановое начисление'},
    'event_interest_posting': {LANG_EN: 'Interest posting', LANG_RU: 'Начисление процентов'},
    'event_adjustment': {LANG_EN: 'Adjustment', LANG_RU: 'Корректировка'},
}

DAY_NAME_TEXT = {
    LANG_EN: DAY_NAMES,
    LANG_RU: ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'),
}


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
    profile = _get_profile_for_telegram_id(telegram_user_id)
    lang = _profile_language(profile) if profile else LANG_EN
    access_message = _access_error_message(chat, telegram_user_id, lang)
    if access_message:
        return _single_message(chat_id, access_message)

    if not profile:
        return _single_message(
            chat_id,
            f'{_t(LANG_EN, "access_not_configured")}\n'
            f'Telegram ID: {telegram_user_id}\n\n'
            f'{_t(LANG_EN, "access_add_profile")}',
        )
    _cache_user_language(profile.user, lang)

    if not text:
        return _single_message(chat_id, _help_text(profile.user, lang), reply_markup=_main_menu_markup(lang=lang))

    if not text.startswith('/') and telegram_user_id in PENDING_OBLIGATION_CREATIONS:
        return _pending_obligation_creation_text(
            profile.user,
            telegram_user_id,
            chat_id,
            text,
            today,
            lang,
        )

    if not text.startswith('/') and telegram_user_id in PENDING_MANUAL_TRANSFERS:
        return _pending_repayment_preview(
            profile.user,
            telegram_user_id,
            chat_id,
            text,
            today,
            nonce_factory,
            lang,
        )

    command, *raw_args = text.split(maxsplit=1)
    command = command.split('@', maxsplit=1)[0].lower()
    args_text = raw_args[0] if raw_args else ''

    if command == '/start' and args_text.startswith('login_'):
        result = confirm_challenge_by_start_payload(args_text, telegram_user_id)
        return _single_message(chat_id, _telegram_login_confirmation_text(result, lang))
    if command in ('/login', '/signin'):
        result = confirm_challenge_by_code(args_text, telegram_user_id)
        return _single_message(chat_id, _telegram_login_confirmation_text(result, lang))

    if command in ('/start', '/help'):
        _clear_pending_context(telegram_user_id)
        return _single_message(chat_id, _start_text(profile.user, today, lang), reply_markup=_main_menu_markup(lang=lang))
    if command == '/balance':
        return _single_message(chat_id, _balance_text(profile.user, lang), reply_markup=_main_menu_markup(current='balance', lang=lang))
    if command in ('/settings', '/setting'):
        _clear_pending_context(telegram_user_id)
        return _single_message(
            chat_id,
            _settings_text(profile.user, lang),
            reply_markup=_settings_markup(profile, lang),
        )
    if command in ('/new', '/newobligation'):
        return _single_message(chat_id, _new_obligation_role_text(lang), reply_markup=_new_obligation_role_markup(lang))
    if command in ('/debt', '/obligation'):
        obligation = _find_obligation_from_code(profile.user, args_text.strip())
        if not obligation:
            return _single_message(
                chat_id,
                _t(lang, 'choose_obligation_below'),
                reply_markup=_obligations_menu_markup(profile.user, lang),
            )
        return _single_message(
            chat_id,
            _obligation_detail_text(profile.user, obligation, lang),
            reply_markup=_obligation_detail_markup(obligation, lang),
        )
    if command in ('/repay', '/payment'):
        return _repayment_preview(profile.user, chat_id, args_text, today, nonce_factory, lang)

    return _single_message(chat_id, _unknown_command_text(profile.user, lang), reply_markup=_main_menu_markup(lang=lang))


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

    profile = _get_profile_for_telegram_id(telegram_user_id)
    lang = _profile_language(profile) if profile else LANG_EN
    access_message = _access_error_message(chat, telegram_user_id, lang)
    if access_message:
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, access_message)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'access_denied'),
        )

    if not profile:
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _t(LANG_EN, 'access_not_configured'))],
            callback_query_id=callback_query_id,
            callback_text=_t(LANG_EN, 'access_denied'),
        )
    _cache_user_language(profile.user, lang)

    if data == 'noop:cancel':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _t(lang, 'cancelled'), _main_menu_markup(current=None, lang=lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'cancelled'),
        )
    if data == 'menu:home':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _start_text(profile.user, today, lang), _main_menu_markup(lang=lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'home'),
        )
    if data == 'menu:balance':
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _balance_text(profile.user, lang), _main_menu_markup(current='balance', lang=lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'balance'),
        )
    if data == 'menu:obligations':
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _obligations_menu_text(profile.user, today, lang),
                    _obligations_menu_markup(profile.user, lang),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'open_obligations'),
        )
    if data == 'menu:recent':
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _recent_transactions_text(profile.user, lang),
                    _main_menu_markup(current='recent', lang=lang),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'recent_transactions'),
        )
    if data == 'menu:settings':
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _settings_text(profile.user, lang), _settings_markup(profile, lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'settings_title'),
        )
    if data.startswith('settings:lang:'):
        requested_language = data.split(':', maxsplit=2)[2]
        if requested_language not in SUPPORTED_TELEGRAM_LANGUAGES:
            requested_language = lang
        if profile.telegram_language != requested_language:
            profile.telegram_language = requested_language
            profile.save(update_fields=['telegram_language', 'updated_at'])
        lang = requested_language
        _cache_user_language(profile.user, lang)
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _settings_text(profile.user, lang, updated=True),
                    _settings_markup(profile, lang),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'settings_language_updated'),
        )
    if data.startswith('settings:due:'):
        requested_state = data.split(':', maxsplit=2)[2]
        enabled = requested_state == 'on'
        if profile.payment_due_notifications != enabled:
            profile.payment_due_notifications = enabled
            profile.save(update_fields=['payment_due_notifications', 'updated_at'])
        return TelegramBotResult(
            messages=[
                _panel_message(
                    chat_id,
                    message_id,
                    _settings_text(profile.user, lang, notification_updated=True),
                    _settings_markup(profile, lang),
                )
            ],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'settings_notifications_updated'),
        )
    if data == 'menu:new_obligation':
        _clear_pending_context(telegram_user_id)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, _new_obligation_role_text(lang), _new_obligation_role_markup(lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'new_obligation'),
        )
    if data.startswith('ob:'):
        text, reply_markup = _obligation_callback_response(profile.user, data, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'obligation'),
        )
    if data.startswith('transfermenu:'):
        text, reply_markup = _manual_transfer_menu_callback_response(profile.user, data, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'manual_transfer'),
        )
    if data.startswith('transfer:repayment:'):
        text, reply_markup = _manual_transfer_repayment_callback_response(profile.user, data, today, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'event_repayment'),
        )
    if data.startswith('transfer:advance:'):
        text, reply_markup = _custom_debt_increase_callback_response(
            profile.user,
            telegram_user_id,
            data,
            chat_id,
            message_id,
            lang,
        )
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'transfer_debt_increase'),
        )
    if data.startswith('repaymenu:'):
        text, reply_markup = _repayment_menu_callback_response(profile.user, data, today, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'event_repayment'),
        )
    if data.startswith('customrepay:'):
        text, reply_markup = _custom_repayment_callback_response(
            profile.user,
            telegram_user_id,
            data,
            chat_id,
            message_id,
            lang,
        )
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'custom_amount'),
        )
    if data.startswith('newob:'):
        text, reply_markup = _new_obligation_callback_response(
            profile.user,
            telegram_user_id,
            data,
            chat_id,
            message_id,
            today,
            lang,
        )
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'new_obligation'),
        )
    if data.startswith('repayamt:'):
        text, reply_markup = _repayment_amount_callback_response(profile.user, data, today, nonce_factory, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, reply_markup)],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'confirm_repayment'),
        )
    if data.startswith('repay:'):
        text = _confirm_repayment(profile.user, data, today, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, _main_menu_markup(current=None, lang=lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'processed'),
        )
    if data.startswith('advance:'):
        text = _confirm_debt_increase(profile.user, data, today, lang)
        return TelegramBotResult(
            messages=[_panel_message(chat_id, message_id, text, _main_menu_markup(current=None, lang=lang))],
            callback_query_id=callback_query_id,
            callback_text=_t(lang, 'processed'),
        )

    return TelegramBotResult(
        messages=[_panel_message(chat_id, message_id, _t(lang, 'unsupported_button'), _main_menu_markup(current=None, lang=lang))],
        callback_query_id=callback_query_id,
        callback_text=_t(lang, 'unsupported_action'),
    )


def _access_error_message(chat, telegram_user_id, lang=LANG_EN):
    if (chat.get('type') or '') != 'private':
        return _t(lang, 'access_private_chat')
    if not telegram_user_id:
        return _t(lang, 'access_missing_user_id')
    return ''


def _get_profile_for_telegram_id(telegram_user_id):
    return UserProfile.objects.select_related('user').filter(telegram_id=telegram_user_id).first()


def _profile_language(profile):
    language = profile.telegram_language or LANG_EN
    if language not in SUPPORTED_TELEGRAM_LANGUAGES:
        return LANG_EN
    return language


def _cache_user_language(user, lang):
    setattr(user, '_trusttrack_telegram_language', lang)


def _language_for_user(user):
    cached_language = getattr(user, '_trusttrack_telegram_language', None)
    if cached_language in SUPPORTED_TELEGRAM_LANGUAGES:
        return cached_language
    try:
        profile = user.trusttrack_profile
    except UserProfile.DoesNotExist:
        return LANG_EN
    return _profile_language(profile)


def _t(lang, key, **kwargs):
    language = lang if lang in SUPPORTED_TELEGRAM_LANGUAGES else LANG_EN
    template = TELEGRAM_TEXT.get(key, {}).get(language) or TELEGRAM_TEXT.get(key, {}).get(LANG_EN) or key
    return template.format(**kwargs)


def _language_label(lang, viewer_lang):
    if lang == LANG_RU:
        return _t(viewer_lang, 'language_russian')
    return _t(viewer_lang, 'language_english')


def _start_text(user, today, lang=None):
    lang = lang or _language_for_user(user)
    obligations = list(_open_obligations_for_user(user))
    i_owe_units, owed_to_me_units, net_units = _portfolio_totals(user, obligations)
    return '\n'.join([
        'TrustTrack',
        f'{_t(lang, "user")}: {_user_label(user)}',
        '',
        f'{_t(lang, "i_owe")}: {_format_money(i_owe_units)}',
        f'{_t(lang, "owed_to_me")}: {_format_money(owed_to_me_units)}',
        f'{_t(lang, "net")}: {_format_signed_money(net_units)}',
        '',
        _t(lang, 'choose_action'),
    ])


def _help_text(user, lang=None):
    lang = lang or _language_for_user(user)
    return '\n'.join([
        _t(lang, 'help_access', user=_user_label(user)),
        '',
        _t(lang, 'help_buttons'),
    ])


def _settings_text(user, lang=None, updated=False, notification_updated=False):
    lang = lang or _language_for_user(user)
    profile = UserProfile.objects.filter(user=user).first()
    notifications_enabled = True if profile is None else profile.payment_due_notifications
    notification_status = _t(lang, 'settings_on' if notifications_enabled else 'settings_off')
    lines = []
    if updated:
        lines.extend([_t(lang, 'settings_language_updated'), ''])
    if notification_updated:
        lines.extend([_t(lang, 'settings_notifications_updated'), ''])
    lines.extend([
        _t(lang, 'settings_title'),
        f'{_t(lang, "settings_current_language", language=_language_label(lang, lang))}',
        _t(lang, 'settings_notifications_current', status=notification_status),
        '',
        _t(lang, 'settings_choose_language'),
        _t(lang, 'settings_notifications_hint'),
    ])
    return '\n'.join(lines)


def _settings_markup(profile, lang):
    notifications_enabled = profile.payment_due_notifications
    next_notification_state = 'off' if notifications_enabled else 'on'
    notification_button = (
        'settings_notifications_disable'
        if notifications_enabled
        else 'settings_notifications_enable'
    )
    return {
        'inline_keyboard': [
            [
                _telegram_button(_t(lang, 'language_english'), f'settings:lang:{LANG_EN}'),
                _telegram_button(_t(lang, 'language_russian'), f'settings:lang:{LANG_RU}'),
            ],
            [
                _telegram_button(
                    _t(lang, notification_button),
                    f'settings:due:{next_notification_state}',
                    BUTTON_STYLE_DANGER if notifications_enabled else BUTTON_STYLE_SUCCESS,
                )
            ],
            _navigation_row(lang, current='settings'),
        ],
    }


def _balance_text(user, lang=None):
    lang = lang or _language_for_user(user)
    obligations = list(_open_obligations_for_user(user))
    i_owe_units, owed_to_me_units, net_units = _portfolio_totals(user, obligations)
    lines = [
        _t(lang, 'balance_title'),
        f'{_t(lang, "i_owe")}: {_format_money(i_owe_units)}',
        f'{_t(lang, "owed_to_me")}: {_format_money(owed_to_me_units)}',
        f'{_t(lang, "net")}: {_format_signed_money(net_units)}',
    ]
    if obligations:
        lines.extend(['', f'{_t(lang, "open_obligations")}:'])
        lines.extend(_obligation_summary_line(user, obligation, lang=lang) for obligation in obligations)
    return '\n'.join(lines)


def _recent_transactions_text(user, lang=None):
    lang = lang or _language_for_user(user)
    obligations = list(_related_obligations_for_user(user))
    if not obligations:
        return _t(lang, 'no_transactions')

    transactions = (
        LedgerTransaction.objects
        .filter(obligation__in=obligations, status=LedgerTransaction.Status.POSTED)
        .select_related('obligation', 'financial_event')
        .order_by('-transaction_date', '-created_at')[:10]
    )
    if not transactions:
        return _t(lang, 'no_transactions')

    lines = [_t(lang, 'recent_transactions_title'), '']
    for transaction_item in transactions:
        event = transaction_item.financial_event
        lines.append(
            ' - '.join([
                _format_date(transaction_item.transaction_date),
                transaction_item.obligation.title,
                _event_type_label(event, lang),
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


def _obligation_text(user, args_text, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _find_obligation_from_code(user, args_text.strip())
    if not obligation:
        return _t(lang, 'choose_obligation_start')

    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        obligation.title,
        f'{_t(lang, "role")}: {_t(lang, role)}',
        f'{_t(lang, "counterparty")}: {_user_label(counterparty)}',
        f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
        '',
        _t(lang, 'use_repayment_button'),
    ])


def _repayment_preview(user, chat_id, args_text, today, nonce_factory, lang=None):
    lang = lang or _language_for_user(user)
    parsed = _parse_repayment_args(args_text, today, lang)
    if 'error' in parsed:
        return _single_message(chat_id, parsed['error'])

    obligation = _find_obligation_from_code(user, parsed['code'])
    if not obligation:
        return _single_message(chat_id, _t(lang, 'unknown_obligation'))

    return _repayment_preview_for_obligation(
        chat_id=chat_id,
        obligation=obligation,
        amount_units=parsed['amount_units'],
        event_date=parsed['event_date'],
        today=today,
        nonce_factory=nonce_factory,
        lang=lang,
    )


def _pending_repayment_preview(user, telegram_user_id, chat_id, amount_text, today, nonce_factory, lang=None):
    lang = lang or _language_for_user(user)
    pending_context = PENDING_MANUAL_TRANSFERS.get(telegram_user_id)
    if isinstance(pending_context, dict):
        obligation_id = pending_context.get('obligation_id')
        transfer_type = pending_context.get('transfer_type') or FinancialEvent.EventType.REPAYMENT
        panel_chat_id = pending_context.get('chat_id') or chat_id
        panel_message_id = pending_context.get('message_id')
    else:
        obligation_id = pending_context
        transfer_type = FinancialEvent.EventType.REPAYMENT
        panel_chat_id = chat_id
        panel_message_id = None

    obligation = _get_related_open_obligation(user, obligation_id)
    if not obligation:
        PENDING_MANUAL_TRANSFERS.pop(telegram_user_id, None)
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            _t(lang, 'unavailable_obligation'),
            reply_markup=_main_menu_markup(lang=lang),
        )

    amount_units, error = _parse_amount_units(amount_text, lang)
    if error:
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            f'{error}\n{_t(lang, "amount_example")}',
            reply_markup=_manual_transfer_error_markup(obligation, transfer_type, today, lang),
        )

    PENDING_MANUAL_TRANSFERS.pop(telegram_user_id, None)
    return _manual_transfer_preview_for_obligation(
        chat_id=panel_chat_id,
        obligation=obligation,
        transfer_type=transfer_type,
        amount_units=amount_units,
        event_date=today,
        today=today,
        nonce_factory=nonce_factory,
        message_id=panel_message_id,
        lang=lang,
    )


def _repayment_preview_for_obligation(chat_id, obligation, amount_units, event_date, today, nonce_factory, message_id=None, lang=LANG_EN):
    return _manual_transfer_preview_for_obligation(
        chat_id=chat_id,
        obligation=obligation,
        transfer_type=FinancialEvent.EventType.REPAYMENT,
        amount_units=amount_units,
        event_date=event_date,
        today=today,
        nonce_factory=nonce_factory,
        message_id=message_id,
        lang=lang,
    )


def _manual_transfer_preview_for_obligation(
    chat_id,
    obligation,
    transfer_type,
    amount_units,
    event_date,
    today,
    nonce_factory,
    message_id=None,
    lang=LANG_EN,
):
    balance_units = get_obligation_balance(obligation, as_of=event_date)
    is_repayment = transfer_type == FinancialEvent.EventType.REPAYMENT
    if is_repayment and amount_units > balance_units:
        return _panel_result(
            chat_id,
            message_id,
            _t(lang, 'repayment_exceeds_balance', date=_format_date(event_date), balance=_format_money(balance_units)),
            reply_markup=_obligation_detail_markup(obligation, lang),
        )

    nonce = nonce_factory()
    callback_prefix = 'repay' if is_repayment else 'advance'
    confirm_key = 'confirm_repayment' if is_repayment else 'confirm_debt_increase'
    action_key = 'transfer_repayment' if is_repayment else 'transfer_debt_increase'
    callback_data = f'{callback_prefix}:{obligation.pk}:{amount_units}:{event_date.isoformat()}:{nonce}'
    text = '\n'.join([
        _t(lang, confirm_key),
        f'{_t(lang, "obligation")}: {obligation.title}',
        f'{_t(lang, "action")}: {_t(lang, action_key)}',
        f'{_t(lang, "amount")}: {_format_money(amount_units)}',
        f'{_t(lang, "date")}: {_format_date(event_date)}',
    ])
    return _panel_result(
        chat_id,
        message_id,
        text,
        reply_markup={
            'inline_keyboard': [
                [_telegram_button(
                    _t(lang, confirm_key),
                    callback_data,
                    BUTTON_STYLE_SUCCESS if is_repayment else BUTTON_STYLE_DANGER,
                )],
                [_telegram_button(_t(lang, 'cancel'), 'noop:cancel', BUTTON_STYLE_DANGER)],
            ],
        },
    )


def _obligations_menu_text(user, today, lang=None):
    lang = lang or _language_for_user(user)
    obligations = list(_open_obligations_for_user(user))
    if not obligations:
        return _t(lang, 'no_open_obligations')
    lines = [f'{_t(lang, "open_obligations")}:', '']
    lines.extend(_obligation_summary_line(user, obligation, today=today, lang=lang) for obligation in obligations)
    return '\n'.join(lines)


def _obligation_callback_response(user, data, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_callback(user, data, 'ob')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)
    return _obligation_detail_text(user, obligation, lang), _obligation_detail_markup(obligation, lang)


def _manual_transfer_menu_callback_response(user, data, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_callback(user, data, 'transfermenu')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)

    balance_units = get_obligation_balance(obligation)
    return (
        '\n'.join([
            _t(lang, 'manual_transfer'),
            obligation.title,
            f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
            '',
            _t(lang, 'choose_transfer_action'),
        ]),
        _manual_transfer_type_markup(obligation, lang),
    )


def _manual_transfer_repayment_callback_response(user, data, today, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_transfer_action_callback(user, data, 'repayment')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)
    return _repayment_menu_response(obligation, today, lang)


def _repayment_menu_callback_response(user, data, today, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_callback(user, data, 'repaymenu')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)

    return _repayment_menu_response(obligation, today, lang)


def _repayment_menu_response(obligation, today, lang):
    balance_units = get_obligation_balance(obligation, as_of=today)
    if balance_units <= 0:
        return (
            _t(lang, 'no_balance_to_repay', title=obligation.title),
            _obligation_detail_markup(obligation, lang),
        )

    return (
        '\n'.join([
            _t(lang, 'record_repayment', title=obligation.title),
            f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
            '',
            _t(lang, 'choose_amount'),
        ]),
        _repayment_amount_markup(obligation, balance_units, lang),
    )


def _custom_debt_increase_callback_response(user, telegram_user_id, data, chat_id, message_id, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_transfer_action_callback(user, data, 'advance')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)

    return _custom_manual_transfer_callback_response(
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        message_id=message_id,
        obligation=obligation,
        transfer_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE,
        prompt_key='custom_debt_increase',
        lang=lang,
    )


def _custom_repayment_callback_response(user, telegram_user_id, data, chat_id, message_id, lang=None):
    lang = lang or _language_for_user(user)
    obligation = _get_obligation_from_callback(user, data, 'customrepay')
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)

    return _custom_manual_transfer_callback_response(
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        message_id=message_id,
        obligation=obligation,
        transfer_type=FinancialEvent.EventType.REPAYMENT,
        prompt_key='custom_repayment',
        lang=lang,
    )


def _custom_manual_transfer_callback_response(
    telegram_user_id,
    chat_id,
    message_id,
    obligation,
    transfer_type,
    prompt_key,
    lang,
):
    PENDING_MANUAL_TRANSFERS[telegram_user_id] = {
        'obligation_id': obligation.pk,
        'transfer_type': transfer_type,
        'chat_id': chat_id,
        'message_id': message_id,
    }
    return (
        '\n'.join([
            _t(lang, prompt_key, title=obligation.title),
            _t(lang, 'amount_example'),
        ]),
        _obligation_detail_markup(obligation, lang),
    )


def _new_obligation_callback_response(user, telegram_user_id, data, chat_id, message_id, today, lang=None):
    lang = lang or _language_for_user(user)
    parts = data.split(':')
    if len(parts) < 2:
        return _t(lang, 'unsupported_action'), _main_menu_markup(current=None, lang=lang)

    action = parts[1]
    if action == 'role' and len(parts) == 3:
        role = parts[2]
        if role not in (ROLE_LENT, ROLE_BORROWED):
            return _t(lang, 'unsupported_role'), _new_obligation_role_markup(lang)
        PENDING_OBLIGATION_CREATIONS[telegram_user_id] = {
            'chat_id': chat_id,
            'message_id': message_id,
            'role': role,
            'step': 'counterparty',
        }
        return _new_obligation_counterparty_text(role, lang), _new_obligation_counterparty_markup(user, lang)

    context = PENDING_OBLIGATION_CREATIONS.get(telegram_user_id)
    if not context:
        return _new_obligation_role_text(lang), _new_obligation_role_markup(lang)
    context['chat_id'] = chat_id
    context['message_id'] = message_id

    if action == 'cancel':
        PENDING_OBLIGATION_CREATIONS.pop(telegram_user_id, None)
        return _t(lang, 'new_obligation_cancelled'), _main_menu_markup(current=None, lang=lang)

    if action == 'cp' and len(parts) == 3:
        counterparty = _get_counterparty(user, parts[2])
        if not counterparty:
            return _t(lang, 'choose_counterparty'), _new_obligation_counterparty_markup(user, lang)
        context['counterparty_id'] = counterparty.pk
        context['step'] = 'title'
        return _new_obligation_title_text(context, counterparty, lang), _new_obligation_cancel_markup(lang)

    if action == 'date' and len(parts) == 3:
        if parts[2] == 'today':
            context['opened_on'] = today
            context['step'] = 'schedule'
            return _new_obligation_schedule_text(context, lang), _new_obligation_schedule_markup(lang)
        if parts[2] == 'custom':
            context['step'] = 'opened_on'
            return _t(lang, 'send_opened_date'), _new_obligation_cancel_markup(lang)
        return _t(lang, 'choose_date_option'), _new_obligation_date_markup(lang)

    if action == 'schedule' and len(parts) == 3:
        return _new_obligation_schedule_callback(context, parts[2], lang)

    if action == 'dom' and len(parts) == 3:
        day_of_month, error = _parse_day_of_month(parts[2], lang)
        if error:
            return error, _new_obligation_month_day_markup(context, lang)
        context['recurring_day_of_month'] = day_of_month
        context['step'] = 'interest'
        return _new_obligation_interest_text(context, lang), _new_obligation_interest_markup(lang)

    if action == 'interest' and len(parts) == 3:
        if parts[2] == 'no':
            context['has_interest'] = False
            context['annual_rate_percent'] = None
            context['step'] = 'confirm'
            return _new_obligation_confirm_text(user, context, lang), _new_obligation_confirm_markup(lang)
        if parts[2] == 'yes':
            context['has_interest'] = True
            context['step'] = 'interest_rate'
            return _t(lang, 'send_interest_rate'), _new_obligation_cancel_markup(lang)
        return _t(lang, 'interest_option_error'), _new_obligation_interest_markup(lang)

    if action == 'create':
        try:
            obligation = _create_obligation_from_context(user, context, lang)
        except (ValidationError, ValueError) as error:
            return _validation_error_text(error, lang), _new_obligation_confirm_markup(lang)
        PENDING_OBLIGATION_CREATIONS.pop(telegram_user_id, None)
        return _new_obligation_created_text(obligation, lang), _main_menu_markup(current=None, lang=lang)

    return _t(lang, 'unsupported_action'), _main_menu_markup(current=None, lang=lang)


def _pending_obligation_creation_text(user, telegram_user_id, chat_id, text, today, lang=None):
    lang = lang or _language_for_user(user)
    context = PENDING_OBLIGATION_CREATIONS.get(telegram_user_id)
    if not context:
        return _single_message(chat_id, _unknown_command_text(user, lang), reply_markup=_main_menu_markup(lang=lang))

    panel_chat_id = context.get('chat_id') or chat_id
    panel_message_id = context.get('message_id')
    step = context.get('step')

    if step == 'counterparty':
        return _panel_result(
            panel_chat_id,
            panel_message_id,
            _new_obligation_counterparty_text(context.get('role'), lang),
            _new_obligation_counterparty_markup(user, lang),
        )

    if step == 'title':
        title = text.strip()
        if not title or len(title) > 160:
            return _panel_result(panel_chat_id, panel_message_id, _t(lang, 'title_error'), _new_obligation_cancel_markup(lang))
        context['title'] = title
        context['step'] = 'amount'
        return _panel_result(panel_chat_id, panel_message_id, _t(lang, 'send_initial_amount'), _new_obligation_cancel_markup(lang))

    if step == 'amount':
        amount_units, error = _parse_amount_units(text, lang)
        if error:
            return _panel_result(panel_chat_id, panel_message_id, error, _new_obligation_cancel_markup(lang))
        context['amount_units'] = amount_units
        context['step'] = 'opened_on_choice'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_date_text(lang), _new_obligation_date_markup(lang))

    if step == 'opened_on':
        opened_on, error = _parse_user_date(text)
        if error:
            return _panel_result(panel_chat_id, panel_message_id, _t(lang, 'opened_date_error', date=_format_date(today)), _new_obligation_cancel_markup(lang))
        context['opened_on'] = opened_on
        context['step'] = 'schedule'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_schedule_text(context, lang), _new_obligation_schedule_markup(lang))

    if step == 'opened_on_choice':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_date_text(lang), _new_obligation_date_markup(lang))

    if step == 'schedule':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_schedule_text(context, lang), _new_obligation_schedule_markup(lang))

    if step == 'day_of_month':
        day_of_month, error = _parse_day_of_month(text, lang)
        if error:
            return _panel_result(panel_chat_id, panel_message_id, error, _new_obligation_month_day_markup(context, lang))
        context['recurring_day_of_month'] = day_of_month
        context['step'] = 'interest'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_interest_text(context, lang), _new_obligation_interest_markup(lang))

    if step == 'interest_rate':
        try:
            annual_rate_percent = Decimal(_normalize_decimal_text(text))
        except (InvalidOperation, ValueError):
            return _panel_result(panel_chat_id, panel_message_id, _t(lang, 'interest_number_error'), _new_obligation_cancel_markup(lang))
        if annual_rate_percent <= 0:
            return _panel_result(panel_chat_id, panel_message_id, _t(lang, 'interest_positive_error'), _new_obligation_cancel_markup(lang))
        context['annual_rate_percent'] = annual_rate_percent
        context['step'] = 'confirm'
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_confirm_text(user, context, lang), _new_obligation_confirm_markup(lang))

    if step == 'interest':
        return _panel_result(panel_chat_id, panel_message_id, _new_obligation_interest_text(context, lang), _new_obligation_interest_markup(lang))

    return _panel_result(panel_chat_id, panel_message_id, _new_obligation_confirm_text(user, context, lang), _new_obligation_confirm_markup(lang))


def _new_obligation_schedule_callback(context, schedule, lang=LANG_EN):
    opened_on = context.get('opened_on')
    if not opened_on:
        return _new_obligation_date_text(lang), _new_obligation_date_markup(lang)

    if schedule == PAYMENT_MODE_ONE_TIME:
        context['payment_mode'] = PAYMENT_MODE_ONE_TIME
        context['step'] = 'interest'
        return _new_obligation_interest_text(context, lang), _new_obligation_interest_markup(lang)

    if schedule == EventSeries.Frequency.MONTHLY:
        context['payment_mode'] = PAYMENT_MODE_RECURRING
        context['recurring_frequency'] = EventSeries.Frequency.MONTHLY
        context['recurring_starts_on'] = opened_on
        context['step'] = 'day_of_month'
        return _new_obligation_month_day_text(opened_on, lang), _new_obligation_month_day_markup(context, lang)

    if schedule in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY):
        context['payment_mode'] = PAYMENT_MODE_RECURRING
        context['recurring_frequency'] = schedule
        context['recurring_day_of_week'] = opened_on.weekday()
        context['recurring_starts_on'] = opened_on
        context['step'] = 'interest'
        return _new_obligation_interest_text(context, lang), _new_obligation_interest_markup(lang)

    return _t(lang, 'choose_schedule_option'), _new_obligation_schedule_markup(lang)


def _create_obligation_from_context(user, context, lang=LANG_EN):
    required = ('role', 'counterparty_id', 'title', 'amount_units', 'opened_on', 'payment_mode')
    if any(context.get(field_name) in (None, '') for field_name in required):
        raise ValueError(_t(lang, 'new_obligation_incomplete'))

    counterparty = _get_counterparty(user, context['counterparty_id'])
    if not counterparty:
        raise ValueError(_t(lang, 'counterparty_unavailable'))

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
        recalculation_result = recalculate_obligation(obligation)
    send_obligation_created_notification(obligation, context['amount_units'], recalculation_result)
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


def _repayment_amount_callback_response(user, data, today, nonce_factory, lang=None):
    lang = lang or _language_for_user(user)
    parts = data.split(':')
    if len(parts) != 3:
        return _t(lang, 'invalid_repayment_amount'), _main_menu_markup(lang=lang)
    try:
        obligation_id = int(parts[1])
        amount_units = int(parts[2])
    except ValueError:
        return _t(lang, 'invalid_repayment_amount'), _main_menu_markup(lang=lang)

    obligation = _get_related_open_obligation(user, obligation_id)
    if not obligation:
        return _t(lang, 'unavailable_obligation'), _main_menu_markup(lang=lang)

    result = _repayment_preview_for_obligation(
        chat_id=0,
        obligation=obligation,
        amount_units=amount_units,
        event_date=today,
        today=today,
        nonce_factory=nonce_factory,
        lang=lang,
    )
    if not result.messages:
        return _t(lang, 'invalid_repayment_amount'), _obligation_detail_markup(obligation, lang)
    message = result.messages[0]
    return message.text, message.reply_markup


def _confirm_repayment(user, callback_data, today, lang=None):
    lang = lang or _language_for_user(user)
    parts = callback_data.split(':')
    if len(parts) != 5:
        return _t(lang, 'invalid_repayment_confirmation')

    _, obligation_id, amount_units_text, event_date_text, nonce = parts
    try:
        obligation = _get_related_open_obligation(user, int(obligation_id))
        amount_units = int(amount_units_text)
        event_date = date.fromisoformat(event_date_text)
    except (TypeError, ValueError):
        return _t(lang, 'invalid_repayment_confirmation')

    if not obligation or amount_units <= 0:
        return _t(lang, 'invalid_repayment_confirmation')
    if event_date > today:
        return _t(lang, 'repayment_future_date')

    idempotency_key = f'telegram-repayment:{user.pk}:{obligation.pk}:{nonce}'
    existing = LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return _repayment_recorded_text(obligation, amount_units, event_date, already_recorded=True, lang=lang)

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
        return _validation_error_text(error, lang)

    return _repayment_recorded_text(obligation, amount_units, event_date, lang=lang)


def _confirm_debt_increase(user, callback_data, today, lang=None):
    lang = lang or _language_for_user(user)
    parts = callback_data.split(':')
    if len(parts) != 5:
        return _t(lang, 'invalid_repayment_confirmation')

    _, obligation_id, amount_units_text, event_date_text, nonce = parts
    try:
        obligation = _get_related_open_obligation(user, int(obligation_id))
        amount_units = int(amount_units_text)
        event_date = date.fromisoformat(event_date_text)
    except (TypeError, ValueError):
        return _t(lang, 'invalid_repayment_confirmation')

    if not obligation or amount_units <= 0:
        return _t(lang, 'invalid_repayment_confirmation')
    if event_date > today:
        return _t(lang, 'repayment_future_date')

    idempotency_key = f'telegram-advance:{user.pk}:{obligation.pk}:{nonce}'
    existing = LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return _debt_increase_recorded_text(obligation, amount_units, event_date, already_recorded=True, lang=lang)

    try:
        post_principal_advance(
            obligation,
            amount_units=amount_units,
            event_date=event_date,
            memo=f'Telegram debt increase by {_user_label(user)}',
            category='telegram',
            idempotency_key=idempotency_key,
        )
    except ValidationError as error:
        return _validation_error_text(error, lang)

    return _debt_increase_recorded_text(obligation, amount_units, event_date, lang=lang)


def _repayment_recorded_text(obligation, amount_units, event_date, already_recorded=False, lang=LANG_EN):
    balance_units = get_obligation_balance(obligation)
    heading = _t(lang, 'repayment_already_recorded') if already_recorded else _t(lang, 'repayment_recorded')
    return '\n'.join([
        heading,
        f'{_t(lang, "obligation")}: {obligation.title}',
        f'{_t(lang, "amount")}: {_format_money(amount_units)}',
        f'{_t(lang, "date")}: {_format_date(event_date)}',
        f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
    ])


def _debt_increase_recorded_text(obligation, amount_units, event_date, already_recorded=False, lang=LANG_EN):
    balance_units = get_obligation_balance(obligation)
    heading = _t(lang, 'debt_increase_already_recorded') if already_recorded else _t(lang, 'debt_increase_recorded')
    return '\n'.join([
        heading,
        f'{_t(lang, "obligation")}: {obligation.title}',
        f'{_t(lang, "amount")}: {_format_money(amount_units)}',
        f'{_t(lang, "date")}: {_format_date(event_date)}',
        f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
    ])


def _parse_repayment_args(args_text, today, lang=LANG_EN):
    try:
        tokens = shlex.split(args_text)
    except ValueError:
        return {'error': _t(lang, 'repayment_parse_error')}

    if len(tokens) < 2:
        return {'error': _t(lang, 'repayment_choose_buttons')}

    code = tokens[0].upper()
    amount_units, error = _parse_amount_units(tokens[1], lang)
    if error:
        return {'error': error}

    event_date = today
    if len(tokens) >= 3:
        event_date, error = _parse_user_date(tokens[2])
        if error:
            return {'error': _t(lang, 'repayment_date_format')}

    if event_date > today:
        return {'error': _t(lang, 'repayment_future_date')}

    return {
        'code': code,
        'amount_units': amount_units,
        'event_date': event_date,
    }


def _unknown_command_text(user, lang=None):
    lang = lang or _language_for_user(user)
    return '\n'.join([
        _t(lang, 'unknown_command'),
        '',
        _t(lang, 'unknown_command_hint', user=_user_label(user)),
    ])


def _telegram_login_confirmation_text(result, lang=LANG_EN):
    if result.status == 'confirmed':
        return _t(lang, 'telegram_login_confirmed', user=_user_label(result.user))
    if result.status == 'expired':
        return _t(lang, 'telegram_login_expired')
    if result.status == 'consumed':
        return _t(lang, 'telegram_login_consumed')
    if result.status == 'access_denied':
        return _t(lang, 'telegram_login_access_denied')
    return _t(lang, 'telegram_login_not_found')


def _obligation_summary_line(user, obligation, today=None, lang=None):
    lang = lang or _language_for_user(user)
    balance_units = get_obligation_balance(obligation, as_of=today)
    role = _t(lang, 'you_owe') if obligation.borrower_id == user.id else _t(lang, 'owed_to_you')
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


def _get_obligation_from_transfer_action_callback(user, data, action):
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'transfer' or parts[1] != action:
        return None
    try:
        obligation_id = int(parts[2])
    except ValueError:
        return None
    return _get_related_open_obligation(user, obligation_id)


def _parse_amount_units(amount_text, lang=LANG_EN):
    amount_text = _normalize_decimal_text(amount_text)
    try:
        amount_units = units_from_decimal(Decimal(amount_text))
    except (InvalidOperation, ValueError):
        return None, _t(lang, 'amount_number_error')

    if amount_units <= 0:
        return None, _t(lang, 'amount_positive_error')
    return amount_units, ''


def _normalize_decimal_text(value):
    text = str(value).strip()
    if text.count(',') == 1 and '.' not in text:
        return text.replace(',', '.')
    return text


def _parse_user_date(value):
    text = str(value).strip()
    for date_format in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, date_format).date(), ''
        except ValueError:
            continue
    return None, 'Date must use MM/DD/YYYY.'


def _parse_day_of_month(value, lang=LANG_EN):
    try:
        day_of_month = int(str(value).strip())
    except (TypeError, ValueError):
        return None, _t(lang, 'day_number_error')
    if day_of_month < 1 or day_of_month > 31:
        return None, _t(lang, 'day_range_error')
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
    PENDING_MANUAL_TRANSFERS.pop(telegram_user_id, None)
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


def _obligation_detail_text(user, obligation, lang=None):
    lang = lang or _language_for_user(user)
    balance_units = get_obligation_balance(obligation)
    role = 'borrower' if obligation.borrower_id == user.id else 'creditor'
    counterparty = obligation.creditor if role == 'borrower' else obligation.borrower
    return '\n'.join([
        obligation.title,
        f'{_t(lang, "role")}: {_t(lang, role)}',
        f'{_t(lang, "counterparty")}: {_user_label(counterparty)}',
        f'{_t(lang, "current_balance")}: {_format_money(balance_units)}',
    ])


def _telegram_button(text, callback_data, style=None):
    button = {'text': text, 'callback_data': callback_data}
    if style:
        button['style'] = style
    return button


def _home_button(lang):
    return _telegram_button(_t(lang, 'home'), 'menu:home', BUTTON_STYLE_PRIMARY)


def _balance_button(lang):
    return _telegram_button(_t(lang, 'balance'), 'menu:balance', BUTTON_STYLE_PRIMARY)


def _navigation_row(lang, current=None):
    row = []
    if current != 'home':
        row.append(_home_button(lang))
    if current != 'balance':
        row.append(_balance_button(lang))
    return row


def _main_menu_markup(current='home', lang=LANG_EN):
    rows = []
    if current != 'obligations':
        rows.append([_telegram_button(_t(lang, 'open_obligations'), 'menu:obligations')])
    if current != 'recent':
        rows.append([_telegram_button(_t(lang, 'recent_transactions'), 'menu:recent')])
    nav_row = _navigation_row(lang, current=current)
    if nav_row:
        rows.append(nav_row)
    return {'inline_keyboard': rows}


def _obligations_menu_markup(user, lang=None):
    lang = lang or _language_for_user(user)
    rows = []
    for obligation in _open_obligations_for_user(user):
        balance_units = get_obligation_balance(obligation)
        rows.append([
            _telegram_button(f'{obligation.title} - {_format_money(balance_units)}', f'ob:{obligation.pk}')
        ])
    rows.append([_telegram_button(_t(lang, 'new_obligation'), 'menu:new_obligation', BUTTON_STYLE_SUCCESS)])
    rows.append(_navigation_row(lang, current='obligations'))
    return {'inline_keyboard': rows}


def _obligation_detail_markup(obligation, lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'add_manual_transfer'), f'transfermenu:{obligation.pk}', BUTTON_STYLE_SUCCESS)],
            [_telegram_button(_t(lang, 'back_to_obligations'), 'menu:obligations')],
            _navigation_row(lang, current='obligation'),
        ],
    }


def _manual_transfer_type_markup(obligation, lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'transfer_repayment'), f'transfer:repayment:{obligation.pk}', BUTTON_STYLE_SUCCESS)],
            [_telegram_button(_t(lang, 'transfer_debt_increase'), f'transfer:advance:{obligation.pk}', BUTTON_STYLE_DANGER)],
            [_telegram_button(_t(lang, 'back'), f'ob:{obligation.pk}', BUTTON_STYLE_PRIMARY)],
        ],
    }


def _new_obligation_role_text(lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'new_obligation'),
        '',
        _t(lang, 'who_are_you'),
    ])


def _new_obligation_role_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'i_lent_money'), f'newob:role:{ROLE_LENT}')],
            [_telegram_button(_t(lang, 'i_borrowed_money'), f'newob:role:{ROLE_BORROWED}')],
            [_home_button(lang)],
        ],
    }


def _new_obligation_counterparty_text(role, lang=LANG_EN):
    direction = _t(lang, 'who_borrowed_from_you') if role == ROLE_LENT else _t(lang, 'who_lent_to_you')
    return '\n'.join([
        _t(lang, 'new_obligation'),
        '',
        direction,
    ])


def _new_obligation_counterparty_markup(user, lang=LANG_EN):
    rows = []
    for counterparty in _available_counterparties(user):
        rows.append([_telegram_button(_user_label(counterparty), f'newob:cp:{counterparty.pk}')])
    rows.append([_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)])
    return {'inline_keyboard': rows}


def _new_obligation_title_text(context, counterparty, lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'new_obligation'),
        f'{_t(lang, "role")}: {_role_label(context["role"], lang)}',
        f'{_t(lang, "counterparty")}: {_user_label(counterparty)}',
        '',
        _t(lang, 'send_title'),
    ])


def _new_obligation_cancel_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
            [_home_button(lang)],
        ],
    }


def _new_obligation_date_text(lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'new_obligation'),
        '',
        _t(lang, 'choose_opened_date'),
    ])


def _new_obligation_date_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'today'), 'newob:date:today')],
            [_telegram_button(_t(lang, 'custom_date'), 'newob:date:custom')],
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
        ],
    }


def _new_obligation_schedule_text(context, lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'new_obligation'),
        f'{_t(lang, "title")}: {context.get("title", "")}',
        f'{_t(lang, "amount")}: {_format_money(context.get("amount_units", 0))}',
        f'{_t(lang, "opened")}: {_format_date(context["opened_on"])}',
        '',
        _t(lang, 'choose_payment_schedule'),
    ])


def _new_obligation_schedule_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'one_time_payment'), f'newob:schedule:{PAYMENT_MODE_ONE_TIME}')],
            [_telegram_button(_t(lang, 'monthly_recurring'), f'newob:schedule:{EventSeries.Frequency.MONTHLY}')],
            [_telegram_button(_t(lang, 'weekly_recurring'), f'newob:schedule:{EventSeries.Frequency.WEEKLY}')],
            [_telegram_button(_t(lang, 'biweekly_recurring'), f'newob:schedule:{EventSeries.Frequency.BIWEEKLY}')],
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
        ],
    }


def _new_obligation_month_day_text(opened_on, lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'monthly_recurring'),
        '',
        _t(lang, 'monthly_day_prompt'),
    ])


def _new_obligation_month_day_markup(context, lang=LANG_EN):
    opened_on = context.get('opened_on') or timezone.localdate()
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'use_day', day=opened_on.day), f'newob:dom:{opened_on.day}')],
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
        ],
    }


def _new_obligation_interest_text(context, lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'new_obligation'),
        f'{_t(lang, "title")}: {context.get("title", "")}',
        f'{_t(lang, "schedule")}: {_new_obligation_schedule_label(context, lang)}',
        '',
        _t(lang, 'add_interest'),
    ])


def _new_obligation_interest_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'no_interest'), 'newob:interest:no')],
            [_telegram_button(_t(lang, 'with_interest'), 'newob:interest:yes')],
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
        ],
    }


def _new_obligation_confirm_text(user, context, lang=LANG_EN):
    counterparty = get_user_model().objects.filter(pk=context.get('counterparty_id')).first()
    counterparty_label = _user_label(counterparty) if counterparty else _t(lang, 'unknown')
    interest_label = _t(lang, 'yes_no_no')
    if context.get('has_interest'):
        interest_label = f'{context["annual_rate_percent"]}% APY'
    return '\n'.join([
        _t(lang, 'create_obligation_question'),
        f'{_t(lang, "title")}: {context.get("title", "")}',
        f'{_t(lang, "role")}: {_role_label(context.get("role"), lang)}',
        f'{_t(lang, "counterparty")}: {counterparty_label}',
        f'{_t(lang, "amount")}: {_format_money(context.get("amount_units", 0))}',
        f'{_t(lang, "opened")}: {_format_date(context.get("opened_on"))}',
        f'{_t(lang, "schedule")}: {_new_obligation_schedule_label(context, lang)}',
        f'{_t(lang, "interest")}: {interest_label}',
    ])


def _new_obligation_confirm_markup(lang=LANG_EN):
    return {
        'inline_keyboard': [
            [_telegram_button(_t(lang, 'create_obligation'), 'newob:create', BUTTON_STYLE_SUCCESS)],
            [_telegram_button(_t(lang, 'cancel'), 'newob:cancel', BUTTON_STYLE_DANGER)],
        ],
    }


def _new_obligation_created_text(obligation, lang=LANG_EN):
    return '\n'.join([
        _t(lang, 'obligation_created'),
        f'{_t(lang, "title")}: {obligation.title}',
        f'{_t(lang, "current_balance")}: {_format_money(get_obligation_balance(obligation))}',
    ])


def _repayment_amount_markup(obligation, balance_units, lang=LANG_EN):
    rows = []
    used_amounts = set()
    quick_buttons = []
    for amount in QUICK_REPAYMENT_AMOUNTS:
        amount_units = units_from_decimal(amount)
        if amount_units <= balance_units:
            used_amounts.add(amount_units)
            quick_buttons.append(_telegram_button(
                _t(lang, 'pay', amount=_format_money(amount_units)),
                f'repayamt:{obligation.pk}:{amount_units}',
                BUTTON_STYLE_SUCCESS,
            ))
    for index in range(0, len(quick_buttons), 2):
        rows.append(quick_buttons[index:index + 2])

    if balance_units not in used_amounts:
        rows.append([
            _telegram_button(
                _t(lang, 'pay_full_balance', amount=_format_money(balance_units)),
                f'repayamt:{obligation.pk}:{balance_units}',
                BUTTON_STYLE_SUCCESS,
            )
        ])

    rows.append([_telegram_button(_t(lang, 'custom_amount'), f'customrepay:{obligation.pk}', BUTTON_STYLE_PRIMARY)])
    rows.append([_telegram_button(_t(lang, 'back'), f'ob:{obligation.pk}', BUTTON_STYLE_PRIMARY)])
    return {'inline_keyboard': rows}


def _manual_transfer_error_markup(obligation, transfer_type, today, lang=LANG_EN):
    if transfer_type == FinancialEvent.EventType.REPAYMENT:
        return _repayment_amount_markup(obligation, get_obligation_balance(obligation, as_of=today), lang)
    return _obligation_detail_markup(obligation, lang)


def _format_money(amount_units):
    return f'${decimal_from_units(amount_units):,.2f}'


def _format_signed_money(amount_units):
    sign = '+' if amount_units >= 0 else '-'
    return f'{sign}{_format_money(abs(amount_units))}'


def _format_date(value):
    return value.strftime('%m/%d/%Y')


def _role_label(role, lang=LANG_EN):
    if role == ROLE_LENT:
        return _t(lang, 'role_lent')
    if role == ROLE_BORROWED:
        return _t(lang, 'role_borrowed')
    return _t(lang, 'unknown')


def _new_obligation_schedule_label(context, lang=LANG_EN):
    if context.get('payment_mode') == PAYMENT_MODE_ONE_TIME:
        return _t(lang, 'schedule_one_time')

    frequency = context.get('recurring_frequency')
    if frequency == EventSeries.Frequency.MONTHLY:
        return _t(lang, 'schedule_monthly', day=context.get('recurring_day_of_month'))
    if frequency == EventSeries.Frequency.WEEKLY:
        day_name = DAY_NAME_TEXT.get(lang, DAY_NAMES)[context.get('recurring_day_of_week') or 0]
        return _t(lang, 'schedule_weekly', day=day_name)
    if frequency == EventSeries.Frequency.BIWEEKLY:
        day_name = DAY_NAME_TEXT.get(lang, DAY_NAMES)[context.get('recurring_day_of_week') or 0]
        return _t(lang, 'schedule_biweekly', day=day_name)
    return _t(lang, 'schedule_recurring')


def _event_type_label(event, lang=LANG_EN):
    key = f'event_{event.event_type}'
    if key in TELEGRAM_TEXT:
        return _t(lang, key)
    return event.get_event_type_display()


def _validation_error_text(error, lang=LANG_EN):
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
