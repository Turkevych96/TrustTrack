from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from ledger.models import (
    EventSeries,
    EventSeriesVersion,
    FinancialEvent,
    InterestAccrualRun,
    InterestRatePeriod,
    LedgerEntry,
    LedgerTransaction,
    Obligation,
    ObligationCategory,
    TelegramLoginChallenge,
    UserProfile,
)
from ledger.services.backups import create_sqlite_backup
from ledger.services.balances import get_obligation_balance
from ledger.services.events import ensure_obligation_accounts, post_principal_advance, post_repayment, post_scheduled_charge
from ledger.services.due_jobs import run_due_jobs
from ledger.services.interest import (
    calculate_monthly_interest,
    generate_due_interest,
    post_monthly_interest,
    recalculate_interest_from,
)
from ledger.services.notifications import DueJobNotificationItem, build_due_job_notification_messages
from ledger.services.planner import build_portfolio_projection, simulate_monthly_payment
from ledger.services.recurring import (
    generate_due_recurring_events,
    generate_recurring_events_for_month,
    recalculate_due_recurring_events,
)
from ledger.services.telegram import TelegramChatIdentity, TelegramLookupError
from ledger.services.telegram_bot import PENDING_OBLIGATION_CREATIONS, PENDING_REPAYMENT_OBLIGATIONS, process_telegram_update
from ledger.templatetags.money import money_units


TEST_STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}


class LedgerTestCase(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.creditor_user = user_model.objects.create_user(
            username='alex',
            email='alex@example.com',
            first_name='Alex',
        )
        self.borrower_user = user_model.objects.create_user(
            username='andrii',
            email='andrii@example.com',
            first_name='Andrii',
        )
        self.obligation = Obligation.objects.create(
            creditor=self.creditor_user,
            borrower=self.borrower_user,
            title='Test loan',
            opened_on=date(2026, 1, 1),
        )


class ModelRuleTests(LedgerTestCase):
    def test_obligation_uses_real_users_directly(self):
        self.assertEqual(self.obligation.creditor, self.creditor_user)
        self.assertEqual(self.obligation.borrower, self.borrower_user)

    def test_obligation_requires_different_creditor_and_borrower(self):
        obligation = Obligation(
            creditor=self.creditor_user,
            borrower=self.creditor_user,
            title='Invalid',
            opened_on=date(2026, 1, 1),
        )

        with self.assertRaises(ValidationError):
            obligation.full_clean()

    def test_money_display_uses_two_decimals(self):
        self.assertEqual(money_units(1_000_000), '$100.00')
        self.assertEqual(money_units(1_000_055), '$100.01')

    def test_unbalanced_transaction_cannot_post(self):
        receivable, _ = ensure_obligation_accounts(self.obligation)
        event = FinancialEvent.objects.create(
            obligation=self.obligation,
            event_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE,
            source=FinancialEvent.Source.MANUAL,
            event_date=date(2026, 1, 1),
            amount_units=1_000_000,
            direction=FinancialEvent.Direction.INCREASES_DEBT,
        )
        ledger_transaction = LedgerTransaction.objects.create(
            obligation=self.obligation,
            financial_event=event,
            transaction_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE,
            transaction_date=date(2026, 1, 1),
        )
        LedgerEntry.objects.create(
            transaction=ledger_transaction,
            account=receivable,
            entry_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE,
            effective_date=date(2026, 1, 1),
            side=LedgerEntry.Side.DEBIT,
            amount_units=1_000_000,
        )

        with self.assertRaises(ValidationError):
            ledger_transaction.post()

    def test_posted_entries_are_immutable(self):
        ledger_transaction = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        )
        entry = ledger_transaction.entries.first()
        entry.amount_units += 1

        with self.assertRaises(ValidationError):
            entry.save()

    def test_posted_transactions_are_immutable(self):
        ledger_transaction = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        )
        ledger_transaction.memo = 'changed'

        with self.assertRaises(ValidationError):
            ledger_transaction.save()


class BalanceTests(LedgerTestCase):
    def test_principal_advance_creates_balance(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        self.assertEqual(get_obligation_balance(self.obligation), 1_000_000)

    def test_repayment_reduces_balance(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=250_000, event_date=date(2026, 1, 10))

        self.assertEqual(get_obligation_balance(self.obligation), 750_000)

    def test_overpayment_is_rejected(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        with self.assertRaises(ValidationError):
            post_repayment(self.obligation, amount_units=1_250_000, event_date=date(2026, 1, 10))


class TelegramBotTests(LedgerTestCase):
    def tearDown(self):
        PENDING_OBLIGATION_CREATIONS.clear()
        PENDING_REPAYMENT_OBLIGATIONS.clear()
        super().tearDown()

    def test_start_requires_profile_telegram_id(self):
        result = process_telegram_update(self._telegram_message('/start', telegram_id=555))

        self.assertEqual(len(result.messages), 1)
        self.assertIn('Access is not configured', result.messages[0].text)
        self.assertIn('555', result.messages[0].text)

    def test_login_command_confirms_challenge_for_configured_telegram_id(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        challenge = TelegramLoginChallenge.objects.create(
            token='login-token',
            code='ABCD1234',
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        result = process_telegram_update(self._telegram_message('/login ABCD-1234', telegram_id=555))

        challenge.refresh_from_db()
        self.assertEqual(challenge.user, self.borrower_user)
        self.assertEqual(challenge.telegram_id, 555)
        self.assertIsNotNone(challenge.confirmed_at)
        self.assertIn('Web login confirmed for Andrii', result.messages[0].text)

    def test_start_login_payload_confirms_challenge_for_configured_telegram_id(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        challenge = TelegramLoginChallenge.objects.create(
            token='deep-link-token',
            code='EFGH5678',
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        result = process_telegram_update(self._telegram_message('/start login_deep-link-token', telegram_id=555))

        challenge.refresh_from_db()
        self.assertEqual(challenge.user, self.borrower_user)
        self.assertIsNotNone(challenge.confirmed_at)
        self.assertIn('Web login confirmed for Andrii', result.messages[0].text)

    def test_login_command_requires_configured_telegram_id(self):
        challenge = TelegramLoginChallenge.objects.create(
            token='login-token',
            code='ABCD1234',
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        result = process_telegram_update(self._telegram_message('/login ABCD-1234', telegram_id=555))

        challenge.refresh_from_db()
        self.assertIsNone(challenge.user)
        self.assertIsNone(challenge.confirmed_at)
        self.assertIn('Access is not configured', result.messages[0].text)

    def test_start_shows_home_summary_without_obligation_list(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        result = process_telegram_update(
            self._telegram_message('/start', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertEqual(len(result.messages), 1)
        self.assertIn('TrustTrack', result.messages[0].text)
        self.assertIn('User: Andrii', result.messages[0].text)
        self.assertIn('I owe: $100.00', result.messages[0].text)
        self.assertIn('Net: -$100.00', result.messages[0].text)
        self.assertNotIn(self.obligation.title, result.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', result.messages[0].text)
        self.assertNotIn('/start -', result.messages[0].text)
        self.assertIn('$100.00', result.messages[0].text)
        self.assertEqual(
            result.messages[0].reply_markup['inline_keyboard'],
            [
                [{'text': 'Balance', 'callback_data': 'menu:balance'}],
                [{'text': 'Open obligations', 'callback_data': 'menu:obligations'}],
                [{'text': 'Recent transactions', 'callback_data': 'menu:recent'}],
            ],
        )

    def test_settings_switches_telegram_language_to_russian(self):
        profile = UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        settings = process_telegram_update(self._telegram_message('/settings', telegram_id=555))
        switched = process_telegram_update(self._telegram_callback('settings:lang:ru', telegram_id=555))
        profile.refresh_from_db()
        start = process_telegram_update(
            self._telegram_message('/start', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertIn('Settings', settings.messages[0].text)
        self.assertIn('Balance notifications: on', settings.messages[0].text)
        self.assertEqual(
            settings.messages[0].reply_markup['inline_keyboard'][0],
            [
                {'text': 'English', 'callback_data': 'settings:lang:en'},
                {'text': 'Russian', 'callback_data': 'settings:lang:ru'},
            ],
        )
        self.assertEqual(profile.telegram_language, UserProfile.TelegramLanguage.RUSSIAN)
        self.assertTrue(switched.messages[0].replace_existing)
        self.assertEqual(switched.callback_text, 'Язык обновлён.')
        self.assertIn('Настройки', switched.messages[0].text)
        self.assertIn('Текущий язык: русский', switched.messages[0].text)
        self.assertIn('Пользователь: Andrii', start.messages[0].text)
        self.assertIn('Я должен: $0.00', start.messages[0].text)
        self.assertEqual(
            start.messages[0].reply_markup['inline_keyboard'],
            [
                [{'text': 'Баланс', 'callback_data': 'menu:balance'}],
                [{'text': 'Открытые обязательства', 'callback_data': 'menu:obligations'}],
                [{'text': 'Последние операции', 'callback_data': 'menu:recent'}],
            ],
        )

    def test_settings_can_disable_telegram_due_notifications(self):
        profile = UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        disabled = process_telegram_update(self._telegram_callback('settings:due:off', telegram_id=555))
        profile.refresh_from_db()
        self.assertFalse(profile.payment_due_notifications)

        enabled = process_telegram_update(self._telegram_callback('settings:due:on', telegram_id=555))
        profile.refresh_from_db()

        self.assertIn('Notification settings updated', disabled.messages[0].text)
        self.assertIn('Balance notifications: off', disabled.messages[0].text)
        self.assertEqual(disabled.messages[0].reply_markup['inline_keyboard'][1][0]['text'], 'Turn notifications on')
        self.assertIn('Balance notifications: on', enabled.messages[0].text)
        self.assertTrue(profile.payment_due_notifications)

    def test_russian_language_localizes_telegram_panels(self):
        UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            telegram_language=UserProfile.TelegramLanguage.RUSSIAN,
        )
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=250_000, event_date=date(2026, 1, 10))

        obligations = process_telegram_update(
            self._telegram_callback('menu:obligations', telegram_id=555),
            today=date(2026, 1, 10),
        )
        detail = process_telegram_update(
            self._telegram_callback(f'ob:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        recent = process_telegram_update(
            self._telegram_callback('menu:recent', telegram_id=555),
            today=date(2026, 1, 10),
        )
        group_warning = process_telegram_update(
            self._telegram_message('/start', telegram_id=555, chat_id=-100, chat_type='group'),
        )

        self.assertIn('Открытые обязательства', obligations.messages[0].text)
        self.assertEqual(
            obligations.messages[0].reply_markup['inline_keyboard'][-2],
            [{'text': 'Новое обязательство', 'callback_data': 'menu:new_obligation'}],
        )
        self.assertIn('Ваша роль: заёмщик', detail.messages[0].text)
        self.assertIn('Текущий баланс: $75.00', detail.messages[0].text)
        self.assertEqual(
            detail.messages[0].reply_markup['inline_keyboard'][0][0],
            {'text': 'Ручная операция', 'callback_data': f'transfermenu:{self.obligation.pk}'},
        )
        self.assertIn('Последние операции', recent.messages[0].text)
        self.assertIn('Погашение', recent.messages[0].text)
        self.assertIn('личном чате', group_warning.messages[0].text)

    def test_home_button_edits_existing_panel(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        result = process_telegram_update(
            self._telegram_callback('menu:home', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertTrue(result.messages[0].replace_existing)
        self.assertEqual(result.messages[0].message_id, 99)
        self.assertIn('TrustTrack', result.messages[0].text)
        self.assertIn('I owe: $100.00', result.messages[0].text)

    def test_recent_transactions_button_edits_panel(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=250_000, event_date=date(2026, 1, 10))

        result = process_telegram_update(
            self._telegram_callback('menu:recent', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertTrue(result.messages[0].replace_existing)
        self.assertIn('Recent transactions', result.messages[0].text)
        self.assertIn('Test loan', result.messages[0].text)
        self.assertIn('Repayment', result.messages[0].text)
        self.assertIn('01/10/2026', result.messages[0].text)
        self.assertIn('+$25.00', result.messages[0].text)
        self.assertIn('-$100.00', result.messages[0].text)

    def test_new_obligation_wizard_creates_one_time_obligation(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        start = process_telegram_update(self._telegram_callback('menu:new_obligation', telegram_id=555))
        role = process_telegram_update(self._telegram_callback('newob:role:lent', telegram_id=555))
        counterparty = process_telegram_update(self._telegram_callback(f'newob:cp:{self.creditor_user.pk}', telegram_id=555))
        title = process_telegram_update(self._telegram_message('Phone', telegram_id=555))
        amount = process_telegram_update(self._telegram_message('125,00', telegram_id=555))
        opened = process_telegram_update(
            self._telegram_callback('newob:date:today', telegram_id=555),
            today=date(2026, 1, 10),
        )
        schedule = process_telegram_update(self._telegram_callback('newob:schedule:one_time', telegram_id=555))
        interest = process_telegram_update(self._telegram_callback('newob:interest:no', telegram_id=555))
        created = process_telegram_update(self._telegram_callback('newob:create', telegram_id=555))

        self.assertIn('Choose your role', start.messages[0].text)
        self.assertEqual(
            [row[0]['text'] for row in start.messages[0].reply_markup['inline_keyboard'][:2]],
            ['Creditor', 'Borrower'],
        )
        self.assertIn('Who borrowed from you', role.messages[0].text)
        self.assertIn('Send title', counterparty.messages[0].text)
        self.assertIn('Send initial amount', title.messages[0].text)
        self.assertIn('Choose opened date', amount.messages[0].text)
        self.assertIn('Choose payment schedule', opened.messages[0].text)
        self.assertIn('Opened: 01/10/2026', opened.messages[0].text)
        self.assertIn('Add interest', schedule.messages[0].text)
        self.assertIn('Create obligation?', interest.messages[0].text)
        self.assertIn('Obligation created', created.messages[0].text)

        obligation = Obligation.objects.get(title='Phone')
        self.assertEqual(obligation.creditor, self.borrower_user)
        self.assertEqual(obligation.borrower, self.creditor_user)
        self.assertEqual(get_obligation_balance(obligation), 1_250_000)
        self.assertNotIn(555, PENDING_OBLIGATION_CREATIONS)

    def test_new_obligation_custom_date_uses_american_format(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        process_telegram_update(self._telegram_callback('menu:new_obligation', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:role:lent', telegram_id=555))
        process_telegram_update(self._telegram_callback(f'newob:cp:{self.creditor_user.pk}', telegram_id=555))
        process_telegram_update(self._telegram_message('Camera', telegram_id=555))
        process_telegram_update(self._telegram_message('125.00', telegram_id=555))
        prompt = process_telegram_update(self._telegram_callback('newob:date:custom', telegram_id=555))
        schedule = process_telegram_update(
            self._telegram_message('01/10/2026', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertIn('Send opened date as MM/DD/YYYY', prompt.messages[0].text)
        self.assertIn('Choose payment schedule', schedule.messages[0].text)
        self.assertIn('Opened: 01/10/2026', schedule.messages[0].text)

    def test_new_obligation_wizard_creates_monthly_interest_obligation(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        process_telegram_update(self._telegram_callback('menu:new_obligation', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:role:borrowed', telegram_id=555))
        process_telegram_update(self._telegram_callback(f'newob:cp:{self.creditor_user.pk}', telegram_id=555))
        process_telegram_update(self._telegram_message('Telegram rent', telegram_id=555))
        process_telegram_update(self._telegram_message('625', telegram_id=555))
        process_telegram_update(
            self._telegram_callback('newob:date:today', telegram_id=555),
            today=date(2026, 1, 10),
        )
        process_telegram_update(self._telegram_callback('newob:schedule:monthly', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:dom:1', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:interest:yes', telegram_id=555))
        confirmation = process_telegram_update(self._telegram_message('3,5', telegram_id=555))
        created = process_telegram_update(self._telegram_callback('newob:create', telegram_id=555))

        obligation = Obligation.objects.get(title='Telegram rent')
        series = EventSeries.objects.get(obligation=obligation)
        rate = InterestRatePeriod.objects.get(obligation=obligation)
        self.assertIn('3.5% APY', confirmation.messages[0].text)
        self.assertIn('Obligation created', created.messages[0].text)
        self.assertEqual(obligation.creditor, self.creditor_user)
        self.assertEqual(obligation.borrower, self.borrower_user)
        self.assertEqual(series.frequency, EventSeries.Frequency.MONTHLY)
        self.assertEqual(series.day_of_month, 1)
        self.assertEqual(rate.annual_rate_percent, Decimal('3.5'))

    def test_new_obligation_recalculates_backdated_recurring_and_interest(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        process_telegram_update(self._telegram_callback('menu:new_obligation', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:role:borrowed', telegram_id=555))
        process_telegram_update(self._telegram_callback(f'newob:cp:{self.creditor_user.pk}', telegram_id=555))
        process_telegram_update(self._telegram_message('Backdated rent', telegram_id=555))
        process_telegram_update(self._telegram_message('100.00', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:date:custom', telegram_id=555))
        process_telegram_update(self._telegram_message('01/10/2026', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:schedule:monthly', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:dom:1', telegram_id=555))
        process_telegram_update(self._telegram_callback('newob:interest:yes', telegram_id=555))
        process_telegram_update(self._telegram_message('3.5', telegram_id=555))
        with patch('ledger.services.recalculation.timezone.localdate', return_value=date(2026, 4, 15)):
            created = process_telegram_update(self._telegram_callback('newob:create', telegram_id=555))

        obligation = Obligation.objects.get(title='Backdated rent')
        generated_events = FinancialEvent.objects.filter(
            obligation=obligation,
            source=FinancialEvent.Source.GENERATED,
            event_series__isnull=False,
            voided_at__isnull=True,
        )
        posted_interest = InterestAccrualRun.objects.filter(
            obligation=obligation,
            status=InterestAccrualRun.Status.POSTED,
        )

        self.assertIn('Obligation created', created.messages[0].text)
        self.assertEqual(generated_events.count(), 3)
        self.assertEqual(posted_interest.count(), 3)
        self.assertGreater(get_obligation_balance(obligation), 4_000_000)

    def test_obligation_buttons_open_detail_and_repayment_amounts(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        list_result = process_telegram_update(
            self._telegram_callback('menu:obligations', telegram_id=555),
            today=date(2026, 1, 10),
        )
        detail_result = process_telegram_update(
            self._telegram_callback(f'ob:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        repayment_menu_result = process_telegram_update(
            self._telegram_callback(f'transfer:repayment:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        transfer_menu_result = process_telegram_update(
            self._telegram_callback(f'transfermenu:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )

        self.assertEqual(
            list_result.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'ob:{self.obligation.pk}',
        )
        self.assertTrue(list_result.messages[0].replace_existing)
        self.assertEqual(list_result.messages[0].message_id, 99)
        self.assertEqual(
            list_result.messages[0].reply_markup['inline_keyboard'][-2],
            [{'text': 'New obligation', 'callback_data': 'menu:new_obligation'}],
        )
        self.assertEqual(
            list_result.messages[0].reply_markup['inline_keyboard'][-1],
            [
                {'text': 'Home', 'callback_data': 'menu:home'},
                {'text': 'Balance', 'callback_data': 'menu:balance'},
            ],
        )
        self.assertIn(self.obligation.title, list_result.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', list_result.messages[0].text)
        self.assertIn('Current balance: $100.00', detail_result.messages[0].text)
        self.assertTrue(detail_result.messages[0].replace_existing)
        self.assertIn(self.obligation.title, detail_result.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', detail_result.messages[0].text)
        self.assertEqual(
            detail_result.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'transfermenu:{self.obligation.pk}',
        )
        self.assertIn('Manual transfer', transfer_menu_result.messages[0].text)
        self.assertEqual(
            transfer_menu_result.messages[0].reply_markup['inline_keyboard'][:2],
            [
                [{'text': 'Repayment', 'callback_data': f'transfer:repayment:{self.obligation.pk}'}],
                [{'text': 'Debt increase', 'callback_data': f'transfer:advance:{self.obligation.pk}'}],
            ],
        )
        self.assertNotIn(f'O{self.obligation.pk}', repayment_menu_result.messages[0].text)
        repayment_buttons = repayment_menu_result.messages[0].reply_markup['inline_keyboard']
        self.assertTrue(repayment_menu_result.messages[0].replace_existing)
        self.assertIn({'text': 'Pay $25.00', 'callback_data': f'repayamt:{self.obligation.pk}:250000'}, repayment_buttons[0])
        self.assertEqual(
            repayment_buttons[-2][0],
            {'text': 'Custom amount', 'callback_data': f'customrepay:{self.obligation.pk}'},
        )

    def test_repayment_amount_button_opens_confirmation(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        result = process_telegram_update(
            self._telegram_callback(f'repayamt:{self.obligation.pk}:250000', telegram_id=555),
            today=date(2026, 1, 10),
            nonce_factory=lambda: 'fixed',
        )

        self.assertIn('Confirm repayment', result.messages[0].text)
        self.assertTrue(result.messages[0].replace_existing)
        self.assertEqual(result.messages[0].message_id, 99)
        self.assertNotIn(f'O{self.obligation.pk}', result.messages[0].text)
        self.assertIn('Date: 01/10/2026', result.messages[0].text)
        self.assertEqual(
            result.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'repay:{self.obligation.pk}:250000:2026-01-10:fixed',
        )

    def test_custom_repayment_amount_uses_selected_obligation(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        custom_prompt = process_telegram_update(
            self._telegram_callback(f'customrepay:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        confirmation = process_telegram_update(
            self._telegram_message('37.50', telegram_id=555),
            today=date(2026, 1, 10),
            nonce_factory=lambda: 'custom',
        )

        self.assertIn('Send only the amount', custom_prompt.messages[0].text)
        self.assertTrue(custom_prompt.messages[0].replace_existing)
        self.assertNotIn(f'O{self.obligation.pk}', custom_prompt.messages[0].text)
        self.assertIn('Confirm repayment', confirmation.messages[0].text)
        self.assertTrue(confirmation.messages[0].replace_existing)
        self.assertEqual(confirmation.messages[0].message_id, 99)
        self.assertIn('Amount: $37.50', confirmation.messages[0].text)
        self.assertIn('Date: 01/10/2026', confirmation.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', confirmation.messages[0].text)
        self.assertEqual(
            confirmation.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'repay:{self.obligation.pk}:375000:2026-01-10:custom',
        )
        self.assertNotIn(555, PENDING_REPAYMENT_OBLIGATIONS)

    def test_custom_repayment_amount_accepts_single_decimal_comma(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        process_telegram_update(
            self._telegram_callback(f'customrepay:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        confirmation = process_telegram_update(
            self._telegram_message('54,50', telegram_id=555),
            today=date(2026, 1, 10),
            nonce_factory=lambda: 'comma',
        )

        self.assertIn('Confirm repayment', confirmation.messages[0].text)
        self.assertIn('Amount: $54.50', confirmation.messages[0].text)
        self.assertIn('Date: 01/10/2026', confirmation.messages[0].text)
        self.assertEqual(
            confirmation.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'repay:{self.obligation.pk}:545000:2026-01-10:comma',
        )

    def test_repayment_preview_requires_confirmation_button(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        result = process_telegram_update(
            self._telegram_message(f'/repay O{self.obligation.pk} 25', telegram_id=555),
            today=date(2026, 1, 10),
            nonce_factory=lambda: 'fixed',
        )

        self.assertEqual(get_obligation_balance(self.obligation), 1_000_000)
        self.assertEqual(len(result.messages), 1)
        self.assertIn('Confirm repayment', result.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', result.messages[0].text)
        self.assertIn('Date: 01/10/2026', result.messages[0].text)
        self.assertEqual(
            result.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'repay:{self.obligation.pk}:250000:2026-01-10:fixed',
        )

    def test_repayment_callback_posts_once(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        update = self._telegram_callback(
            f'repay:{self.obligation.pk}:250000:2026-01-10:nonce',
            telegram_id=555,
        )

        first_result = process_telegram_update(update, today=date(2026, 1, 10))
        second_result = process_telegram_update(update, today=date(2026, 1, 10))

        self.assertEqual(get_obligation_balance(self.obligation), 750_000)
        self.assertIn('Repayment recorded', first_result.messages[0].text)
        self.assertTrue(first_result.messages[0].replace_existing)
        self.assertIn('Date: 01/10/2026', first_result.messages[0].text)
        self.assertNotIn(f'O{self.obligation.pk}', first_result.messages[0].text)
        self.assertIn('already recorded', second_result.messages[0].text)
        self.assertTrue(second_result.messages[0].replace_existing)
        self.assertNotIn(f'O{self.obligation.pk}', second_result.messages[0].text)
        self.assertEqual(LedgerTransaction.objects.filter(idempotency_key__startswith='telegram-repayment:').count(), 1)

    def test_debt_increase_button_records_manual_advance(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)

        prompt = process_telegram_update(
            self._telegram_callback(f'transfer:advance:{self.obligation.pk}', telegram_id=555),
            today=date(2026, 1, 10),
        )
        confirmation = process_telegram_update(
            self._telegram_message('75.00', telegram_id=555),
            today=date(2026, 1, 10),
            nonce_factory=lambda: 'advance',
        )
        update = self._telegram_callback(
            f'advance:{self.obligation.pk}:750000:2026-01-10:advance',
            telegram_id=555,
        )

        first_result = process_telegram_update(update, today=date(2026, 1, 10))
        second_result = process_telegram_update(update, today=date(2026, 1, 10))

        self.assertIn('Debt increase for Test loan', prompt.messages[0].text)
        self.assertIn('Confirm debt increase', confirmation.messages[0].text)
        self.assertIn('Action: Debt increase', confirmation.messages[0].text)
        self.assertEqual(
            confirmation.messages[0].reply_markup['inline_keyboard'][0][0]['callback_data'],
            f'advance:{self.obligation.pk}:750000:2026-01-10:advance',
        )
        self.assertEqual(get_obligation_balance(self.obligation), 750_000)
        self.assertIn('Debt increase recorded', first_result.messages[0].text)
        self.assertIn('already recorded', second_result.messages[0].text)
        self.assertEqual(LedgerTransaction.objects.filter(idempotency_key__startswith='telegram-advance:').count(), 1)

    def test_group_chat_does_not_expose_financial_data(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))

        result = process_telegram_update(
            self._telegram_message('/start', telegram_id=555, chat_id=-100, chat_type='group'),
        )

        self.assertEqual(len(result.messages), 1)
        self.assertIn('private chat', result.messages[0].text)
        self.assertNotIn(self.obligation.title, result.messages[0].text)

    def _telegram_message(self, text, telegram_id=555, chat_id=None, chat_type='private'):
        return {
            'message': {
                'chat': {
                    'id': telegram_id if chat_id is None else chat_id,
                    'type': chat_type,
                },
                'from': {
                    'id': telegram_id,
                },
                'text': text,
            },
        }

    def _telegram_callback(self, data, telegram_id=555, chat_id=None, chat_type='private', message_id=99):
        return {
            'callback_query': {
                'id': 'callback-1',
                'from': {
                    'id': telegram_id,
                },
                'message': {
                    'message_id': message_id,
                    'chat': {
                        'id': telegram_id if chat_id is None else chat_id,
                        'type': chat_type,
                    },
                },
                'data': data,
            },
        }


class RecurringTests(LedgerTestCase):
    def test_monthly_series_generates_once_per_month(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=date(2026, 1, 1),
        )

        first_run = generate_recurring_events_for_month(date(2026, 1, 1))
        second_run = generate_recurring_events_for_month(date(2026, 1, 1))

        self.assertEqual(len(first_run), 1)
        self.assertEqual(len(second_run), 0)
        self.assertEqual(get_obligation_balance(self.obligation), 10_000_000)

    def test_series_version_change_affects_future_months(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 6, 30),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=11_000_000,
            valid_from=date(2026, 7, 1),
        )

        transactions = generate_recurring_events_for_month(date(2026, 7, 1))

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].financial_event.amount_units, 11_000_000)

    def test_generate_due_recurring_events_backfills_without_duplicates(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2024, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=date(2024, 1, 1),
        )

        first_run = generate_due_recurring_events(
            obligation=self.obligation,
            through_date=date(2024, 4, 15),
        )
        second_run = generate_due_recurring_events(
            obligation=self.obligation,
            through_date=date(2024, 4, 15),
        )

        self.assertEqual(len(first_run), 4)
        self.assertEqual(len(second_run), 0)
        self.assertEqual(get_obligation_balance(self.obligation), 40_000_000)

    def test_closed_obligation_does_not_generate_due_recurring_events(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2024, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=date(2024, 1, 1),
        )
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2024, 1, 15)
        self.obligation.save()

        transactions = generate_due_recurring_events(
            obligation=self.obligation,
            through_date=date(2024, 4, 15),
        )

        self.assertEqual(transactions, [])

    def test_monthly_repayment_series_reduces_debt(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Direct deposit',
            event_type=FinancialEvent.EventType.REPAYMENT,
            day_of_month=5,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=100_000,
            valid_from=date(2026, 1, 1),
        )

        transactions = generate_recurring_events_for_month(date(2026, 1, 1))

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].transaction_type, FinancialEvent.EventType.REPAYMENT)
        self.assertEqual(get_obligation_balance(self.obligation), 900_000)

    def test_weekly_series_generates_each_selected_weekday_in_month(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Weekly service fee',
            frequency=EventSeries.Frequency.WEEKLY,
            day_of_week=2,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000,
            valid_from=date(2026, 1, 1),
        )

        transactions = generate_recurring_events_for_month(date(2026, 1, 1))

        self.assertEqual([transaction.transaction_date for transaction in transactions], [
            date(2026, 1, 7),
            date(2026, 1, 14),
            date(2026, 1, 21),
            date(2026, 1, 28),
        ])
        self.assertEqual(get_obligation_balance(self.obligation), 40_000)

    def test_biweekly_series_generates_every_two_weeks_from_anchor(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Biweekly direct deposit',
            event_type=FinancialEvent.EventType.REPAYMENT,
            frequency=EventSeries.Frequency.BIWEEKLY,
            day_of_week=4,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=100_000,
            valid_from=date(2026, 1, 1),
        )

        transactions = generate_recurring_events_for_month(date(2026, 1, 1))

        self.assertEqual([transaction.transaction_date for transaction in transactions], [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ])
        self.assertEqual(get_obligation_balance(self.obligation), 700_000)

    def test_recalculate_recurring_reverses_generated_events_after_schedule_end(self):
        post_principal_advance(self.obligation, amount_units=100_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Weekly direct deposit',
            event_type=FinancialEvent.EventType.REPAYMENT,
            frequency=EventSeries.Frequency.WEEKLY,
            day_of_week=0,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        generate_due_recurring_events(obligation=self.obligation, through_date=date(2026, 5, 28))
        generated_after_end = list(
            FinancialEvent.objects.filter(
                obligation=self.obligation,
                event_series=series,
                source=FinancialEvent.Source.GENERATED,
                event_date__gt=date(2026, 3, 31),
            )
        )
        old_balance = get_obligation_balance(self.obligation)

        series.ends_on = date(2026, 3, 31)
        series.save(update_fields=['ends_on', 'updated_at'])
        result = recalculate_due_recurring_events(
            self.obligation,
            from_date=date(2026, 4, 1),
            through_date=date(2026, 5, 28),
        )

        for event in generated_after_end:
            event.refresh_from_db()
            self.assertIsNotNone(event.voided_at)
        self.assertEqual(len(result['reversed_events']), len(generated_after_end))
        self.assertEqual(len(result['reversal_transactions']), len(generated_after_end))
        self.assertEqual(get_obligation_balance(self.obligation), old_balance + len(generated_after_end) * 1_000_000)

    def test_recalculate_recurring_can_restore_voided_events_as_new_revisions(self):
        post_principal_advance(self.obligation, amount_units=100_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Weekly direct deposit',
            event_type=FinancialEvent.EventType.REPAYMENT,
            frequency=EventSeries.Frequency.WEEKLY,
            day_of_week=0,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        generate_due_recurring_events(obligation=self.obligation, through_date=date(2026, 5, 28))
        original_balance = get_obligation_balance(self.obligation)

        series.ends_on = date(2026, 3, 31)
        series.save(update_fields=['ends_on', 'updated_at'])
        first_result = recalculate_due_recurring_events(
            self.obligation,
            from_date=date(2026, 4, 1),
            through_date=date(2026, 5, 28),
        )

        series.ends_on = None
        series.save(update_fields=['ends_on', 'updated_at'])
        second_result = recalculate_due_recurring_events(
            self.obligation,
            from_date=date(2026, 4, 1),
            through_date=date(2026, 5, 28),
        )

        active_restored_events = FinancialEvent.objects.filter(
            obligation=self.obligation,
            event_series=series,
            source=FinancialEvent.Source.GENERATED,
            event_date__gte=date(2026, 4, 1),
            event_date__lte=date(2026, 5, 28),
            voided_at__isnull=True,
            revision=2,
        )
        self.assertEqual(len(second_result['created_transactions']), len(first_result['reversed_events']))
        self.assertEqual(active_restored_events.count(), len(first_result['reversed_events']))
        self.assertEqual(get_obligation_balance(self.obligation), original_balance)


class InterestTests(LedgerTestCase):
    def test_interest_uses_apy_converted_to_monthly_rate(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        calculation = calculate_monthly_interest(self.obligation, date(2026, 1, 1))

        self.assertEqual(calculation['amount_units'], 29_106)
        self.assertEqual(calculation['daily_details'][0]['annual_rate_type'], 'APY')

    def test_apy_annual_total_matches_displayed_yield_after_monthly_posting(self):
        post_principal_advance(self.obligation, amount_units=10_000_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('3.5000'),
            effective_from=date(2026, 1, 1),
        )

        runs = generate_due_interest(self.obligation, through_date=date(2027, 1, 15))

        self.assertEqual(len(runs), 12)
        self.assertEqual(sum(run.calculated_interest_amount_units for run in runs), 349_999)
        self.assertEqual(money_units(sum(run.calculated_interest_amount_units for run in runs)), '$35.00')

    def test_repayment_changes_interest_base_from_repayment_date(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=1_825_000, event_date=date(2026, 1, 16))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        calculation = calculate_monthly_interest(self.obligation, date(2026, 1, 1))

        self.assertEqual(calculation['amount_units'], 21_594)

    def test_rate_change_splits_interest_calculation(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 1, 15),
        )
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('20.0000'),
            effective_from=date(2026, 1, 16),
        )

        calculation = calculate_monthly_interest(self.obligation, date(2026, 1, 1))

        self.assertEqual(calculation['amount_units'], 42_924)

    def test_monthly_interest_posting_creates_balanced_transaction(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        run = post_monthly_interest(self.obligation, date(2026, 1, 1))
        ledger_transaction = run.ledger_transaction
        debit_total = sum(
            entry.amount_units for entry in ledger_transaction.entries.all() if entry.side == LedgerEntry.Side.DEBIT
        )
        credit_total = sum(
            entry.amount_units for entry in ledger_transaction.entries.all() if entry.side == LedgerEntry.Side.CREDIT
        )

        self.assertEqual(run.calculated_interest_amount_units, 29_106)
        self.assertEqual(ledger_transaction.status, LedgerTransaction.Status.POSTED)
        self.assertEqual(debit_total, credit_total)

    def test_generate_due_interest_posts_completed_months_once(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        first_run = generate_due_interest(self.obligation, through_date=date(2026, 4, 15))
        second_run = generate_due_interest(self.obligation, through_date=date(2026, 4, 15))

        self.assertEqual(len(first_run), 3)
        self.assertEqual(len(second_run), 0)
        self.assertEqual([run.period_start for run in first_run], [
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ])

    def test_recalculate_interest_leaves_unchanged_runs_alone(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )
        january = post_monthly_interest(self.obligation, date(2026, 1, 1))
        february = post_monthly_interest(self.obligation, date(2026, 2, 1))
        transaction_count = LedgerTransaction.objects.count()

        result = recalculate_interest_from(
            self.obligation,
            from_date=date(2026, 1, 1),
            through_date=date(2026, 3, 15),
        )

        january.refresh_from_db()
        february.refresh_from_db()
        self.assertEqual(result['reversed_runs'], [])
        self.assertEqual(result['reversal_transactions'], [])
        self.assertEqual(result['posted_runs'], [])
        self.assertEqual(result['unchanged_runs'], [january, february])
        self.assertEqual(january.status, InterestAccrualRun.Status.POSTED)
        self.assertEqual(february.status, InterestAccrualRun.Status.POSTED)
        self.assertEqual(january.revision, 1)
        self.assertEqual(february.revision, 1)
        self.assertEqual(InterestAccrualRun.objects.count(), 2)
        self.assertEqual(LedgerTransaction.objects.count(), transaction_count)

    def test_recalculate_interest_reverses_old_runs_and_posts_new_revisions(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )
        original_january = post_monthly_interest(self.obligation, date(2026, 1, 1))
        original_february = post_monthly_interest(self.obligation, date(2026, 2, 1))
        old_balance = get_obligation_balance(self.obligation)

        post_repayment(self.obligation, amount_units=1_825_000, event_date=date(2026, 1, 16))
        result = recalculate_interest_from(
            self.obligation,
            from_date=date(2026, 1, 16),
            through_date=date(2026, 3, 15),
        )

        original_january.refresh_from_db()
        original_february.refresh_from_db()
        posted_runs = InterestAccrualRun.objects.filter(
            obligation=self.obligation,
            status=InterestAccrualRun.Status.POSTED,
        ).order_by('period_start')

        self.assertEqual(original_january.status, InterestAccrualRun.Status.VOIDED)
        self.assertEqual(original_february.status, InterestAccrualRun.Status.VOIDED)
        self.assertEqual(len(result['reversal_transactions']), 2)
        self.assertEqual([run.revision for run in posted_runs], [2, 2])
        self.assertEqual([run.calculated_interest_amount_units for run in posted_runs], [21_594, 14_725])
        self.assertLess(get_obligation_balance(self.obligation), old_balance)


class BackupTests(SimpleTestCase):
    def test_create_sqlite_backup_creates_dated_integrity_checked_copy(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.sqlite3'
            backup_dir = Path(temporary_directory) / 'backups'
            self._create_source_database(source_path)

            result = create_sqlite_backup(
                source_path,
                backup_dir,
                keep=5,
                now=datetime(2026, 6, 18, 8, 30, 0, tzinfo=datetime_timezone.utc),
            )

            self.assertEqual(result.backup_path.name, 'trusttrack-20260618-083000.sqlite3')
            self.assertEqual(result.integrity_check, 'ok')
            self.assertGreater(result.size_bytes, 0)
            self.assertEqual(self._backup_names(backup_dir), ['trusttrack-20260618-083000.sqlite3'])
            with closing(sqlite3.connect(result.backup_path)) as connection:
                rows = connection.execute('SELECT name FROM sample ORDER BY id').fetchall()
            self.assertEqual(rows, [('alpha',), ('beta',)])

    def test_create_sqlite_backup_prunes_old_backups(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.sqlite3'
            backup_dir = Path(temporary_directory) / 'backups'
            self._create_source_database(source_path)

            create_sqlite_backup(
                source_path,
                backup_dir,
                keep=2,
                now=datetime(2026, 6, 18, 8, 30, 0, tzinfo=datetime_timezone.utc),
            )
            create_sqlite_backup(
                source_path,
                backup_dir,
                keep=2,
                now=datetime(2026, 6, 19, 8, 30, 0, tzinfo=datetime_timezone.utc),
            )
            result = create_sqlite_backup(
                source_path,
                backup_dir,
                keep=2,
                now=datetime(2026, 6, 20, 8, 30, 0, tzinfo=datetime_timezone.utc),
            )

            self.assertEqual([path.name for path in result.deleted_paths], ['trusttrack-20260618-083000.sqlite3'])
            self.assertEqual(
                self._backup_names(backup_dir),
                ['trusttrack-20260619-083000.sqlite3', 'trusttrack-20260620-083000.sqlite3'],
            )

    def test_backup_sqlite_command_once_outputs_created_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.sqlite3'
            backup_dir = Path(temporary_directory) / 'backups'
            self._create_source_database(source_path)
            output = StringIO()

            call_command(
                'backup_sqlite',
                once=True,
                database_path=str(source_path),
                output_dir=str(backup_dir),
                keep=3,
                stdout=output,
            )

            self.assertIn('Backup created:', output.getvalue())
            self.assertIn('integrity: ok', output.getvalue())
            self.assertEqual(len(self._backup_names(backup_dir)), 1)

    def test_run_trusttrack_can_start_backup_scheduler_process(self):
        started_commands = []

        def remember_command(process):
            started_commands.append(process.command)

        with (
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.start', autospec=True) as start,
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.poll', autospec=True, return_value=None),
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.stop', autospec=True),
            patch('ledger.management.commands.run_trusttrack.time.sleep', side_effect=KeyboardInterrupt),
        ):
            start.side_effect = remember_command
            call_command(
                'run_trusttrack',
                '--no-site',
                '--no-bot',
                '--no-scheduler',
                '--backup-interval',
                '11',
                '--backup-keep',
                '4',
                stdout=StringIO(),
                stderr=StringIO(),
            )

        self.assertEqual(len(started_commands), 1)
        self.assertIn('backup_sqlite', started_commands[0])
        self.assertIn('--interval', started_commands[0])
        self.assertIn('11', started_commands[0])
        self.assertIn('--keep', started_commands[0])
        self.assertIn('4', started_commands[0])

    def test_run_trusttrack_can_start_site_with_gunicorn(self):
        started_commands = []

        def remember_command(process):
            started_commands.append(process.command)

        with (
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.start', autospec=True) as start,
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.poll', autospec=True, return_value=None),
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.stop', autospec=True),
            patch('ledger.management.commands.run_trusttrack.time.sleep', side_effect=KeyboardInterrupt),
        ):
            start.side_effect = remember_command
            call_command(
                'run_trusttrack',
                '--no-bot',
                '--no-scheduler',
                '--no-backups',
                '--site-runner',
                'gunicorn',
                '--host',
                '0.0.0.0',
                '--port',
                '9000',
                '--gunicorn-workers',
                '3',
                '--gunicorn-threads',
                '7',
                '--gunicorn-timeout',
                '99',
                '--gunicorn-keep-alive',
                '8',
                stdout=StringIO(),
                stderr=StringIO(),
            )

        self.assertEqual(len(started_commands), 1)
        self.assertIn('-m', started_commands[0])
        self.assertIn('gunicorn', started_commands[0])
        self.assertIn('trusttrack.wsgi:application', started_commands[0])
        self.assertIn('0.0.0.0:9000', started_commands[0])
        self.assertIn('gthread', started_commands[0])
        self.assertIn('3', started_commands[0])
        self.assertIn('7', started_commands[0])
        self.assertIn('99', started_commands[0])
        self.assertIn('8', started_commands[0])

    def _create_source_database(self, source_path):
        with closing(sqlite3.connect(source_path)) as connection:
            connection.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
            connection.executemany('INSERT INTO sample (name) VALUES (?)', [('alpha',), ('beta',)])
            connection.commit()

    def _backup_names(self, backup_dir):
        return sorted(path.name for path in Path(backup_dir).glob('trusttrack-*.sqlite3'))


class DueJobTests(LedgerTestCase):
    def test_run_due_jobs_notifies_obligation_participants_when_balance_changes(self):
        UserProfile.objects.create(user=self.creditor_user, telegram_id=111, telegram_chat_type='private')
        UserProfile.objects.create(user=self.borrower_user, telegram_id=222, telegram_chat_type='private')
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        second_obligation = Obligation.objects.create(
            creditor=self.creditor_user,
            borrower=self.borrower_user,
            title='Utilities',
            opened_on=date(2026, 1, 1),
        )
        second_series = EventSeries.objects.create(
            obligation=second_obligation,
            name='Utilities',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=second_series,
            amount_units=500_000,
            valid_from=date(2026, 1, 1),
        )

        with patch('ledger.services.notifications.send_telegram_message', return_value={'message_id': 1}) as send:
            first_result = run_due_jobs(through_date=date(2026, 1, 1))
            second_result = run_due_jobs(through_date=date(2026, 1, 1))

        self.assertEqual(first_result.recurring_created, 2)
        self.assertEqual(first_result.notifications_sent, 2)
        self.assertEqual(second_result.notifications_sent, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[0] for call in send.call_args_list}, {111, 222})
        messages_by_chat = {call.args[0]: call.args[1] for call in send.call_args_list}
        creditor_message = messages_by_chat[111]
        borrower_message = messages_by_chat[222]

        self.assertIn('TrustTrack balance updates', creditor_message)
        self.assertIn('2 obligation(s) changed', creditor_message)
        self.assertIn('Owed to you change: +$150.00', creditor_message)
        self.assertIn('Test loan', creditor_message)
        self.assertIn('Type: Owed to you', creditor_message)
        self.assertIn('Owed to you: $0.00 -> $100.00', creditor_message)
        self.assertIn('Scheduled: +$100.00', creditor_message)
        self.assertIn('Utilities', creditor_message)
        self.assertIn('Owed to you: $0.00 -> $50.00', creditor_message)
        self.assertIn('Scheduled: +$50.00', creditor_message)
        self.assertIn('/settings', creditor_message)

        self.assertIn('TrustTrack balance updates', borrower_message)
        self.assertIn('2 obligation(s) changed', borrower_message)
        self.assertIn('You owe change: +$150.00', borrower_message)
        self.assertIn('Test loan', borrower_message)
        self.assertIn('Type: You owe', borrower_message)
        self.assertIn('You owe: $0.00 -> $100.00', borrower_message)
        self.assertIn('Utilities', borrower_message)
        self.assertIn('You owe: $0.00 -> $50.00', borrower_message)
        self.assertIn('/settings', borrower_message)

    def test_run_due_jobs_respects_telegram_notification_opt_out(self):
        UserProfile.objects.create(user=self.creditor_user, telegram_id=111, telegram_chat_type='private')
        UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=222,
            telegram_chat_type='private',
            payment_due_notifications=False,
        )
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )

        with patch('ledger.services.notifications.send_telegram_message', return_value={'message_id': 1}) as send:
            result = run_due_jobs(through_date=date(2026, 1, 1))

        self.assertEqual(result.notifications_sent, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 111)

    def test_run_due_jobs_notification_shows_interest_amount(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=222, telegram_chat_type='private')
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('12.0000'),
            effective_from=date(2026, 1, 1),
        )

        with patch('ledger.services.notifications.send_telegram_message', return_value={'message_id': 1}) as send:
            result = run_due_jobs(through_date=date(2026, 2, 1))

        self.assertEqual(result.interest_posted, 1)
        message_text = send.call_args.args[1]
        self.assertNotIn('Scheduled:', message_text)
        self.assertIn('Interest: +$0.95', message_text)
        self.assertIn('You owe change: +$0.95', message_text)

    def test_due_job_notification_digest_splits_long_messages(self):
        profile = UserProfile.objects.create(user=self.borrower_user, telegram_id=222)
        items = []
        for index in range(4):
            obligation = Obligation.objects.create(
                creditor=self.creditor_user,
                borrower=self.borrower_user,
                title=f'Long notification loan {index}',
                opened_on=date(2026, 1, 1),
            )
            items.append(
                DueJobNotificationItem(
                    obligation=obligation,
                    balance_before_units=0,
                    balance_after_units=1_000_000,
                    recurring_change_units=1_000_000,
                    interest_units=0,
                )
            )

        messages = build_due_job_notification_messages(profile, items, max_message_length=260)

        self.assertGreater(len(messages), 1)
        self.assertIn('TrustTrack balance updates (1/', messages[0])
        self.assertIn('Long notification loan 0', messages[0])
        self.assertIn('/settings', messages[-1])

    def test_run_due_jobs_generates_due_recurring_and_interest_once(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        first_result = run_due_jobs(through_date=date(2026, 4, 15))
        second_result = run_due_jobs(through_date=date(2026, 4, 15))

        self.assertEqual(first_result.obligation_count, 1)
        self.assertEqual(first_result.recurring_created, 4)
        self.assertEqual(first_result.interest_posted, 3)
        self.assertEqual(first_result.error_count, 0)
        self.assertEqual(second_result.recurring_created, 0)
        self.assertEqual(second_result.interest_posted, 0)
        self.assertEqual(
            FinancialEvent.objects.filter(
                obligation=self.obligation,
                source=FinancialEvent.Source.GENERATED,
                event_series=series,
                voided_at__isnull=True,
            ).count(),
            4,
        )
        self.assertEqual(
            InterestAccrualRun.objects.filter(
                obligation=self.obligation,
                status=InterestAccrualRun.Status.POSTED,
            ).count(),
            3,
        )

    def test_run_due_jobs_command_once_outputs_summary(self):
        output = StringIO()
        errors = StringIO()

        call_command(
            'run_due_jobs',
            '--once',
            '--date',
            '2026-04-15',
            stdout=output,
            stderr=errors,
        )

        self.assertIn('Due jobs through 2026-04-15', output.getvalue())
        self.assertIn('checked 1 open obligation(s)', output.getvalue())
        self.assertEqual(errors.getvalue(), '')

    def test_run_trusttrack_can_start_due_scheduler_process(self):
        started_commands = []

        def remember_command(process):
            started_commands.append(process.command)

        with (
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.start', autospec=True) as start,
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.poll', autospec=True, return_value=None),
            patch('ledger.management.commands.run_trusttrack.ManagedProcess.stop', autospec=True),
            patch('ledger.management.commands.run_trusttrack.time.sleep', side_effect=KeyboardInterrupt),
        ):
            start.side_effect = remember_command
            call_command(
                'run_trusttrack',
                '--no-site',
                '--no-bot',
                '--no-backups',
                '--scheduler-interval',
                '7',
                stdout=StringIO(),
                stderr=StringIO(),
            )

        self.assertEqual(len(started_commands), 1)
        self.assertIn('run_due_jobs', started_commands[0])
        self.assertIn('--interval', started_commands[0])
        self.assertIn('7', started_commands[0])


class PlannerTests(LedgerTestCase):
    def test_portfolio_projection_uses_planned_recurring_repayments(self):
        post_principal_advance(self.obligation, amount_units=3_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Monthly repayment',
            event_type=FinancialEvent.EventType.REPAYMENT,
            day_of_month=2,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )

        projection = build_portfolio_projection(
            [self.obligation],
            self.borrower_user,
            months=3,
            start_date=date(2026, 1, 1),
        )

        self.assertEqual(projection['points'][0]['i_owe_units'], 3_000_000)
        self.assertEqual(projection['points'][-1]['i_owe_units'], 0)
        self.assertEqual(projection['rows'][0]['projected_balance_units'], 0)
        self.assertEqual(projection['rows'][0]['payoff_date'], date(2026, 3, 2))

    def test_monthly_payment_simulator_estimates_payoff_date(self):
        post_principal_advance(self.obligation, amount_units=3_000_000, event_date=date(2026, 1, 1))

        simulation = simulate_monthly_payment(
            self.obligation,
            monthly_payment_units=1_000_000,
            payment_day=2,
            months=3,
            start_date=date(2026, 1, 1),
        )

        self.assertEqual(simulation['payoff_date'], date(2026, 3, 2))
        self.assertEqual(simulation['points'][-1]['balance_units'], 0)


class ViewTests(LedgerTestCase):
    def test_anonymous_user_redirects_to_login(self):
        response = self.client.get(reverse('ledger:dashboard'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_login_page_links_to_signup(self):
        response = self.client.get(reverse('login'))

        self.assertContains(response, reverse('signup'))
        self.assertContains(response, 'Create account')
        self.assertContains(response, reverse('telegram_login'))
        self.assertContains(response, 'Log in with Telegram')

    @override_settings(TELEGRAM_BOT_USERNAME='TrustTrack_bot')
    def test_telegram_login_page_creates_challenge_with_qr_and_code(self):
        response = self.client.get(reverse('telegram_login'))

        challenge = TelegramLoginChallenge.objects.get()
        self.assertEqual(self.client.session['telegram_login_challenge_token'], challenge.token)
        self.assertContains(response, 'Telegram login')
        self.assertContains(response, 'Open Telegram')
        self.assertContains(response, '@TrustTrack_bot')
        self.assertContains(response, 'data:image/svg+xml')
        self.assertContains(response, 'telegram-command-copy')
        self.assertContains(response, f'data-copy-command="/login {challenge.code[:4]}-{challenge.code[4:]}"')
        self.assertNotContains(response, 'id_telegram_command')
        self.assertContains(response, reverse('telegram_login_status'))

    def test_telegram_login_status_authenticates_confirmed_session_challenge(self):
        self.client.get(reverse('telegram_login'))
        challenge = TelegramLoginChallenge.objects.get()
        challenge.user = self.borrower_user
        challenge.telegram_id = 555
        challenge.confirmed_at = timezone.now()
        challenge.save(update_fields=['user', 'telegram_id', 'confirmed_at', 'updated_at'])

        response = self.client.get(reverse('telegram_login_status'))

        challenge.refresh_from_db()
        self.assertEqual(response.json()['status'], 'authenticated')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.borrower_user.pk)
        self.assertIsNotNone(challenge.consumed_at)
        self.assertNotIn('telegram_login_challenge_token', self.client.session)

    def test_telegram_login_status_does_not_authenticate_without_matching_session_token(self):
        TelegramLoginChallenge.objects.create(
            token='other-token',
            code='ABCD1234',
            user=self.borrower_user,
            telegram_id=555,
            expires_at=timezone.now() + timedelta(minutes=5),
            confirmed_at=timezone.now(),
        )

        response = self.client.get(reverse('telegram_login_status'))

        self.assertEqual(response.json()['status'], 'missing')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signup_creates_user_profile_and_logs_user_in(self):
        response = self.client.post(
            reverse('signup'),
            {
                'username': 'maria',
                'first_name': 'Maria',
                'last_name': 'Ivanova',
                'email': 'maria@example.com',
                'password1': 'TrustTrackPass2026!',
                'password2': 'TrustTrackPass2026!',
            },
        )

        self.assertRedirects(response, reverse('ledger:dashboard'))
        user = get_user_model().objects.get(username='maria')
        self.assertEqual(user.get_full_name(), 'Maria Ivanova')
        self.assertEqual(user.email, 'maria@example.com')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_signup_redirects_authenticated_users_to_dashboard(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('signup'))

        self.assertRedirects(response, reverse('ledger:dashboard'))

    def test_password_rule_status_reports_valid_password(self):
        response = self.client.post(
            reverse('password_rules'),
            {
                'username': 'maria',
                'first_name': 'Maria',
                'last_name': 'Ivanova',
                'email': 'maria@example.com',
                'password': 'TrustTrackPass2026!',
            },
        )

        self.assertEqual(
            response.json()['rules'],
            {
                'personal': True,
                'length': True,
                'common': True,
                'numeric': True,
            },
        )

    def test_password_rule_status_reports_failed_rules(self):
        common_response = self.client.post(reverse('password_rules'), {'password': 'password'})
        similar_response = self.client.post(
            reverse('password_rules'),
            {
                'username': 'maria',
                'password': 'maria',
            },
        )
        numeric_response = self.client.post(reverse('password_rules'), {'password': '12345678'})

        self.assertFalse(common_response.json()['rules']['common'])
        self.assertFalse(similar_response.json()['rules']['personal'])
        self.assertFalse(similar_response.json()['rules']['length'])
        self.assertFalse(numeric_response.json()['rules']['numeric'])

    def test_user_sees_only_related_obligations(self):
        user_model = get_user_model()
        other_creditor = user_model.objects.create_user(username='casey')
        other_borrower = user_model.objects.create_user(username='taylor')
        Obligation.objects.create(
            creditor=other_creditor,
            borrower=other_borrower,
            title='Private loan',
            opened_on=date(2026, 1, 1),
        )
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:obligation_list'))

        self.assertContains(response, 'Test loan')
        self.assertNotContains(response, 'Private loan')

    def test_user_cannot_open_unrelated_obligation_detail(self):
        user_model = get_user_model()
        other_creditor = user_model.objects.create_user(username='casey')
        other_borrower = user_model.objects.create_user(username='taylor')
        obligation = Obligation.objects.create(
            creditor=other_creditor,
            borrower=other_borrower,
            title='Private loan',
            opened_on=date(2026, 1, 1),
        )
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))

        self.assertEqual(response.status_code, 404)

    def test_nav_hides_duplicate_new_obligation_and_limits_admin_link_to_staff(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:dashboard'))

        self.assertContains(response, 'New obligation', count=1)
        self.assertContains(response, reverse('ledger:profile'))
        self.assertContains(response, reverse('ledger:planner'))
        self.assertNotContains(response, reverse('admin:index'))
        self.assertNotContains(response, 'Admin')

        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        staff_response = self.client.get(reverse('ledger:dashboard'))

        self.assertContains(staff_response, 'New obligation', count=1)
        self.assertContains(staff_response, reverse('ledger:admin_panel'))
        self.assertNotContains(staff_response, reverse('admin:index'))
        self.assertContains(staff_response, 'Admin')

    def test_dashboard_recent_activity_uses_human_financial_events(self):
        post_principal_advance(
            self.obligation,
            amount_units=500_000,
            event_date=date(2026, 6, 22),
            category='telegram',
            memo='Telegram debt increase by andrii',
        )
        post_scheduled_charge(
            self.obligation,
            amount_units=100_000,
            event_date=date(2026, 6, 1),
            category='rent',
        )
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:dashboard'))

        latest_row = response.context['recent_activity_rows'][0]
        self.assertEqual(latest_row['label'], 'You borrowed')
        self.assertEqual(latest_row['category'], 'telegram')
        self.assertEqual(latest_row['details'], 'Telegram debt increase by andrii')
        self.assertEqual(latest_row['signed_amount_units'], 500_000)
        self.assertContains(response, 'Recent activity')
        self.assertContains(response, 'You borrowed')
        self.assertContains(response, 'telegram')
        self.assertContains(response, 'Telegram debt increase by andrii')
        self.assertContains(response, '+$50.00')
        self.assertContains(response, 'Charge added')
        self.assertNotContains(response, '<td>Principal advance</td>', html=True)

    def test_admin_panel_is_staff_only_and_links_to_django_admin(self):
        self.client.force_login(self.borrower_user)

        forbidden_response = self.client.get(reverse('ledger:admin_panel'))

        self.assertEqual(forbidden_response.status_code, 403)

        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:admin_panel'))

        self.assertContains(response, 'TrustTrack operations overview')
        self.assertContains(response, reverse('ledger:admin_users'))
        self.assertContains(response, reverse('admin:index'))
        self.assertContains(response, 'Django admin')
        self.assertContains(response, reverse('ledger:admin_obligations'))
        self.assertContains(response, 'Restore or remove inaccurate history')
        self.assertNotContains(response, reverse('ledger:obligation_create'))
        self.assertNotContains(response, 'Low-level model maintenance')

    def test_admin_obligations_page_lists_internal_management_actions(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:admin_obligations'))

        self.assertContains(response, 'Obligation directory')
        self.assertContains(response, 'Test loan')
        self.assertContains(response, 'Andrii')
        self.assertContains(response, 'owes Alex')
        self.assertContains(response, '$100.00')
        self.assertContains(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertContains(response, 'class="filter-bar"')
        self.assertNotContains(response, reverse('admin:ledger_obligation_change', kwargs={'object_id': self.obligation.pk}))
        self.assertNotContains(response, reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, f'DELETE {self.obligation.pk}')
        self.assertNotContains(response, reverse('ledger:obligation_create'))

    def test_staff_can_preview_unrelated_obligation_detail(self):
        advance = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        ).financial_event
        post_repayment(
            self.obligation,
            amount_units=100_000,
            event_date=date(2026, 1, 2),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['admin_preview'])
        self.assertFalse(response.context['can_manage_obligation'])
        self.assertEqual(response.context['role'], 'admin preview')
        self.assertEqual(response.context['activity_rows'][0]['label'], 'Repayment')
        self.assertEqual(response.context['activity_rows'][0]['signed_amount_units'], -100_000)
        self.assertEqual(response.context['manual_transfer_rows'][1]['label'], 'Debt increase')
        self.assertContains(response, 'Admin preview')
        self.assertContains(response, 'Creditor')
        self.assertContains(response, 'Borrower')
        self.assertNotContains(response, 'View mode')
        self.assertContains(response, 'Debt increase')
        self.assertContains(response, 'Repayment')
        self.assertContains(response, '+$100.00')
        self.assertContains(response, '-$10.00')
        self.assertContains(response, reverse('ledger:admin_obligation_close', kwargs={'pk': self.obligation.pk}))
        self.assertContains(response, f'CLOSE {self.obligation.pk}')
        self.assertNotContains(response, 'Admin obligations')
        self.assertNotContains(response, reverse('admin:ledger_obligation_change', kwargs={'object_id': self.obligation.pk}))
        self.assertNotContains(response, reverse('ledger:admin_obligation_restore', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, 'You lent')
        self.assertNotContains(response, 'You received')
        self.assertNotContains(response, 'data-recalculate-form')
        self.assertNotContains(response, reverse('ledger:manual_transfer_create', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(
            response,
            reverse(
                'ledger:manual_transfer_update',
                kwargs={'pk': self.obligation.pk, 'event_pk': advance.pk},
            ),
        )

        history_response = self.client.get(reverse('ledger:obligation_history', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(history_response.status_code, 200)
        self.assertTrue(history_response.context['admin_preview'])
        self.assertContains(history_response, 'Debt increase')
        self.assertNotContains(history_response, 'You lent')

    def test_staff_preview_shows_restore_and_delete_only_after_close(self):
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2026, 2, 1)
        self.obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['admin_preview'])
        self.assertContains(response, reverse('ledger:admin_obligation_restore', kwargs={'pk': self.obligation.pk}))
        self.assertContains(response, reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}))
        self.assertContains(response, f'OPEN {self.obligation.pk}')
        self.assertContains(response, f'DELETE {self.obligation.pk}')
        self.assertNotContains(response, reverse('ledger:admin_obligation_close', kwargs={'pk': self.obligation.pk}))

    @override_settings(STORAGES=TEST_STORAGES)
    def test_django_admin_obligation_form_hides_internal_ledger_fields(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        admin_user = get_user_model().objects.create_superuser(username='admin', password='admin-pass')
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin:ledger_obligation_change', kwargs={'object_id': self.obligation.pk}))

        self.assertContains(response, 'Creditor is the person who is owed money.')
        self.assertContains(response, 'People')
        self.assertContains(response, 'Obligation')
        self.assertNotContains(response, 'Ledger accounts')
        self.assertNotContains(response, 'Add another Ledger account')
        self.assertNotContains(response, 'Currency exponent')
        self.assertNotContains(response, 'name="currency"')

    @override_settings(STORAGES=TEST_STORAGES)
    def test_django_admin_ledger_index_hides_internal_accounting_models(self):
        admin_user = get_user_model().objects.create_superuser(username='admin', password='admin-pass')
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin:app_list', kwargs={'app_label': 'ledger'}))

        self.assertContains(response, 'Obligations')
        self.assertContains(response, 'Interest rate periods')
        self.assertNotContains(response, 'Ledger accounts')
        self.assertNotContains(response, 'Ledger entries')
        self.assertNotContains(response, 'Ledger transactions')
        self.assertNotContains(response, 'Financial events')
        self.assertNotContains(response, 'Interest accrual runs')
        self.assertNotContains(response, 'Event series versions')
        self.assertNotContains(response, 'Audit events')

    def test_admin_obligations_page_is_staff_only(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:admin_obligations'))

        self.assertEqual(response.status_code, 403)

    def test_admin_obligation_restore_requires_confirmation_and_reopens(self):
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2026, 2, 1)
        self.obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        wrong_response = self.client.post(
            reverse('ledger:admin_obligation_restore', kwargs={'pk': self.obligation.pk}),
            {'restore_confirmation': 'OPEN wrong'},
        )

        self.assertRedirects(wrong_response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.CLOSED)

        response = self.client.post(
            reverse('ledger:admin_obligation_restore', kwargs={'pk': self.obligation.pk}),
            {'restore_confirmation': f'OPEN {self.obligation.pk}'},
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.OPEN)
        self.assertIsNone(self.obligation.closed_on)

    def test_admin_obligation_close_requires_confirmation_and_stops_tracking(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        wrong_response = self.client.post(
            reverse('ledger:admin_obligation_close', kwargs={'pk': self.obligation.pk}),
            {'close_confirmation': 'CLOSE wrong'},
        )

        self.assertRedirects(wrong_response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.OPEN)

        response = self.client.post(
            reverse('ledger:admin_obligation_close', kwargs={'pk': self.obligation.pk}),
            {'close_confirmation': f'CLOSE {self.obligation.pk}'},
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        series.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.CLOSED)
        self.assertEqual(self.obligation.closed_on, timezone.localdate())
        self.assertFalse(series.active)
        self.assertEqual(series.ends_on, timezone.localdate())

    def test_admin_obligation_delete_requires_confirmation_and_removes_history(self):
        transaction = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        )
        event_id = transaction.financial_event_id
        transaction_id = transaction.pk
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2026, 2, 1)
        self.obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        wrong_response = self.client.post(
            reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}),
            {'delete_confirmation': 'DELETE wrong'},
        )

        self.assertRedirects(wrong_response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertTrue(Obligation.objects.filter(pk=self.obligation.pk).exists())

        response = self.client.post(
            reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}),
            {'delete_confirmation': f'DELETE {self.obligation.pk}'},
        )

        self.assertRedirects(response, reverse('ledger:admin_obligations'))
        self.assertFalse(Obligation.objects.filter(pk=self.obligation.pk).exists())
        self.assertFalse(FinancialEvent.objects.filter(pk=event_id).exists())
        self.assertFalse(LedgerTransaction.objects.filter(pk=transaction_id).exists())

    def test_admin_obligation_delete_rejects_open_obligation(self):
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.post(
            reverse('ledger:admin_obligation_delete', kwargs={'pk': self.obligation.pk}),
            {'delete_confirmation': f'DELETE {self.obligation.pk}'},
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertTrue(Obligation.objects.filter(pk=self.obligation.pk).exists())

    def test_admin_users_page_lists_users_with_profile_status_and_actions(self):
        profile = UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_username='andrii_t',
            telegram_first_name='Andrii',
            telegram_last_name='Turkevych',
            telegram_checked_at=timezone.now(),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:admin_users'))

        self.assertContains(response, 'User directory')
        self.assertContains(response, self.creditor_user.username)
        self.assertContains(response, self.borrower_user.username)
        self.assertContains(response, 'Andrii Turkevych (@andrii_t)')
        self.assertContains(response, 'Verified')
        self.assertContains(response, 'Joined')
        self.assertEqual(UserProfile.objects.filter(user=self.creditor_user).count(), 1)
        self.assertContains(response, reverse('ledger:admin_user_update', kwargs={'pk': self.borrower_user.pk}))
        self.assertContains(response, reverse('ledger:admin_profile_update', kwargs={'pk': profile.pk}))
        self.assertNotContains(response, reverse('ledger:admin_user_reset_password', kwargs={'pk': self.borrower_user.pk}))

    def test_admin_user_edit_requires_confirmation(self):
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.post(
            reverse('ledger:admin_user_update', kwargs={'pk': self.borrower_user.pk}),
            {
                'username': self.borrower_user.username,
                'first_name': 'Updated',
                'last_name': 'Borrower',
                'email': 'updated@example.com',
                'is_active': 'on',
                'confirm_username': 'wrong',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Type the current username')
        self.borrower_user.refresh_from_db()
        self.assertNotEqual(self.borrower_user.first_name, 'Updated')

    def test_admin_user_edit_saves_after_confirmation(self):
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.post(
            reverse('ledger:admin_user_update', kwargs={'pk': self.borrower_user.pk}),
            {
                'username': self.borrower_user.username,
                'first_name': 'Updated',
                'last_name': 'Borrower',
                'email': 'updated@example.com',
                'is_active': 'on',
                'is_staff': 'on',
                'confirm_username': self.borrower_user.username,
            },
        )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        self.borrower_user.refresh_from_db()
        self.assertEqual(self.borrower_user.get_full_name(), 'Updated Borrower')
        self.assertEqual(self.borrower_user.email, 'updated@example.com')
        self.assertTrue(self.borrower_user.is_staff)

    def test_admin_user_edit_does_not_allow_self_lockout(self):
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.post(
            reverse('ledger:admin_user_update', kwargs={'pk': staff_user.pk}),
            {
                'username': staff_user.username,
                'first_name': '',
                'last_name': '',
                'email': '',
                'confirm_username': staff_user.username,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You cannot deactivate your own account.')
        self.assertContains(response, 'You cannot remove your own admin access.')
        staff_user.refresh_from_db()
        self.assertTrue(staff_user.is_active)
        self.assertTrue(staff_user.is_staff)

    def test_admin_user_edit_shows_telegram_password_reset_dialog_when_ready(self):
        UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_username='andrii_t',
            telegram_checked_at=timezone.now(),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:admin_user_update', kwargs={'pk': self.borrower_user.pk}))

        self.assertContains(response, 'Reset password')
        self.assertContains(response, f'password-reset-{self.borrower_user.pk}')
        self.assertContains(response, reverse('ledger:admin_user_reset_password', kwargs={'pk': self.borrower_user.pk}))
        self.assertContains(response, 'name="next" value="ledger:admin_user_update"')
        self.assertContains(response, 'Type "andrii" to generate a temporary password')

    def test_admin_user_password_reset_requires_confirmed_username(self):
        target_user = get_user_model().objects.create_user(username='reset_target', password='OldPass123!')
        UserProfile.objects.create(
            user=target_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_checked_at=timezone.now(),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        with patch('ledger.views.send_telegram_message') as send_message:
            response = self.client.post(
                reverse('ledger:admin_user_reset_password', kwargs={'pk': target_user.pk}),
                {'reset_confirm_username': 'wrong'},
            )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        send_message.assert_not_called()
        target_user.refresh_from_db()
        self.assertTrue(target_user.check_password('OldPass123!'))

    def test_admin_user_password_reset_requires_verified_active_private_telegram(self):
        inactive_user = get_user_model().objects.create_user(
            username='inactive_reset',
            password='OldPass123!',
            is_active=False,
        )
        UserProfile.objects.create(
            user=inactive_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_checked_at=timezone.now(),
        )
        unverified_user = get_user_model().objects.create_user(username='unverified_reset', password='OldPass123!')
        UserProfile.objects.create(user=unverified_user, telegram_id=777)
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        with patch('ledger.views.send_telegram_message') as send_message:
            inactive_response = self.client.post(
                reverse('ledger:admin_user_reset_password', kwargs={'pk': inactive_user.pk}),
                {'reset_confirm_username': inactive_user.username},
            )
            unverified_response = self.client.post(
                reverse('ledger:admin_user_reset_password', kwargs={'pk': unverified_user.pk}),
                {'reset_confirm_username': unverified_user.username},
            )

        self.assertRedirects(inactive_response, reverse('ledger:admin_users'))
        self.assertRedirects(unverified_response, reverse('ledger:admin_users'))
        send_message.assert_not_called()
        inactive_user.refresh_from_db()
        unverified_user.refresh_from_db()
        self.assertTrue(inactive_user.check_password('OldPass123!'))
        self.assertTrue(unverified_user.check_password('OldPass123!'))

    def test_admin_user_password_reset_rolls_back_when_telegram_fails(self):
        target_user = get_user_model().objects.create_user(username='reset_target', password='OldPass123!')
        UserProfile.objects.create(
            user=target_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_checked_at=timezone.now(),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        with patch('ledger.views.send_telegram_message', side_effect=TelegramLookupError('Telegram failed.')):
            response = self.client.post(
                reverse('ledger:admin_user_reset_password', kwargs={'pk': target_user.pk}),
                {'reset_confirm_username': target_user.username},
            )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        target_user.refresh_from_db()
        self.assertTrue(target_user.check_password('OldPass123!'))

    def test_admin_user_password_reset_sends_temporary_password_and_changes_password(self):
        target_user = get_user_model().objects.create_user(username='reset_target', password='OldPass123!')
        UserProfile.objects.create(
            user=target_user,
            telegram_id=555,
            telegram_chat_type='private',
            telegram_checked_at=timezone.now(),
        )
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        with patch('ledger.views.send_telegram_message', return_value={'message_id': 123}) as send_message:
            response = self.client.post(
                reverse('ledger:admin_user_reset_password', kwargs={'pk': target_user.pk}),
                {'reset_confirm_username': target_user.username},
            )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        send_message.assert_called_once()
        chat_id, message_text = send_message.call_args.args
        temporary_password = message_text.split('Temporary password: ', 1)[1].splitlines()[0]
        self.assertEqual(chat_id, 555)
        self.assertIn('TrustTrack password reset', message_text)
        target_user.refresh_from_db()
        self.assertFalse(target_user.check_password('OldPass123!'))
        self.assertTrue(target_user.check_password(temporary_password))

    def test_admin_panel_links_to_combined_users_and_categories_interfaces(self):
        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        response = self.client.get(reverse('ledger:admin_panel'))

        self.assertContains(response, reverse('ledger:admin_users'))
        self.assertContains(response, reverse('ledger:admin_categories'))
        self.assertNotContains(response, reverse('ledger:admin_profiles'))
        self.assertContains(response, 'Accounts, Telegram, activity')
        self.assertContains(response, 'Obligation category interface')

    def test_admin_categories_page_lists_categories_and_links_to_edit(self):
        category = ObligationCategory.objects.create(name='Rent')
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        response = self.client.get(reverse('ledger:admin_categories'))

        self.assertContains(response, 'Category list')
        self.assertContains(response, 'Rent')
        self.assertContains(response, reverse('ledger:admin_category_create'))
        self.assertContains(response, reverse('ledger:admin_category_update', kwargs={'pk': category.pk}))

    def test_admin_category_create_and_update(self):
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        create_response = self.client.post(
            reverse('ledger:admin_category_create'),
            {
                'name': 'Utilities',
                'active': 'on',
            },
        )

        self.assertRedirects(create_response, reverse('ledger:admin_categories'))
        category = ObligationCategory.objects.get(name='Utilities')
        self.assertTrue(category.active)

        update_response = self.client.post(
            reverse('ledger:admin_category_update', kwargs={'pk': category.pk}),
            {
                'name': 'Utilities updated',
            },
        )

        self.assertRedirects(update_response, reverse('ledger:admin_categories'))
        category.refresh_from_db()
        self.assertEqual(category.name, 'Utilities updated')
        self.assertFalse(category.active)

    def test_admin_profiles_page_redirects_to_combined_users_page(self):
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        response = self.client.get(reverse('ledger:admin_profiles'))

        self.assertRedirects(response, reverse('ledger:admin_users'))

    def test_admin_profile_edit_updates_telegram_without_changing_modules(self):
        profile = UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            telegram_username='old_user',
            telegram_checked_at=timezone.now(),
            show_planner_module=True,
            show_dashboard_balance_history=False,
        )
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        response = self.client.post(
            reverse('ledger:admin_profile_update', kwargs={'pk': profile.pk}),
            {
                'telegram_id': 777,
                'telegram_submit': '1',
            },
        )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        profile.refresh_from_db()
        self.assertEqual(profile.telegram_id, 777)
        self.assertEqual(profile.telegram_username, '')
        self.assertIsNone(profile.telegram_checked_at)
        self.assertTrue(profile.show_planner_module)
        self.assertFalse(profile.show_dashboard_balance_history)
        self.assertTrue(profile.payment_due_notifications)

    def test_admin_profile_edit_updates_modules_without_changing_telegram(self):
        profile = UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            telegram_username='old_user',
            telegram_checked_at=timezone.now(),
            show_planner_module=True,
            show_dashboard_balance_history=False,
        )
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        response = self.client.post(
            reverse('ledger:admin_profile_update', kwargs={'pk': profile.pk}),
            {
                'show_dashboard_balance_history': 'on',
                'modules_submit': '1',
            },
        )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        profile.refresh_from_db()
        self.assertEqual(profile.telegram_id, 555)
        self.assertEqual(profile.telegram_username, 'old_user')
        self.assertIsNotNone(profile.telegram_checked_at)
        self.assertFalse(profile.show_planner_module)
        self.assertTrue(profile.show_dashboard_balance_history)
        self.assertFalse(profile.payment_due_notifications)

    def test_admin_profile_edit_hides_telegram_id_until_change(self):
        profile = UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=555,
            show_planner_module=True,
        )
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        response = self.client.get(reverse('ledger:admin_profile_update', kwargs={'pk': profile.pk}))

        self.assertContains(response, 'data-edit-target="admin-telegram-form"')
        self.assertContains(
            response,
            '<form id="admin-telegram-form" class="form-grid setting-edit" method="post" hidden>',
        )
        self.assertContains(response, 'data-field-name="telegram_id"')
        self.assertContains(response, 'id="modules-panel"')
        self.assertContains(response, 'data-field-name="show_planner_module"')
        self.assertContains(response, 'data-field-name="payment_due_notifications"')
        self.assertContains(response, 'Planner')
        content = response.content.decode()
        self.assertLess(content.index('data-field-name="telegram_id"'), content.index('id="modules-panel"'))

    def test_admin_profile_check_telegram_refreshes_identity(self):
        profile = UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        with patch(
            'ledger.views.get_telegram_chat_identity',
            return_value=TelegramChatIdentity(
                chat_id=555,
                chat_type='private',
                username='andrii_t',
                first_name='Andrii',
                last_name='Turkevych',
            ),
        ):
            response = self.client.post(
                reverse('ledger:admin_profile_check_telegram', kwargs={'pk': profile.pk}),
                {'next': 'ledger:admin_users'},
            )

        self.assertRedirects(response, reverse('ledger:admin_users'))
        profile.refresh_from_db()
        self.assertEqual(profile.telegram_chat_type, 'private')
        self.assertEqual(profile.telegram_username, 'andrii_t')
        self.assertEqual(profile.telegram_display_name, 'Andrii Turkevych (@andrii_t)')
        self.assertEqual(profile.telegram_lookup_error, '')
        self.assertIsNotNone(profile.telegram_checked_at)

    def test_admin_profile_check_telegram_records_errors(self):
        profile = UserProfile.objects.create(user=self.borrower_user, telegram_id=555)
        self.client.force_login(get_user_model().objects.create_user(username='staff', is_staff=True))

        with patch('ledger.views.get_telegram_chat_identity', side_effect=TelegramLookupError('Telegram failed.')):
            response = self.client.post(
                reverse('ledger:admin_profile_check_telegram', kwargs={'pk': profile.pk}),
                {'next': 'ledger:admin_profile_update'},
            )

        self.assertRedirects(response, reverse('ledger:admin_profile_update', kwargs={'pk': profile.pk}))
        profile.refresh_from_db()
        self.assertEqual(profile.telegram_lookup_error, 'Telegram failed.')
        self.assertIsNotNone(profile.telegram_checked_at)

    def test_nav_can_hide_planner_from_profile_preferences(self):
        UserProfile.objects.create(user=self.borrower_user, show_planner_module=False)
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:dashboard'))

        self.assertContains(response, reverse('ledger:dashboard'))
        self.assertNotContains(response, reverse('ledger:planner'))
        self.assertNotContains(response, 'Planner')

    def test_dashboard_shows_actual_month_end_balance_history(self):
        post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        )
        post_repayment(
            self.obligation,
            amount_units=250_000,
            event_date=date(2026, 2, 10),
        )
        self.client.force_login(self.borrower_user)

        with patch('ledger.services.history.timezone.localdate', return_value=date(2026, 6, 17)):
            response = self.client.get(reverse('ledger:dashboard'))

        payload = response.context['balance_history_chart_payload']
        self.assertContains(response, 'Balance history')
        self.assertContains(response, 'balance-history-chart-data')
        self.assertEqual(payload['labels'][0], 'Jun 2025')
        self.assertEqual(payload['labels'][-1], 'May 2026')
        january_index = payload['labels'].index('Jan 2026')
        february_index = payload['labels'].index('Feb 2026')
        self.assertEqual(payload['iOweValues'][january_index], 100.0)
        self.assertEqual(payload['values'][january_index], -100.0)
        self.assertEqual(payload['iOweValues'][february_index], 75.0)
        self.assertEqual(payload['values'][february_index], -75.0)
        self.assertEqual(response.context['balance_history']['latest_net_units'], -750_000)
        self.assertEqual(response.context['balance_history_latest_net_class'], 'negative')

    def test_dashboard_can_hide_balance_history_from_profile_preferences(self):
        UserProfile.objects.create(user=self.borrower_user, show_dashboard_balance_history=False)
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:dashboard'))

        self.assertFalse(response.context['show_balance_history'])
        self.assertNotContains(response, 'Balance history')
        self.assertNotContains(response, 'balance-history-chart-data')
        self.assertNotContains(response, 'chart.umd.min.js')

    def test_profile_page_shows_collapsed_setting_sections(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:profile'))

        content = response.content.decode()
        self.assertLess(content.index('id="telegram-panel"'), content.index('id="password-panel"'))
        self.assertLess(content.index('id="modules-panel"'), content.index('id="password-panel"'))
        self.assertContains(response, 'Telegram ID')
        self.assertContains(response, 'Modules')
        self.assertContains(response, 'Dashboard balance history')
        self.assertContains(response, 'Telegram balance notifications')
        self.assertContains(response, 'Not connected')
        self.assertContains(response, 'id="password-form" class="form-grid setting-edit" method="post" hidden')
        self.assertContains(response, 'data-edit-target="password-form"')
        self.assertContains(response, 'min="1"')

    def test_profile_page_updates_module_preferences(self):
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:profile'),
            {
                'show_dashboard_balance_history': 'on',
                'modules_submit': '1',
            },
        )

        self.assertRedirects(response, reverse('ledger:profile'))
        profile = UserProfile.objects.get(user=self.borrower_user)
        self.assertFalse(profile.show_planner_module)
        self.assertTrue(profile.show_dashboard_balance_history)
        self.assertFalse(profile.payment_due_notifications)

    def test_profile_page_updates_telegram_id(self):
        self.client.force_login(self.borrower_user)

        with patch(
            'ledger.views.get_telegram_chat_identity',
            return_value=TelegramChatIdentity(
                chat_id=123456789,
                chat_type='private',
                username='andrii_t',
                first_name='Andrii',
                last_name='Turkevych',
            ),
        ):
            response = self.client.post(
                reverse('ledger:profile'),
                {
                    'telegram_id': '123456789',
                    'profile_submit': '1',
                },
            )

        self.assertRedirects(response, reverse('ledger:profile'))
        profile = UserProfile.objects.get(user=self.borrower_user)
        self.assertEqual(profile.telegram_id, 123456789)
        self.assertEqual(profile.telegram_chat_type, 'private')
        self.assertEqual(profile.telegram_username, 'andrii_t')
        self.assertEqual(profile.telegram_display_name, 'Andrii Turkevych (@andrii_t)')
        self.assertIsNotNone(profile.telegram_checked_at)

    def test_profile_page_refreshes_existing_telegram_identity(self):
        UserProfile.objects.create(user=self.borrower_user, telegram_id=123456789)
        self.client.force_login(self.borrower_user)

        with patch(
            'ledger.views.get_telegram_chat_identity',
            return_value=TelegramChatIdentity(
                chat_id=123456789,
                chat_type='private',
                username='family_user',
                first_name='Family',
            ),
        ):
            response = self.client.get(reverse('ledger:profile'))

        self.assertContains(response, 'Family (@family_user)')
        self.assertContains(response, 'private')

    def test_profile_page_can_clear_telegram_id(self):
        UserProfile.objects.create(
            user=self.borrower_user,
            telegram_id=123456789,
            telegram_username='andrii_t',
            telegram_chat_type='private',
            telegram_lookup_error='old error',
            telegram_checked_at=timezone.now(),
        )
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:profile'),
            {
                'telegram_id': '',
                'profile_submit': '1',
            },
        )

        self.assertRedirects(response, reverse('ledger:profile'))
        profile = UserProfile.objects.get(user=self.borrower_user)
        self.assertIsNone(profile.telegram_id)
        self.assertEqual(profile.telegram_username, '')
        self.assertEqual(profile.telegram_chat_type, '')
        self.assertEqual(profile.telegram_lookup_error, '')
        self.assertIsNone(profile.telegram_checked_at)

    def test_profile_page_rejects_duplicate_telegram_id(self):
        other_user = get_user_model().objects.create_user(username='maria')
        UserProfile.objects.create(user=other_user, telegram_id=123456789)
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:profile'),
            {
                'telegram_id': '123456789',
                'profile_submit': '1',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertContains(response, 'id="telegram-form" class="form-grid setting-edit" method="post"')
        profile = UserProfile.objects.get(user=self.borrower_user)
        self.assertIsNone(profile.telegram_id)

    def test_profile_page_changes_password_and_keeps_user_logged_in(self):
        self.borrower_user.set_password('old-family-pass-2026')
        self.borrower_user.save(update_fields=['password'])
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:profile'),
            {
                'old_password': 'old-family-pass-2026',
                'new_password1': 'S0lid-family-pass-2026',
                'new_password2': 'S0lid-family-pass-2026',
                'password_submit': '1',
            },
        )

        self.assertRedirects(response, reverse('ledger:profile'))
        self.borrower_user.refresh_from_db()
        self.assertTrue(self.borrower_user.check_password('S0lid-family-pass-2026'))
        self.assertEqual(self.client.get(reverse('ledger:profile')).status_code, 200)

    def test_profile_page_keeps_password_form_open_after_password_error(self):
        self.borrower_user.set_password('old-family-pass-2026')
        self.borrower_user.save(update_fields=['password'])
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:profile'),
            {
                'old_password': 'wrong-password',
                'new_password1': 'S0lid-family-pass-2026',
                'new_password2': 'S0lid-family-pass-2026',
                'password_submit': '1',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="password-form" class="form-grid setting-edit" method="post"')
        self.assertNotContains(response, 'id="password-form" class="form-grid setting-edit" method="post" hidden')

    def test_planner_page_shows_active_obligations_and_projection(self):
        post_principal_advance(self.obligation, amount_units=3_000_000, event_date=date(2026, 1, 1))
        self.client.force_login(self.borrower_user)

        with patch('ledger.services.planner.timezone.localdate', return_value=date(2026, 1, 1)):
            response = self.client.get(reverse('ledger:planner'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Net projection')
        self.assertContains(response, 'Payment simulator')
        self.assertContains(response, 'Test loan')
        self.assertEqual(response.context['portfolio_projection']['current_i_owe_units'], 3_000_000)
        self.assertEqual(response.context['chart_payload']['labels'][0], 'Jan 2026')
        self.assertEqual(response.context['chart_payload']['values'][0], -300.0)

    def test_planner_simulator_calculates_monthly_payment_result(self):
        post_principal_advance(self.obligation, amount_units=3_000_000, event_date=date(2026, 1, 1))
        self.client.force_login(self.borrower_user)

        with patch('ledger.services.planner.timezone.localdate', return_value=date(2026, 1, 1)):
            response = self.client.get(
                reverse('ledger:planner'),
                {
                    'projection_months': '12',
                    'simulate': '1',
                    'obligation': self.obligation.pk,
                    'monthly_payment': '100.00',
                    'payment_day': '2',
                    'simulation_months': '12',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['simulator_result']['payoff_date'], date(2026, 3, 2))
        self.assertContains(response, '$0.00')

    def test_open_obligation_detail_keeps_maintenance_actions_in_settings_menu(self):
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertContains(response, 'Manual transfer')
        self.assertContains(response, 'Obligation settings')
        self.assertContains(response, 'Recalculate balance &amp; interest')
        self.assertContains(response, 'data-recalculate-form')
        self.assertContains(response, 'Recalculating...')
        self.assertContains(response, 'data-stop-tracking-form')
        self.assertContains(response, 'name="stop_tracking_confirmation"')
        self.assertNotContains(response, 'Recalculate recurring events')
        self.assertNotContains(response, 'Generate due recurring events')
        self.assertNotContains(response, 'Generate due interest')

    def test_staff_participant_still_sees_stop_tracking(self):
        self.creditor_user.is_staff = True
        self.creditor_user.save(update_fields=['is_staff'])
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stop tracking', count=1)
        self.assertContains(response, 'data-stop-tracking-form')
        self.assertContains(response, 'name="stop_tracking_confirmation"')
        self.assertNotContains(response, reverse('ledger:admin_obligation_close', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, reverse('ledger:admin_obligations'))
        self.assertNotContains(response, reverse('admin:ledger_obligation_change', kwargs={'object_id': self.obligation.pk}))

    def test_obligation_detail_shows_manual_transfers_separately(self):
        advance = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
            memo='Initial transfer',
            category='Home',
        ).financial_event
        post_repayment(
            self.obligation,
            amount_units=250_000,
            event_date=date(2026, 1, 5),
            memo='Partial repayment',
        )
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 2, 1),
        )
        version = EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=100_000,
            valid_from=date(2026, 2, 1),
        )
        post_scheduled_charge(
            self.obligation,
            amount_units=100_000,
            event_date=date(2026, 2, 1),
            event_series=series,
            event_series_version=version,
        )
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertContains(response, 'Manual transfers')
        self.assertEqual(len(response.context['manual_transfer_rows']), 2)
        self.assertContains(response, 'Initial transfer')
        self.assertContains(response, 'Partial repayment')
        self.assertContains(response, 'You borrowed')
        self.assertContains(response, 'You paid')
        self.assertContains(
            response,
            reverse('ledger:manual_transfer_update', kwargs={'pk': self.obligation.pk, 'event_pk': advance.pk}),
        )

    def test_obligation_detail_shows_financial_summary_totals(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('12.0000'),
            effective_from=date(2026, 1, 1),
        )
        interest_run = post_monthly_interest(self.obligation, date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=250_000, event_date=date(2026, 2, 2))
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        summary = response.context['obligation_summary']
        self.assertEqual(summary['paid_to_date_units'], 250_000)
        self.assertEqual(summary['principal_charges_units'], 1_000_000)
        self.assertEqual(summary['interest_accrued_units'], interest_run.calculated_interest_amount_units)
        self.assertContains(response, 'Paid to date')
        self.assertContains(response, '$25.00')
        self.assertContains(response, 'Principal/charges')
        self.assertContains(response, '$100.00')
        self.assertContains(response, 'Interest accrued')
        self.assertContains(response, '$0.95')
        self.assertNotContains(response, 'Paid progress')

    def test_manual_transfer_edit_reverses_original_and_posts_replacement(self):
        original = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
            memo='Old transfer',
            category='Old',
        ).financial_event
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:manual_transfer_update', kwargs={'pk': self.obligation.pk, 'event_pk': original.pk}),
            {
                'transfer_type': FinancialEvent.EventType.PRINCIPAL_ADVANCE,
                'event_date': '2026-01-03',
                'amount': '150.00',
                'category': 'New',
                'memo': 'New transfer',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        original.refresh_from_db()
        self.assertIsNotNone(original.voided_at)
        replacement = FinancialEvent.objects.get(
            obligation=self.obligation,
            source=FinancialEvent.Source.MANUAL,
            voided_at__isnull=True,
            memo='New transfer',
        )
        self.assertEqual(replacement.event_type, FinancialEvent.EventType.PRINCIPAL_ADVANCE)
        self.assertEqual(replacement.event_date, date(2026, 1, 3))
        self.assertEqual(replacement.amount_units, 1_500_000)
        self.assertEqual(replacement.category, 'New')
        reversal = FinancialEvent.objects.get(
            obligation=self.obligation,
            event_type=FinancialEvent.EventType.ADJUSTMENT,
            category='manual_transfer_reversal',
        )
        self.assertEqual(reversal.amount_units, 1_000_000)
        self.assertEqual(reversal.direction, FinancialEvent.Direction.DECREASES_DEBT)
        self.assertEqual(get_obligation_balance(self.obligation), 1_500_000)

    def test_manual_transfer_edit_rolls_back_when_replacement_is_invalid(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        original = post_repayment(
            self.obligation,
            amount_units=200_000,
            event_date=date(2026, 1, 5),
            memo='Small repayment',
        ).financial_event
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:manual_transfer_update', kwargs={'pk': self.obligation.pk, 'event_pk': original.pk}),
            {
                'transfer_type': FinancialEvent.EventType.REPAYMENT,
                'event_date': '2026-01-05',
                'amount': '125.00',
                'category': '',
                'memo': 'Too much',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Repayment cannot exceed')
        original.refresh_from_db()
        self.assertIsNone(original.voided_at)
        self.assertFalse(
            FinancialEvent.objects.filter(
                obligation=self.obligation,
                category='manual_transfer_reversal',
            ).exists()
        )
        self.assertEqual(get_obligation_balance(self.obligation), 800_000)

    def test_manual_transfer_create_form_uses_role_specific_action_labels(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:manual_transfer_create', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Record manual transfer: Test loan')
        self.assertContains(response, 'Repayment')
        self.assertContains(response, 'I paid money back')
        self.assertContains(response, 'Debt increase')
        self.assertContains(response, 'I borrowed more money')
        self.assertContains(response, 'Record transfer')

    def test_manual_transfer_create_can_add_debt_increase(self):
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:manual_transfer_create', kwargs={'pk': self.obligation.pk}),
            {
                'transfer_type': FinancialEvent.EventType.PRINCIPAL_ADVANCE,
                'event_date': '2026-01-10',
                'amount': '75.00',
                'category': 'Cash',
                'memo': 'Borrowed more',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        event = FinancialEvent.objects.get(obligation=self.obligation, memo='Borrowed more')
        self.assertEqual(event.event_type, FinancialEvent.EventType.PRINCIPAL_ADVANCE)
        self.assertEqual(event.amount_units, 750_000)
        self.assertEqual(event.category, 'Cash')
        self.assertEqual(get_obligation_balance(self.obligation), 750_000)

    def test_generated_transfer_cannot_use_manual_transfer_edit_url(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 2, 1),
        )
        version = EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=100_000,
            valid_from=date(2026, 2, 1),
        )
        generated_event = post_scheduled_charge(
            self.obligation,
            amount_units=100_000,
            event_date=date(2026, 2, 1),
            event_series=series,
            event_series_version=version,
        ).financial_event
        self.client.force_login(self.creditor_user)

        response = self.client.get(
            reverse('ledger:manual_transfer_update', kwargs={'pk': self.obligation.pk, 'event_pk': generated_event.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_closed_obligation_rejects_manual_transfer_edit_url(self):
        transfer = post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        ).financial_event
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2026, 1, 2)
        self.obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        self.client.force_login(self.creditor_user)

        response = self.client.get(
            reverse('ledger:manual_transfer_update', kwargs={'pk': self.obligation.pk, 'event_pk': transfer.pk})
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        transfer.refresh_from_db()
        self.assertIsNone(transfer.voided_at)

    def test_obligation_detail_limits_recent_activity(self):
        for index in range(12):
            post_principal_advance(
                self.obligation,
                amount_units=10_000,
                event_date=date(2026, 1, index + 1),
                memo=f'Advance {index}',
            )
            InterestAccrualRun.objects.create(
                obligation=self.obligation,
                period_start=date(2026, 2, index + 1),
                period_end=date(2026, 2, index + 2),
                posted_on=date(2026, 2, index + 2),
                calculated_interest_amount_units=100,
            )
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(len(response.context['activity_rows']), 10)
        self.assertEqual(response.context['activity_total'], 12)
        self.assertTrue(response.context['activity_has_more'])
        self.assertContains(response, 'Recent activity')
        self.assertContains(response, 'View more')
        self.assertNotContains(response, 'Ledger entries')
        self.assertContains(response, reverse('ledger:obligation_history', kwargs={'pk': self.obligation.pk}))

    def test_activity_signs_are_from_current_user_perspective(self):
        post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
        )
        post_repayment(
            self.obligation,
            amount_units=100_000,
            event_date=date(2026, 1, 2),
        )

        self.client.force_login(self.borrower_user)
        borrower_response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        borrower_latest = borrower_response.context['activity_rows'][0]

        self.assertEqual(borrower_latest['label'], 'You paid')
        self.assertEqual(borrower_latest['signed_amount_units'], -100_000)
        self.assertEqual(borrower_latest['amount_class'], 'negative')
        self.assertContains(borrower_response, '-$10.00')

        self.client.force_login(self.creditor_user)
        creditor_response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        creditor_latest = creditor_response.context['activity_rows'][0]

        self.assertEqual(creditor_latest['label'], 'You received')
        self.assertEqual(creditor_latest['signed_amount_units'], 100_000)
        self.assertEqual(creditor_latest['amount_class'], 'positive')
        self.assertContains(creditor_response, '+$10.00')

    def test_obligation_history_shows_full_human_activity(self):
        for index in range(12):
            post_principal_advance(
                self.obligation,
                amount_units=10_000,
                event_date=date(2026, 1, index + 1),
                memo=f'Advance {index}',
            )
            InterestAccrualRun.objects.create(
                obligation=self.obligation,
                period_start=date(2026, 2, index + 1),
                period_end=date(2026, 2, index + 2),
                posted_on=date(2026, 2, index + 2),
                calculated_interest_amount_units=100,
            )
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_history', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(len(response.context['activity_rows']), 12)
        self.assertEqual(response.context['activity_total'], 12)
        self.assertContains(response, 'Activity history')
        self.assertContains(response, 'Accounting ledger')
        self.assertContains(response, reverse('ledger:obligation_accounting_history', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, 'Ledger entries')

    def test_accounting_history_keeps_full_double_entry_details(self):
        for index in range(12):
            post_principal_advance(
                self.obligation,
                amount_units=10_000,
                event_date=date(2026, 1, index + 1),
                memo=f'Advance {index}',
            )
            InterestAccrualRun.objects.create(
                obligation=self.obligation,
                period_start=date(2026, 2, index + 1),
                period_end=date(2026, 2, index + 2),
                posted_on=date(2026, 2, index + 2),
                calculated_interest_amount_units=100,
            )
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_accounting_history', kwargs={'pk': self.obligation.pk}))

        self.assertEqual(len(response.context['ledger_entries']), 24)
        self.assertEqual(response.context['ledger_entries_total'], 24)
        self.assertEqual(len(response.context['financial_events']), 12)
        self.assertEqual(response.context['financial_events_total'], 12)
        self.assertEqual(len(response.context['interest_runs']), 12)
        self.assertEqual(response.context['interest_runs_total'], 12)
        self.assertContains(response, 'accounting ledger')
        self.assertContains(response, 'Ledger entries')

    def test_closed_obligation_detail_hides_active_controls(self):
        self.obligation.status = Obligation.Status.CLOSED
        self.obligation.closed_on = date(2026, 1, 2)
        self.obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))

        self.assertContains(response, 'Closed')
        self.assertNotContains(response, 'Repayment')
        self.assertNotContains(response, reverse('ledger:recurring_charge_create', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, reverse('ledger:interest_rate_create', kwargs={'pk': self.obligation.pk}))
        self.assertNotContains(response, 'Recalculate balance &amp; interest')
        self.assertNotContains(response, 'Generate due recurring events')
        self.assertNotContains(response, 'Generate due interest')
        self.assertNotContains(response, 'Stop tracking')

    def test_create_obligation_form_uses_explicit_role_labels(self):
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_create'))

        self.assertContains(response, 'Your role')
        self.assertContains(response, '>Creditor</option>')
        self.assertContains(response, '>Borrower</option>')
        self.assertContains(response, 'Choose Creditor when the other person owes you.')
        self.assertNotContains(response, 'I lent money')
        self.assertNotContains(response, 'I borrowed money')

    def test_create_obligation_posts_initial_principal(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        category = ObligationCategory.objects.create(name='Equipment')
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:obligation_create'),
            {
                'role': 'lent',
                'counterparty': counterparty.pk,
                'title': 'Hardware',
                'category': category.pk,
                'opened_on': '2026-02-01',
                'amount': '100.00',
                'memo': 'Laptop',
            },
        )

        obligation = Obligation.objects.get(title='Hardware')
        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))
        self.assertEqual(obligation.creditor, self.creditor_user)
        self.assertEqual(obligation.borrower, counterparty)
        self.assertEqual(obligation.category, category)
        self.assertEqual(get_obligation_balance(obligation), 1_000_000)

        event = obligation.financial_events.get(event_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE)
        self.assertEqual(event.memo, 'Laptop')
        self.assertEqual(event.category, 'Equipment')
        self.assertFalse(obligation.event_series.exists())
        self.assertFalse(obligation.interest_rate_periods.exists())

    def test_create_recurring_obligation_recalculates_due_events_after_create(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        self.client.force_login(self.creditor_user)

        with patch('ledger.services.recalculation.timezone.localdate', return_value=date(2026, 6, 17)):
            response = self.client.post(
                reverse('ledger:obligation_create'),
                {
                    'role': 'lent',
                    'counterparty': counterparty.pk,
                    'title': 'Monthly rent',
                    'category': '',
                    'payment_mode': 'recurring',
                    'opened_on': '2026-02-01',
                    'amount': '1000.00',
                    'recurring_frequency': EventSeries.Frequency.MONTHLY,
                    'recurring_day_of_month': '5',
                    'recurring_day_of_week': '',
                    'recurring_starts_on': '2026-03-05',
                    'recurring_ends_on': '',
                    'memo': 'Rent schedule',
                },
            )

        obligation = Obligation.objects.get(title='Monthly rent')
        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))
        self.assertEqual(get_obligation_balance(obligation), 50_000_000)

        series = obligation.event_series.get()
        version = series.versions.get()
        self.assertEqual(series.event_type, FinancialEvent.EventType.SCHEDULED_CHARGE)
        self.assertEqual(series.frequency, EventSeries.Frequency.MONTHLY)
        self.assertEqual(series.day_of_month, 5)
        self.assertEqual(series.starts_on, date(2026, 3, 5))
        self.assertEqual(version.amount_units, 10_000_000)
        self.assertEqual(version.valid_from, date(2026, 3, 5))
        self.assertEqual(
            FinancialEvent.objects.filter(
                obligation=obligation,
                source=FinancialEvent.Source.GENERATED,
                event_series=series,
                voided_at__isnull=True,
            ).count(),
            4,
        )

    def test_create_obligation_can_add_initial_interest_rate(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:obligation_create'),
            {
                'role': 'lent',
                'counterparty': counterparty.pk,
                'title': 'Interest loan',
                'category': '',
                'payment_mode': 'one_time',
                'opened_on': '2026-02-01',
                'amount': '100.00',
                'has_interest': 'on',
                'annual_rate_percent': '3.5',
                'memo': 'With rate',
            },
        )

        obligation = Obligation.objects.get(title='Interest loan')
        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))
        rate = obligation.interest_rate_periods.get()
        self.assertEqual(rate.annual_rate_percent, Decimal('3.5000'))
        self.assertEqual(rate.effective_from, date(2026, 2, 1))
        self.assertEqual(rate.memo, 'With rate')

    def test_create_obligation_sends_notification_after_recalculation(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria', first_name='Maria')
        UserProfile.objects.create(user=self.creditor_user, telegram_id=111, telegram_chat_type='private')
        UserProfile.objects.create(user=counterparty, telegram_id=222, telegram_chat_type='private')
        self.client.force_login(self.creditor_user)

        with (
            patch('ledger.services.recalculation.timezone.localdate', return_value=date(2026, 3, 15)),
            patch('ledger.services.notifications.send_telegram_message', return_value={'message_id': 1}) as send,
        ):
            response = self.client.post(
                reverse('ledger:obligation_create'),
                {
                    'role': 'lent',
                    'counterparty': counterparty.pk,
                    'title': 'Backdated rent',
                    'category': '',
                    'payment_mode': 'recurring',
                    'opened_on': '2026-01-01',
                    'amount': '100.00',
                    'recurring_frequency': EventSeries.Frequency.MONTHLY,
                    'recurring_day_of_month': '1',
                    'recurring_day_of_week': '',
                    'recurring_starts_on': '2026-01-01',
                    'recurring_ends_on': '',
                    'has_interest': 'on',
                    'annual_rate_percent': '12',
                    'memo': 'Created in the past',
                },
            )

        obligation = Obligation.objects.get(title='Backdated rent')
        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[0] for call in send.call_args_list}, {111, 222})
        message_text = send.call_args_list[0].args[1]
        self.assertIn('New TrustTrack obligation', message_text)
        self.assertIn('Backdated rent', message_text)
        self.assertIn('Borrower: Maria', message_text)
        self.assertIn('Creditor: Alex', message_text)
        self.assertIn('Initial amount: $100.00', message_text)
        self.assertIn('Generated scheduled: +$300.00', message_text)
        self.assertIn('Generated interest:', message_text)
        self.assertIn('Current balance:', message_text)

    def test_create_recurring_obligation_requires_matching_schedule_day(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:obligation_create'),
            {
                'role': 'lent',
                'counterparty': counterparty.pk,
                'title': 'Invalid recurring',
                'category': '',
                'payment_mode': 'recurring',
                'opened_on': '2026-02-01',
                'amount': '100.00',
                'recurring_frequency': EventSeries.Frequency.WEEKLY,
                'recurring_day_of_month': '',
                'recurring_day_of_week': '',
                'recurring_starts_on': '2026-02-01',
                'recurring_ends_on': '',
                'memo': '',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Day of week is required')
        self.assertFalse(Obligation.objects.filter(title='Invalid recurring').exists())

    def test_activity_uses_memo_for_details_and_limits_it_to_50_characters(self):
        long_memo = '123456789012345678901234567890123456789012345678901234567890'
        post_principal_advance(
            self.obligation,
            amount_units=1_000_000,
            event_date=date(2026, 1, 1),
            memo=long_memo,
            category='Equipment',
        )
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        row = response.context['activity_rows'][0]

        self.assertEqual(row['category'], 'Equipment')
        self.assertEqual(row['details'], '12345678901234567890123456789012345678901234567...')
        self.assertEqual(len(row['details']), 50)
        self.assertContains(response, 'Equipment')
        self.assertContains(response, row['details'])

    def test_create_obligation_counterparty_uses_full_name_before_username(self):
        user_model = get_user_model()
        user_model.objects.create_user(username='maria_login', first_name='Maria', last_name='Ivanova')
        user_model.objects.create_user(username='username_only')
        self.client.force_login(self.creditor_user)

        response = self.client.get(reverse('ledger:obligation_create'))

        self.assertContains(response, 'Maria Ivanova')
        self.assertContains(response, 'username_only')
        self.assertNotContains(response, 'maria_login')

    def test_repayment_form_rejects_overpayment(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:repayment_create', kwargs={'pk': self.obligation.pk}),
            {
                'event_date': '2026-01-10',
                'amount': '125.00',
                'memo': 'Too much',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Repayment cannot exceed')
        self.assertEqual(get_obligation_balance(self.obligation), 1_000_000)

    def test_recurring_charge_form_creates_series_and_version(self):
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:recurring_charge_create', kwargs={'pk': self.obligation.pk}),
            {
                'event_type': FinancialEvent.EventType.SCHEDULED_CHARGE,
                'name': 'Rent',
                'frequency': EventSeries.Frequency.MONTHLY,
                'day_of_month': 1,
                'day_of_week': '',
                'starts_on': '2026-03-01',
                'ends_on': '',
                'amount': '1000.00',
                'memo': 'Monthly rent',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        series = EventSeries.objects.get(obligation=self.obligation, name='Rent')
        version = series.versions.get()
        self.assertEqual(version.amount_units, 10_000_000)

    def test_recurring_repayment_form_creates_auto_payment_series(self):
        self.client.force_login(self.borrower_user)

        response = self.client.post(
            reverse('ledger:recurring_charge_create', kwargs={'pk': self.obligation.pk}),
            {
                'event_type': FinancialEvent.EventType.REPAYMENT,
                'name': 'Direct deposit',
                'frequency': EventSeries.Frequency.BIWEEKLY,
                'day_of_month': '',
                'day_of_week': 4,
                'starts_on': '2026-03-01',
                'ends_on': '',
                'amount': '50.00',
                'memo': 'Auto pay',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        series = EventSeries.objects.get(obligation=self.obligation, name='Direct deposit')
        self.assertEqual(series.event_type, FinancialEvent.EventType.REPAYMENT)
        self.assertEqual(series.frequency, EventSeries.Frequency.BIWEEKLY)
        self.assertEqual(series.day_of_week, 4)
        self.assertEqual(series.versions.get().amount_units, 500_000)

    def test_recurring_series_edit_updates_schedule_and_adds_future_amount_version(self):
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:recurring_series_update', kwargs={'pk': self.obligation.pk, 'series_pk': series.pk}),
            {
                'event_type': FinancialEvent.EventType.SCHEDULED_CHARGE,
                'name': 'Rent updated',
                'frequency': EventSeries.Frequency.MONTHLY,
                'day_of_month': 3,
                'day_of_week': '',
                'starts_on': '2026-01-01',
                'ends_on': '',
                'active': 'on',
                'memo': 'Updated schedule',
                'new_amount': '1100.00',
                'amount_valid_from': '2026-07-01',
                'version_memo': 'New rent',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        series.refresh_from_db()
        self.assertEqual(series.name, 'Rent updated')
        self.assertEqual(series.day_of_month, 3)
        versions = list(series.versions.order_by('valid_from'))
        self.assertEqual(versions[0].valid_to, date(2026, 6, 30))
        self.assertEqual(versions[1].amount_units, 11_000_000)

    def test_rate_form_creates_interest_rate_period(self):
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:interest_rate_create', kwargs={'pk': self.obligation.pk}),
            {
                'annual_rate_percent': '7.2500',
                'effective_from': '2026-01-01',
                'effective_to': '',
                'memo': 'Initial rate',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        rate = InterestRatePeriod.objects.get(obligation=self.obligation)
        self.assertEqual(rate.annual_rate_percent, Decimal('7.2500'))

    def test_rate_edit_updates_interest_rate_period(self):
        rate = InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('7.2500'),
            effective_from=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:interest_rate_update', kwargs={'pk': self.obligation.pk, 'rate_pk': rate.pk}),
            {
                'annual_rate_percent': '8.5000',
                'effective_from': '2026-02-01',
                'effective_to': '',
                'memo': 'Updated rate',
            },
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        rate.refresh_from_db()
        self.assertEqual(rate.annual_rate_percent, Decimal('8.5000'))
        self.assertEqual(rate.effective_from, date(2026, 2, 1))

    def test_generate_due_interest_view_posts_interest_runs(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(reverse('ledger:interest_due_generate', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertTrue(
            InterestAccrualRun.objects.filter(
                obligation=self.obligation,
                status=InterestAccrualRun.Status.POSTED,
            ).exists()
        )

    def test_recalculate_interest_view_voids_and_regenerates_interest(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )
        old_run = post_monthly_interest(self.obligation, date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=1_825_000, event_date=date(2026, 1, 16))
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:interest_recalculate', kwargs={'pk': self.obligation.pk}),
            {'from_date': '2026-01-16'},
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        old_run.refresh_from_db()
        self.assertEqual(old_run.status, InterestAccrualRun.Status.VOIDED)
        self.assertTrue(
            InterestAccrualRun.objects.filter(
                obligation=self.obligation,
                period_start=date(2026, 1, 1),
                revision=2,
                status=InterestAccrualRun.Status.POSTED,
            ).exists()
        )

    def test_obligation_recalculate_rebuilds_balance_before_interest(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Monthly charge',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        with patch('ledger.services.recalculation.timezone.localdate', return_value=date(2026, 2, 15)):
            response = self.client.post(reverse('ledger:obligation_recalculate', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertEqual(
            FinancialEvent.objects.filter(
                obligation=self.obligation,
                event_type=FinancialEvent.EventType.SCHEDULED_CHARGE,
                source=FinancialEvent.Source.GENERATED,
            ).count(),
            2,
        )
        january_interest = InterestAccrualRun.objects.get(
            obligation=self.obligation,
            period_start=date(2026, 1, 1),
            status=InterestAccrualRun.Status.POSTED,
        )
        self.assertEqual(january_interest.calculated_interest_amount_units, 15_948)

        transaction_count = LedgerTransaction.objects.count()
        financial_event_count = FinancialEvent.objects.count()
        interest_run_count = InterestAccrualRun.objects.count()

        with patch('ledger.services.recalculation.timezone.localdate', return_value=date(2026, 2, 15)):
            second_response = self.client.post(reverse('ledger:obligation_recalculate', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(second_response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertEqual(LedgerTransaction.objects.count(), transaction_count)
        self.assertEqual(FinancialEvent.objects.count(), financial_event_count)
        self.assertEqual(InterestAccrualRun.objects.count(), interest_run_count)

    def test_generate_due_charges_view_posts_due_recurring_events(self):
        current_month = timezone.localdate().replace(day=1)
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=current_month,
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=10_000_000,
            valid_from=current_month,
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(reverse('ledger:recurring_due_generate', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertEqual(get_obligation_balance(self.obligation), 10_000_000)

    def test_recalculate_recurring_view_reverses_no_longer_due_events(self):
        post_principal_advance(self.obligation, amount_units=100_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Weekly direct deposit',
            event_type=FinancialEvent.EventType.REPAYMENT,
            frequency=EventSeries.Frequency.WEEKLY,
            day_of_week=0,
            starts_on=date(2026, 1, 1),
        )
        EventSeriesVersion.objects.create(
            event_series=series,
            amount_units=1_000_000,
            valid_from=date(2026, 1, 1),
        )
        generate_due_recurring_events(obligation=self.obligation, through_date=date(2026, 5, 28))
        series.ends_on = date(2026, 3, 31)
        series.save(update_fields=['ends_on', 'updated_at'])
        self.client.force_login(self.borrower_user)

        with patch('ledger.services.recurring.timezone.localdate', return_value=date(2026, 5, 28)):
            response = self.client.post(
                reverse('ledger:recurring_recalculate', kwargs={'pk': self.obligation.pk}),
                {'from_date': '2026-04-01'},
            )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.assertTrue(
            FinancialEvent.objects.filter(
                obligation=self.obligation,
                event_series=series,
                source=FinancialEvent.Source.GENERATED,
                event_date__gt=date(2026, 3, 31),
                voided_at__isnull=False,
            ).exists()
        )

    def test_close_obligation_requires_stop_confirmation(self):
        self.client.force_login(self.creditor_user)

        response = self.client.post(reverse('ledger:obligation_close', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.OPEN)

    def test_close_obligation_view_stops_tracking_without_deleting_history(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:obligation_close', kwargs={'pk': self.obligation.pk}),
            {'stop_tracking_confirmation': 'STOP'},
        )

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        series.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.CLOSED)
        self.assertEqual(self.obligation.closed_on, timezone.localdate())
        self.assertFalse(series.active)
        self.assertEqual(series.ends_on, timezone.localdate())
        self.assertEqual(self.obligation.ledger_transactions.count(), 1)
