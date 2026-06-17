from datetime import date
from decimal import Decimal
from unittest.mock import patch

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
    ObligationCategory,
)
from ledger.services.balances import get_obligation_balance
from ledger.services.events import ensure_obligation_accounts, post_principal_advance, post_repayment
from ledger.services.interest import (
    calculate_monthly_interest,
    generate_due_interest,
    post_monthly_interest,
    recalculate_interest_from,
)
from ledger.services.planner import build_portfolio_projection, simulate_monthly_payment
from ledger.services.recurring import (
    generate_due_recurring_events,
    generate_recurring_events_for_month,
    recalculate_due_recurring_events,
)
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
        self.assertEqual([run.calculated_interest_amount_units for run in posted_runs], [23_000, 14_176])
        self.assertLess(get_obligation_balance(self.obligation), old_balance)


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

    def test_nav_hides_duplicate_new_obligation_and_limits_admin_link_to_staff(self):
        self.client.force_login(self.borrower_user)

        response = self.client.get(reverse('ledger:dashboard'))

        self.assertContains(response, 'New obligation', count=1)
        self.assertContains(response, reverse('ledger:planner'))
        self.assertNotContains(response, reverse('admin:index'))
        self.assertNotContains(response, 'Admin')

        staff_user = get_user_model().objects.create_user(username='staff', is_staff=True)
        self.client.force_login(staff_user)

        staff_response = self.client.get(reverse('ledger:dashboard'))

        self.assertContains(staff_response, 'New obligation', count=1)
        self.assertContains(staff_response, reverse('admin:index'))
        self.assertContains(staff_response, 'Admin')

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

        self.assertContains(response, 'Repayment')
        self.assertContains(response, 'Obligation settings')
        self.assertContains(response, 'Recalculate balance &amp; interest')
        self.assertContains(response, 'data-recalculate-form')
        self.assertContains(response, 'Recalculating...')
        self.assertContains(response, 'data-stop-tracking-form')
        self.assertContains(response, 'name="stop_tracking_confirmation"')
        self.assertNotContains(response, 'Recalculate recurring events')
        self.assertNotContains(response, 'Generate due recurring events')
        self.assertNotContains(response, 'Generate due interest')

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

    def test_create_recurring_obligation_creates_initial_principal_and_series(self):
        user_model = get_user_model()
        counterparty = user_model.objects.create_user(username='maria')
        self.client.force_login(self.creditor_user)

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
        self.assertEqual(get_obligation_balance(obligation), 10_000_000)

        series = obligation.event_series.get()
        version = series.versions.get()
        self.assertEqual(series.event_type, FinancialEvent.EventType.SCHEDULED_CHARGE)
        self.assertEqual(series.frequency, EventSeries.Frequency.MONTHLY)
        self.assertEqual(series.day_of_month, 5)
        self.assertEqual(series.starts_on, date(2026, 3, 5))
        self.assertEqual(version.amount_units, 10_000_000)
        self.assertEqual(version.valid_from, date(2026, 3, 5))

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
        self.assertEqual(january_interest.calculated_interest_amount_units, 16_986)

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
