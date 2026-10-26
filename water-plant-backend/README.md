# Water Plant Management System: Backend

This is the API-first modular monolith from `Water_Plant_Backend_Architecture.md` v1.0. It is built with Python 3.13, Django 5.2 LTS, Django REST Framework, PostgreSQL 17, Celery and Redis.

## Layout

```
services/api/
  config/              settings (base/dev/test/prod), urls, Celery
  common/              ids, money/quantity, errors, base models, request context
  application/
    commands/          cross-module transactions (complete_sale, delivery, refunds, sync, ...)
    queries/           account summary
    api/               DRF views + serializers for /api/v1 (HTTP layer only)
  modules/             domain apps: organization, identity, customers, catalog, orders,
                       inventory, billing, finance, fulfillment, production, plant_care,
                       notifications, support, documents, audit, synchronization, jobs, idempotency
  infrastructure/      messaging adapters, private storage, outbox worker, Celery tasks, reports
  tests/               API, ledger, concurrency (real PostgreSQL), messaging and sync tests
  deploy/db_roles.sql  separate runtime/read-only DB roles; ledger tables append-only
```

## Key rules as implemented

- **One sale is one transaction.** `application/commands/handover.py` posts the handover, invoice (actual quantities only), stock movements, receipt, allocations, deposit entry, cash movement, audit event and outbox events in a single commit.
- **Lock order:** customer credit row, then orders, then numbering, then deposit account, then cash account, then stock buckets (sorted by UUID), then the sync clock.
- **Idempotency** (`modules/idempotency`): the operation ID is claimed inside the posting transaction. A replay returns the original result (HTTP 200). The same ID with a different payload returns `409 IDEMPOTENCY_CONFLICT`.
- **There is one stock ledger.** The bottle ledger is a filtered view of it (`/inventory/balances?bottles_only=true`). Bucket projections are updated in the same transaction as their movements, and `reconcile_projections` checks them.
- **Money:** unallocated receipts are advance credit, deposits are a separate liability, and the account summary reports dues, deposit, advance and bottles separately.
- **Append-only ledgers** are enforced in the ORM and by PostgreSQL triggers.
- **Outbox:** events are written in the business commit. The dispatcher leases deliveries and workers deduplicate against PostgreSQL. An uncertain provider outcome becomes `UNKNOWN` and is never re-sent blindly.
- **Batch holds:** held or unreleased batches cannot be reserved, dispatched or sold. Release requires recorded test results and evidence.
- **Offline till:** a lease allocates stock and signs a price snapshot. Operations are applied in device order, and late or failed operations are kept as `REVIEW_REQUIRED`.

## Run locally

```bash
cp .env.example .env
docker compose up -d postgres redis
cd services/api
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # dev only: owner/cashier/rider/operator, password demo-password-123
uv run python manage.py runserver
```

API docs are served at http://localhost:8000/api/docs/.

Tests need PostgreSQL. They skip themselves on any other database:

```bash
uv run pytest
```

Run the worker and scheduler with `docker compose up worker scheduler`, or with `celery -A config worker` / `celery -A config beat`.

## Not done yet (needs business decisions or a later gate)

- Customer phone verification (OTP) flow and owner MFA. Account linking already requires `contact_verified_at`.
- Real WhatsApp/SMS adapters. The console and fake adapters implement the full interface, and the provider choice is still open.
- Invoice documents are rendered as HTML. A PDF engine with proper Urdu shaping is still to be chosen.
- Opening-balance import (CSV dry-run and approve). The models exist; the import command is pending.
- Malware scanning hook for uploads (finalize currently checks size and file type only).
- Load and performance validation against the section 20 targets.
