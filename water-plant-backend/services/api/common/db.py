"""Database helpers: bounded transactions, optimistic version updates,
append-only triggers."""

from contextlib import contextmanager

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F

from .errors import StaleVersion


def is_postgres() -> bool:
    return connection.vendor == "postgresql"


@contextmanager
def command_transaction():
    """transaction.atomic with bounded lock/statement timeouts (PostgreSQL)."""
    with transaction.atomic():
        if is_postgres():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = %s", [f"{settings.COMMAND_LOCK_TIMEOUT_MS}ms"])
                cur.execute("SET LOCAL statement_timeout = %s", [f"{settings.COMMAND_STATEMENT_TIMEOUT_MS}ms"])
        yield


def update_versioned(instance, expected_version: int, **changes):
    """Optimistic update for master records. Raises StaleVersion (HTTP 412)."""
    model = type(instance)
    updated = model.objects.filter(pk=instance.pk, version=expected_version).update(
        version=F("version") + 1, **changes
    )
    if not updated:
        raise StaleVersion()
    instance.refresh_from_db()
    return instance


def append_only_trigger_sql(table: str) -> tuple[str, str]:
    forward = f"""
    CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION wp_reject_ledger_change();
    """
    reverse = f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
    return forward, reverse


REJECT_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION wp_reject_ledger_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append-only ledger table %: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""
