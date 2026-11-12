import os
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv()

pool: Optional[asyncpg.Pool] = None


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url.replace("sslmode=disable", "ssl=disable")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(database_url(), min_size=1, max_size=10)
    try:
        yield
    finally:
        await pool.close()
        pool = None


app = FastAPI(title="sbp-donation-api", lifespan=lifespan)


class DonationCreate(BaseModel):
    profile_id: UUID
    donor_name: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0)
    message: Optional[str] = Field(default=None, max_length=500)


class DonationOut(BaseModel):
    id: UUID
    profile_id: UUID
    donor_name: str
    amount: Decimal
    message: Optional[str]
    status: str
    created_at: datetime


def row_to_donation(row: asyncpg.Record) -> DonationOut:
    return DonationOut(
        id=row["id"],
        profile_id=row["profile_id"],
        donor_name=row["donor_name"],
        amount=row["amount"],
        message=row["message"],
        status=row["status"],
        created_at=row["created_at"],
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sbp-donation-api"}


@app.get("/health/db")
async def health_db():
    try:
        value = await pool.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "database": "connected", "check": value}


@app.post("/api/donations", status_code=201)
async def create_donation(body: DonationCreate) -> DonationOut:
    profile_exists = await pool.fetchval(
        "SELECT 1 FROM profiles WHERE id = $1",
        body.profile_id,
    )
    if not profile_exists:
        raise HTTPException(status_code=404, detail="profile not found")

    row = await pool.fetchrow(
        """
        INSERT INTO donations (profile_id, donor_name, amount, message, status)
        VALUES ($1, $2, $3, $4, 'pending')
        RETURNING id, profile_id, donor_name, amount, message, status, created_at
        """,
        body.profile_id,
        body.donor_name.strip(),
        body.amount,
        (body.message or "").strip() or None,
    )
    return row_to_donation(row)


@app.get("/api/donations")
async def list_donations(
    profile_id: UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
) -> list[DonationOut]:
    rows = await pool.fetch(
        """
        SELECT id, profile_id, donor_name, amount, message, status, created_at
        FROM donations
        WHERE profile_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        profile_id,
        limit,
    )
    return [row_to_donation(row) for row in rows]
