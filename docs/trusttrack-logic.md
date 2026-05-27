# TrustTrack Backend Schema and Logic Map

This document defines the first backend planning artifact for TrustTrack. It is intentionally written before models, migrations, and views so the backend can be built from a stable domain map.

Issue scope: [#1 Define domain schema and backend dependency map](https://github.com/Turkevych96/TrustTrack/issues/1).

## Product Direction

TrustTrack is a private, local Django app for tracking debts and recurring financial responsibilities between trusted people. It should stay simple: classic Django, SQLite, server-rendered pages, Django admin, and explicit financial logic covered by tests.

The system should answer five questions clearly:

- Who owes whom?
- What real-world event created or changed the debt?
- What scheduled events will happen again?
- What is the current balance?
- How were interest, monthly charges, and repayments calculated?

## Core Accounting Principle

TrustTrack should not store a manually edited "current balance" as the source of truth.

The current balance of an obligation is derived from posted ledger entries:

- entries that increase debt: principal advances, recurring charges, interest accruals, positive adjustments
- entries that reduce debt: repayments, credits, negative adjustments

Business events explain why ledger transactions exist. Ledger entries explain how balances changed.

Money is stored as integer units with 4 decimal places for v1:

```text
$10.0000 = 100000 units
1 unit = $0.0001
```

Percentages and interest calculations use `Decimal`; posted ledger amounts are rounded into integer units.

User-facing forms and pages display normal currency with 2 decimal places. The 4-decimal unit precision is a backend accounting detail only.

## Domain Entities

### User

Represents a trusted participant in the family ledger. In v1, participants are Django users directly; TrustTrack should not create debts for imaginary people.

Source:

- Django `AUTH_USER_MODEL`

Relationships:

- can be a creditor on an obligation
- can be a borrower on an obligation
- can appear in audit records as the actor

Rules:

- obligations and ledger accounts refer directly to real users
- users are created through Django admin in v1
- if future external contacts are needed, they should be modeled explicitly as non-user counterparties with constraints instead of being silently mixed with real users

### Obligation

Represents one debt account between one borrower and one creditor.

Examples:

- Andrii owes Alex for a cash loan
- Andrii owes Alex for shared rent
- Alex owes Andrii for a phone bill paid on Alex's behalf

Likely fields:

- creditor
- borrower
- title
- category
- currency, default `USD`
- opened_on
- closed_on, nullable
- status: open, closed, canceled
- notes
- timestamps

Rules:

- creditor and borrower must be different people
- balance is derived from ledger entries
- the initial amount is represented by a financial event and ledger entry, not by a separate mutable balance field
- an obligation can have many events, many ledger entries, and many interest rate periods

### Financial Event

Represents a real-world event that may create or change a debt.

This is the user-facing concept behind one-time and recurring actions:

- cash loan was given
- rent charge happened
- phone bill charge happened
- repayment was made
- manual correction was entered
- interest was posted

Likely fields:

- obligation
- event_type: principal_advance, repayment, scheduled_charge, interest_posting, adjustment
- source: manual, generated, system
- event_date
- amount_units
- currency
- direction: increases_debt, decreases_debt
- memo
- optional category
- optional event_series
- optional event_series_version
- optional period_start and period_end for generated monthly events
- voided_at, nullable
- timestamps

Rules:

- one-time events have no event series
- recurring occurrences are generated from an event series
- a financial event can create one or more ledger entries, but the first version should create exactly one ledger entry
- posted events should not be destructively edited; prefer voiding plus a replacement event for history
- changing a future recurring amount should create a new series version, not rewrite historical events

### Ledger Account

Represents one side of an obligation in the double-entry-lite ledger.

For each obligation, v1 creates:

- receivable account owned by the creditor
- payable account owned by the borrower

Rules:

- each obligation has one receivable account and one payable account
- debt increases debit the receivable account and credit the payable account
- repayments debit the payable account and credit the receivable account
- account balances are derived from posted ledger entries

### Ledger Transaction

Represents one balanced accounting transaction.

Examples:

- initial principal advance
- repayment
- generated rent charge
- monthly interest posting

Rules:

- transactions start as draft
- a transaction can be posted only when debit total equals credit total
- posted transactions are immutable
- idempotency keys prevent duplicate generated monthly charges and interest postings

### Event Series

Represents a recurring schedule, such as rent, phone bill, utilities, or another monthly charge.

Likely fields:

- obligation
- name
- event_type, usually scheduled_charge
- frequency: monthly for v1
- day_of_month
- starts_on
- ends_on, nullable
- active flag
- auto_post flag, default false for v1
- memo
- timestamps

Rules:

- first implementation can generate entries from admin or a management command; a background worker is not required
- if day_of_month does not exist in a month, use the last day of that month
- generated events must be idempotent for the same series and month
- editing schedule rules affects future generated events only

### Event Series Version

Represents an amount or memo change inside an existing recurring schedule.

Example:

- rent is `$1000` per month from January through June
- rent becomes `$1100` per month starting July

Likely fields:

- event_series
- amount_units
- currency
- valid_from
- valid_to, nullable
- memo
- timestamps

Rules:

- only one version should be active for a given series on a given occurrence date
- generated events copy amount and memo from the active version
- historical generated events keep the amount that was valid when they were created

### Ledger Entry

Represents every accounting movement that changes an obligation balance.

Likely fields:

- obligation
- financial_event
- entry_type: principal_advance, repayment, scheduled_charge, interest_accrual, adjustment
- effective_date
- amount_units as a positive integer
- currency, default `USD`
- currency_exponent, default `4`
- direction: debit_increase, credit_decrease
- memo
- timestamps

Rules:

- amount is stored as positive integer units, not float
- debit and credit side determines accounting behavior
- posted entries are immutable
- corrections should be modeled as reversal or adjustment entries

### Interest Rate Period

Represents the annual interest rate that applies to an obligation during a date range.

This exists because the interest rate can change over time.

Likely fields:

- obligation
- annual_rate_percent
- effective_from
- effective_to, nullable
- memo
- timestamps

Rules:

- rate is a `Decimal`, for example `3.5` for `3.5%`
- only one rate period should be active for an obligation on a given date
- a zero-rate period is allowed
- changing the rate creates a new period and closes the previous period
- historical interest calculations should be reproducible from stored rate periods and ledger history

### Interest Accrual Run

Represents one monthly interest calculation/posting for an obligation.

Likely fields:

- obligation
- period_start
- period_end
- posted_on
- calculated_interest_amount_units
- ledger_entry, nullable until posted
- status: calculated, posted, voided
- calculation_payload, JSON for explainability
- timestamps

Rules:

- one posted run per obligation and monthly period
- the run stores enough detail to explain the result later
- the posted interest itself becomes a ledger entry with type `interest_accrual`

### Audit Event

Represents meaningful non-financial history.

Likely events:

- obligation created
- financial event created
- financial event voided
- recurring series configured
- recurring series version added
- interest rate changed
- interest posted
- obligation status changed

Rules:

- do not store sensitive banking data
- keep the event payload simple and readable
- audit events are for explanation, not for balance calculation

## Money Flow

### One-Time Debt

1. A person borrows money from another person.
2. TrustTrack creates an obligation.
3. TrustTrack creates a financial event with type `principal_advance`.
4. The event creates a ledger entry that increases the obligation balance.
5. Future repayments create financial events and ledger entries that reduce the balance.

### One-Time Repayment

1. A borrower pays back some amount.
2. TrustTrack creates a financial event with type `repayment`.
3. The event creates a ledger entry that reduces the obligation balance.
4. The repayment affects future interest calculations from its effective date.

### Monthly Scheduled Charge

1. A recurring series is configured for an obligation.
2. The active series version defines the amount for a given month.
3. The recurring service generates one financial event for the month.
4. The event creates a ledger entry that increases the obligation balance.
5. Idempotency prevents duplicate events for the same series and month.

If a recurring series starts in the past, the UI can generate all due monthly charges up to today. A blank end date means the schedule remains active until the obligation is closed or the series is stopped.

### Monthly Automatic Repayment

A recurring series can also represent an automatic repayment, such as a direct deposit or bank autopay.

Rules:

- scheduled charge series increase the debt
- automatic repayment series decrease the debt
- repayment series still use the same monthly schedule and versioned amount model
- generated repayments are posted as `repayment` events with source `generated`
- v1 keeps the same overpayment protection as manual repayments

This lets a debt have both monthly charges, such as rent, and monthly repayments, such as an automatic bank withdrawal.

### Stop Tracking

Obligations and ledger transactions should not be destructively deleted in normal use. When a debt should no longer be tracked, TrustTrack closes the obligation and stops future recurring series. Existing financial events and ledger entries remain available as history.

### Recurring Amount Change

1. The user changes a future monthly amount, for example rent from `$1000` to `$1100`.
2. TrustTrack closes the previous event series version.
3. TrustTrack creates a new event series version with the new amount and valid_from date.
4. Historical generated events remain unchanged.
5. Future generated events use the new amount.

The same versioning rule applies to automatic repayment series.

### Interest Posting

1. The interest service reads the obligation ledger history for a monthly period.
2. The service reads interest rate periods active during that period.
3. The service splits the month into daily or date-range segments whenever balance or rate changes.
4. Interest is calculated using the daily rate for each segment.
5. A monthly interest accrual run stores the calculation details.
6. The posted interest creates a ledger entry that increases the balance.

### Interest Recalculation

Posted interest should not be edited in place. If a backdated repayment or charge changes the balance for an already posted month, TrustTrack reverses the old interest posting with a separate adjustment transaction, marks the old interest run as voided, and posts a new interest run with the next revision number.

Example:

1. January through May interest is generated.
2. A repayment is later recorded with an effective date in January.
3. Recalculation starts from January.
4. January through May posted interest runs are reversed on their original posting dates.
5. Interest is posted again month by month using the corrected dated balance history.

This keeps the ledger append-only and still gives the current balance the corrected result.

## Interest Calculation Rules

TrustTrack should follow a credit-bureau-like daily interest model with monthly posting:

```text
daily_rate = annual_rate_percent / 100 / days_in_year
segment_interest = segment_balance * daily_rate * segment_day_count
monthly_interest = sum(segment_interest for all segments in the month)
```

Rules:

- store posted money as integer units with 4 decimal places
- use `Decimal` for percentage and intermediate interest math
- do not use `float`
- calculate interest from dated balances
- if a repayment happens inside the month, the balance base changes from that repayment date
- if the interest rate changes inside the month, the rate changes from that effective date
- post interest once per obligation per month
- round to currency minor units only at posting time
- stored calculation payload should show period dates, balances, rates, day counts, and rounded result

Day-count basis:

- v1 uses APR divided by a fixed 365-day year
- this can become configurable later if needed

Posting convention:

- calculate for `[month_start, next_month_start)`
- post on `next_month_start`
- posted interest affects balances after the posting date

## Backend Dependency Map

The first Django app should be named `ledger`.

Recommended module responsibilities:

- `ledger.models`: domain tables only, with lightweight validation
- `ledger.admin`: admin registration for maintenance and inspection
- `ledger.forms`: server-rendered forms for obligations, events, repayments, schedules, and rates
- `ledger.views`: page orchestration only, no financial math
- `ledger.services.balances`: balance summaries from ledger entries
- `ledger.services.events`: creates financial events and their ledger entries
- `ledger.services.recurring`: monthly event generation from event series and versions
- `ledger.services.interest`: daily interest calculations and monthly posting helpers
- `ledger.services.audit`: audit event creation helpers
- `ledger.tests`: focused tests for domain rules, services, and page smoke coverage

Dependency rule:

- views may call forms and services
- forms may validate user input
- services may read and write models
- services may call other services intentionally
- financial calculations must not depend on templates or request objects
- models should not perform multi-step financial posting inside `save()`

## Suggested Model List for First Migration

The first real migration should include:

- Django `User`
- `Obligation`
- `FinancialEvent`
- `LedgerAccount`
- `LedgerTransaction`
- `EventSeries`
- `EventSeriesVersion`
- `LedgerEntry`
- `InterestRatePeriod`
- `InterestAccrualRun`
- `AuditEvent`

This is enough to support one-time debts, repayments, editable recurring monthly charges, interest rate changes, monthly interest posting, and an explainable double-entry-lite ledger.

## Initial Pages

First useful server-rendered pages:

- dashboard with total open balances and recent activity
- obligation list
- obligation detail with ledger history, event history, rate history, and balance
- create obligation form
- record one-time event form
- record repayment form
- configure recurring series form
- add recurring amount version form
- add interest rate period form
- run monthly interest posting action

Django admin remains available for maintenance and early data inspection.

## Initial GitHub Issues

### Define domain schema and backend dependency map

Labels: `area:domain`, `type:schema`, `priority:p1`

Acceptance criteria:

- domain entities are documented before migrations are written
- money flow is documented
- service boundaries for balances, interest, and recurring charges are documented

### Create Django domain app structure

Labels: `area:backend`, `type:schema`, `priority:p1`

Acceptance criteria:

- `ledger` app exists and is installed
- initial module structure is present
- Django check passes

### Add core models and admin registration

Labels: `area:backend`, `type:model`, `priority:p1`

Acceptance criteria:

- people, obligations, financial events, event series, ledger entries, interest rates, and audit events are modeled
- migrations are created
- admin pages expose the core records
- model validation covers creditor/borrower differences, money fields, non-overlapping rate periods, and non-overlapping series versions

### Add event and balance services

Labels: `area:backend`, `type:logic`, `priority:p1`

Acceptance criteria:

- financial events create ledger entries consistently
- current balance is derived from ledger entries
- repayments reduce balances correctly
- advances, scheduled charges, and interest increase balances correctly
- tests cover open, closed, and zero-balance obligations

### Add recurring event generation service

Labels: `area:backend`, `type:logic`, `priority:p1`

Acceptance criteria:

- monthly event series generate expected events
- amount changes are handled by series versions
- generated events are idempotent per series and month
- tests cover month-end dates and changed amounts

### Add interest accrual calculation tests

Labels: `area:tests`, `type:logic`, `priority:p1`

Acceptance criteria:

- tests cover daily simple interest
- tests cover partial repayments
- tests cover rate changes
- tests cover monthly capitalization/posting
- tests use integer-unit money expectations and `Decimal` rate expectations

### Add basic dashboard, list, and detail pages

Labels: `area:ui`, `type:view`, `priority:p2`

Acceptance criteria:

- dashboard shows open obligations and recent ledger activity
- list page shows obligations
- detail page shows balance, events, ledger entries, recurring schedules, and rate periods
- view smoke tests pass

### Add forms for obligations, events, schedules, and payments

Labels: `area:ui`, `area:backend`, `type:view`, `priority:p2`

Acceptance criteria:

- obligation creation form creates an obligation and initial event
- repayment form records a repayment event
- recurring series form creates a schedule
- amount version form changes future recurring amounts
- validation errors are shown on the page
- tests cover successful and invalid submissions

### Add audit and history display

Labels: `area:backend`, `area:ui`, `type:view`, `priority:p2`

Acceptance criteria:

- important changes produce audit events
- obligation detail shows meaningful history
- admin can inspect audit records

### Add backup and restore documentation check

Labels: `area:domain`, `area:tests`, `priority:p2`

Acceptance criteria:

- backup/restore instructions are verified against the current app shape
- docs mention what data is included
- docs mention that `db.sqlite3` stays local and uncommitted

## Test Strategy

Baseline command:

```bash
uv run python manage.py check
```

Future test command:

```bash
uv run python manage.py test
```

Minimum scenarios before changing financial behavior:

- full repayment closes or zeroes an obligation
- partial repayment changes the interest base from the repayment date
- daily interest is calculated with `Decimal`
- rate change inside a month splits the calculation
- monthly interest posting is explainable from ledger history
- recurring monthly charges are not duplicated for the same month
- recurring amount changes affect future months without rewriting history
- dashboard and detail pages render for logged-in local users

## Current Implementation Boundary

The first `ledger` app foundation now exists with models, admin registration, services, migrations, and focused tests. The next implementation layer should add server-rendered forms and views on top of the service API instead of writing financial logic in views.
