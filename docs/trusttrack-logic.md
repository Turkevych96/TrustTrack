# TrustTrack Logic Map

This document defines the first backend planning artifact for TrustTrack. It is intentionally written before models, migrations, and views so the backend can be built from a stable domain map.

## Product Direction

TrustTrack is a private, local Django app for tracking family debts between trusted people. It should stay simple: classic Django, SQLite, server-rendered pages, Django admin, and explicit financial logic covered by tests.

The system should answer four questions clearly:

- Who owes whom?
- What created or changed the debt?
- What is the current balance?
- How were interest and monthly charges calculated?

## Domain Entities

### Person

Represents a trusted participant in the family ledger.

Likely fields:

- display name
- optional email or note
- active/inactive flag
- timestamps

Relationships:

- can be a creditor on an obligation
- can be a borrower on an obligation
- can appear in audit records as the actor once app users are connected to domain people

### Obligation

Represents a debt relationship between one borrower and one creditor.

Likely fields:

- creditor
- borrower
- title or category
- original principal amount
- annual interest rate
- start date
- status: open, closed, canceled
- notes
- timestamps

Rules:

- creditor and borrower must be different people
- money values must use `Decimal`
- interest rates must use `Decimal`
- the current balance should be derived from ledger entries, not manually edited as the source of truth

### Ledger Entry

Represents every financial movement that changes an obligation.

Likely entry types:

- principal advance
- repayment
- interest accrual
- recurring monthly charge
- adjustment

Likely fields:

- obligation
- entry type
- effective date
- amount
- memo
- created timestamp
- optional created-by user

Rules:

- positive/negative meaning must be consistent and documented in code
- repayments reduce balance
- advances, monthly charges, and interest accruals increase balance
- auditability matters more than editing convenience

### Recurring Charge

Represents a fixed scheduled charge, such as rent, that can create monthly ledger entries.

Likely fields:

- obligation
- amount
- day of month
- start date
- optional end date
- active flag
- memo/category

Rules:

- first implementation can be manual/admin-triggered instead of a background worker
- generated entries must be idempotent for the same obligation, charge, and month

### Interest Accrual

Represents calculated interest posted to the ledger.

This can be stored as a ledger entry with type `interest_accrual`; a separate model is only needed if the calculation details become too large for ledger metadata.

Rules:

- daily simple interest is calculated from the active principal/balance base
- partial repayments change the base from their effective date
- accumulated interest is posted at the start of the next month
- calculation code must be isolated from views and covered by focused tests

### Audit Event

Represents meaningful non-financial history.

Likely events:

- obligation created
- obligation status changed
- ledger entry created
- ledger entry adjusted or voided
- recurring charge configured

Rules:

- do not store sensitive banking data
- keep the event payload simple and readable

## Money Flow

1. A person borrows money from another person.
2. TrustTrack creates an obligation and an initial principal ledger entry.
3. Repayments are recorded as ledger entries that reduce the balance.
4. Monthly charges can add new principal-like ledger entries.
5. Interest calculation reads the dated ledger history and computes interest for a period.
6. Posted interest becomes a ledger entry so the balance is explainable.
7. The dashboard and detail pages show balances derived from ledger history.

## Backend Dependency Map

The first Django app should be named `ledger` unless a better name is chosen before implementation.

Recommended module responsibilities:

- `ledger.models`: domain tables only, with lightweight validation.
- `ledger.admin`: admin registration for maintenance and inspection.
- `ledger.forms`: server-rendered forms for obligations, repayments, and charges.
- `ledger.views`: page orchestration only, no financial math.
- `ledger.services.balances`: balance summaries from ledger entries.
- `ledger.services.interest`: daily interest calculations and monthly posting helpers.
- `ledger.services.recurring`: monthly charge generation.
- `ledger.tests`: focused tests for domain rules, services, and page smoke coverage.

Dependency rule:

- views may call services
- forms may validate user input
- services may read/write models
- financial calculations must not depend on templates or request objects

## Initial Pages

First useful server-rendered pages:

- dashboard with total open balances and recent activity
- obligation list
- obligation detail with ledger history
- create obligation form
- record repayment form
- configure recurring charge form

Django admin remains available for maintenance and early data inspection.

## GitHub Project Workflow

Project: `TrustTrack`

Recommended statuses:

- Todo
- In Progress
- Review
- Done

Recommended labels:

- `area:domain`
- `area:backend`
- `area:ui`
- `area:tests`
- `type:schema`
- `type:model`
- `type:logic`
- `type:view`
- `priority:p1`
- `priority:p2`

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

- people, obligations, ledger entries, recurring charges, and audit events are modeled
- migrations are created
- admin pages expose the core records
- model validation covers creditor/borrower differences and money fields

### Add balance and ledger calculation service

Labels: `area:backend`, `type:logic`, `priority:p1`

Acceptance criteria:

- current balance is derived from ledger entries
- repayments reduce balances correctly
- advances, charges, and interest increase balances correctly
- tests cover open and closed obligations

### Add interest accrual calculation tests

Labels: `area:tests`, `type:logic`, `priority:p1`

Acceptance criteria:

- tests cover daily simple interest
- tests cover partial repayments
- tests cover monthly capitalization
- tests use `Decimal` expectations

### Add basic dashboard, list, and detail pages

Labels: `area:ui`, `type:view`, `priority:p2`

Acceptance criteria:

- dashboard shows open obligations and recent ledger activity
- list page shows obligations
- detail page shows balance and ledger history
- view smoke tests pass

### Add forms for creating obligations and recording payments

Labels: `area:ui`, `area:backend`, `type:view`, `priority:p2`

Acceptance criteria:

- obligation creation form creates an obligation and initial ledger entry
- repayment form records a repayment ledger entry
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
- monthly interest posting is explainable from ledger history
- recurring monthly charges are not duplicated for the same month
- dashboard and detail pages render for logged-in local users

## Current Implementation Boundary

This document does not create models or migrations. It defines the implementation path so the next PR can add the `ledger` app with clear responsibilities and tests.
