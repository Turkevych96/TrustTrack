from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
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
)
from ledger.services.balances import get_obligation_balance
from ledger.services.events import ensure_obligation_accounts, post_principal_advance, post_repayment
from ledger.services.interest import (
    calculate_monthly_interest,
    generate_due_interest,
    post_monthly_interest,
    recalculate_interest_from,
)
from ledger.services.recurring import generate_due_recurring_events, generate_recurring_events_for_month
from ledger.templatetags.money import money_units


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


class InterestTests(LedgerTestCase):
    def test_interest_uses_apr_divided_by_365(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        calculation = calculate_monthly_interest(self.obligation, date(2026, 1, 1))

        self.assertEqual(calculation['amount_units'], 31_000)

    def test_repayment_changes_interest_base_from_repayment_date(self):
        post_principal_advance(self.obligation, amount_units=3_650_000, event_date=date(2026, 1, 1))
        post_repayment(self.obligation, amount_units=1_825_000, event_date=date(2026, 1, 16))
        InterestRatePeriod.objects.create(
            obligation=self.obligation,
            annual_rate_percent=Decimal('10.0000'),
            effective_from=date(2026, 1, 1),
        )

        calculation = calculate_monthly_interest(self.obligation, date(2026, 1, 1))

        self.assertEqual(calculation['amount_units'], 23_000)

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

        self.assertEqual(calculation['amount_units'], 47_000)

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

        self.assertEqual(run.calculated_interest_amount_units, 31_000)
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
        self.assertEqual([run.calculated_interest_amount_units for run in posted_runs], [23_000, 14_176])
        self.assertLess(get_obligation_balance(self.obligation), old_balance)


class ViewTests(LedgerTestCase):
    def test_anonymous_user_redirects_to_login(self):
        response = self.client.get(reverse('ledger:dashboard'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

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

    def test_create_obligation_posts_initial_principal(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        self.client.force_login(self.creditor_user)

        response = self.client.post(
            reverse('ledger:obligation_create'),
            {
                'role': 'lent',
                'counterparty': counterparty.pk,
                'title': 'Hardware',
                'category': 'Equipment',
                'opened_on': '2026-02-01',
                'amount': '100.00',
                'memo': 'Laptop',
            },
        )

        obligation = Obligation.objects.get(title='Hardware')
        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}))
        self.assertEqual(obligation.creditor, self.creditor_user)
        self.assertEqual(obligation.borrower, counterparty)
        self.assertEqual(get_obligation_balance(obligation), 1_000_000)

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
                'name': 'Rent',
                'day_of_month': 1,
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

    def test_close_obligation_view_stops_tracking_without_deleting_history(self):
        post_principal_advance(self.obligation, amount_units=1_000_000, event_date=date(2026, 1, 1))
        series = EventSeries.objects.create(
            obligation=self.obligation,
            name='Rent',
            day_of_month=1,
            starts_on=date(2026, 1, 1),
        )
        self.client.force_login(self.creditor_user)

        response = self.client.post(reverse('ledger:obligation_close', kwargs={'pk': self.obligation.pk}))

        self.assertRedirects(response, reverse('ledger:obligation_detail', kwargs={'pk': self.obligation.pk}))
        self.obligation.refresh_from_db()
        series.refresh_from_db()
        self.assertEqual(self.obligation.status, Obligation.Status.CLOSED)
        self.assertEqual(self.obligation.closed_on, timezone.localdate())
        self.assertFalse(series.active)
        self.assertEqual(series.ends_on, timezone.localdate())
        self.assertEqual(self.obligation.ledger_transactions.count(), 1)
