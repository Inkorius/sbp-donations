import asyncio
import logging
import os
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg

logger = logging.getLogger(__name__)

RECEIPT_SERVICE_NAME = os.getenv("MOY_NALOG_SERVICE_NAME", "Прочие услуги")
PRINT_URL = "https://lknpd.nalog.ru/api/v1/receipt/{inn}/{uuid}/print"


def nalog_configured() -> bool:
    return bool(os.getenv("MOY_NALOG_INN", "").strip() and os.getenv("MOY_NALOG_PASSWORD", "").strip())


def _credentials() -> tuple[str, str]:
    inn = os.getenv("MOY_NALOG_INN", "").strip()
    password = os.getenv("MOY_NALOG_PASSWORD", "").strip()
    if not inn or not password:
        raise RuntimeError("MOY_NALOG_INN and MOY_NALOG_PASSWORD are required")
    return inn, password


def _receipt_fields(income, inn: str) -> tuple[str, str]:
    uuid = str(
        getattr(income, "approved_receipt_uuid", None)
        or getattr(income, "uuid", None)
        or ""
    )
    if not uuid:
        raise RuntimeError("Мой Налог did not return receipt uuid")
    url = (
        getattr(income, "print_url", None)
        or getattr(income, "link", None)
        or PRINT_URL.format(inn=inn, uuid=uuid)
    )
    return uuid, str(url)


async def create_income_receipt(name: str, amount: Decimal) -> tuple[str, str]:
    from nalogovich.enums import PaymentType
    from nalogovich.lknpd import NpdClient

    inn, password = _credentials()
    async with NpdClient(inn=inn, password=password) as client:
        await client.auth()
        try:
            income = await client.create_check(
                name=name,
                amount=float(amount),
                payment_type=PaymentType.WIRE,
            )
        except Exception:
            income = await client.create_check(
                name=name,
                amount=float(amount),
                payment_type=PaymentType.CASH,
            )
        return _receipt_fields(income, inn)


async def enqueue_receipt(pool: asyncpg.Pool, donation_id: UUID, amount: Decimal) -> None:
    if not nalog_configured():
        logger.info("Мой Налог is not configured, skip receipt for %s", donation_id)
        return
    existing = await pool.fetchval(
        "SELECT 1 FROM receipts_queue WHERE donation_id = $1 AND status IN ('pending', 'done')",
        donation_id,
    )
    if existing:
        return
    await pool.execute(
        """
        INSERT INTO receipts_queue (id, donation_id, amount, service_name, status)
        VALUES ($1, $2, $3, $4, 'pending')
        """,
        uuid4(),
        donation_id,
        amount,
        RECEIPT_SERVICE_NAME,
    )
    await process_pending_receipts(pool)


async def process_pending_receipts(pool: asyncpg.Pool) -> None:
    if not nalog_configured():
        return
    rows = await pool.fetch(
        """
        SELECT id, donation_id, amount, service_name, attempts
        FROM receipts_queue
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT 20
        """
    )
    for row in rows:
        await _send_queued_receipt(pool, row)


async def _send_queued_receipt(pool: asyncpg.Pool, row: asyncpg.Record) -> None:
    donation_id: UUID = row["donation_id"]
    already = await pool.fetchval(
        "SELECT receipt_uuid FROM donations WHERE id = $1",
        donation_id,
    )
    if already:
        await pool.execute(
            "UPDATE receipts_queue SET status = 'done', updated_at = NOW() WHERE id = $1",
            row["id"],
        )
        return
    try:
        uuid, url = await create_income_receipt(row["service_name"], Decimal(row["amount"]))
        await pool.execute(
            """
            UPDATE donations
            SET receipt_uuid = $2, receipt_url = $3
            WHERE id = $1
            """,
            donation_id,
            uuid,
            url,
        )
        await pool.execute(
            """
            UPDATE receipts_queue
            SET status = 'done', last_error = NULL, updated_at = NOW()
            WHERE id = $1
            """,
            row["id"],
        )
        logger.info("Мой Налог receipt saved for donation %s", donation_id)
    except Exception as exc:
        logger.exception("Мой Налог receipt failed for donation %s", donation_id)
        await pool.execute(
            """
            UPDATE receipts_queue
            SET attempts = attempts + 1, last_error = $2, updated_at = NOW()
            WHERE id = $1
            """,
            row["id"],
            str(exc)[:1000],
        )


async def receipts_retry_loop(pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            await process_pending_receipts(pool)
        except Exception:
            logger.exception("Мой Налог retry loop failed")
