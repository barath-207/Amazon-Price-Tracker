"""
SQLite persistence layer.

Schema is created/migrated on first use. The database file lives at
``data/amazon_tracker.db`` and is committed back to the repo by the GitHub
Action so history survives across ephemeral runner runs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from amazon.models import Offer

from .models import PriceRow

log = logging.getLogger("tracker.database")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id           TEXT PRIMARY KEY,
    asin         TEXT,
    name         TEXT,
    url          TEXT,
    target_price REAL,
    config_json  TEXT,
    first_seen   TEXT,
    last_checked TEXT,
    enabled      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          TEXT NOT NULL,
    asin                TEXT,
    timestamp           TEXT NOT NULL,
    selling_price       REAL,
    mrp                 REAL,
    deal_price          REAL,
    coupon_amount       REAL,
    coupon_percent      REAL,
    effective_price     REAL,
    availability        TEXT,
    in_stock            INTEGER,
    seller              TEXT,
    fulfilled_by_amazon INTEGER,
    variant             TEXT,
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_product ON price_history(product_id);
CREATE INDEX IF NOT EXISTS idx_price_ts ON price_history(timestamp);

CREATE TABLE IF NOT EXISTS offers_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    offer_hash        TEXT,
    offer_data        TEXT,
    price_history_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_offers_product ON offers_history(product_id);

CREATE TABLE IF NOT EXISTS checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    status        TEXT,
    error         TEXT,
    response_time REAL,
    changed       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checks_product ON checks(product_id);

CREATE TABLE IF NOT EXISTS product_state (
    product_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    timestamp  TEXT,
    PRIMARY KEY (product_id, key)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Thread-safe wrapper around the SQLite file."""

    _lock = threading.Lock()

    def __init__(self, path: str | Path = "data/amazon_tracker.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                ("version", str(SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------
    def upsert_product(self, pid: str, asin: Optional[str], name: Optional[str],
                       url: str, target_price: Optional[float] = None,
                       config_json: Optional[str] = None, enabled: bool = True) -> None:
        now = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO products(id, asin, name, url, target_price, config_json,
                                     first_seen, last_checked, enabled)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    asin  = COALESCE(excluded.asin, products.asin),
                    name  = COALESCE(excluded.name, products.name),
                    url   = COALESCE(excluded.url, products.url),
                    target_price = COALESCE(excluded.target_price, products.target_price),
                    config_json  = COALESCE(excluded.config_json, products.config_json),
                    enabled = excluded.enabled,
                    first_seen = COALESCE(products.first_seen, ?)
                """,
                (pid, asin, name, url, target_price, config_json, now, None, int(enabled), now),
            )

    def get_product(self, pid: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def list_products(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM products ORDER BY id").fetchall()

    def set_last_checked(self, pid: str, ts: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE products SET last_checked = ? WHERE id = ?", (ts or utc_now(), pid))

    # ------------------------------------------------------------------
    # Price history
    # ------------------------------------------------------------------
    def add_price_observation(
        self,
        product_id: str,
        asin: Optional[str],
        timestamp: str,
        selling_price: Optional[float],
        mrp: Optional[float] = None,
        deal_price: Optional[float] = None,
        coupon_amount: Optional[float] = None,
        coupon_percent: Optional[float] = None,
        effective_price: Optional[float] = None,
        availability: str = "",
        in_stock: bool = True,
        seller: Optional[str] = None,
        fulfilled_by_amazon: bool = False,
        variant: Optional[str] = None,
        raw_json: Optional[str] = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO price_history(
                    product_id, asin, timestamp, selling_price, mrp, deal_price,
                    coupon_amount, coupon_percent, effective_price, availability,
                    in_stock, seller, fulfilled_by_amazon, variant, raw_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, asin, timestamp, selling_price, mrp, deal_price,
                 coupon_amount, coupon_percent, effective_price, availability,
                 int(in_stock), seller, int(fulfilled_by_amazon), variant, raw_json),
            )
            return int(cur.lastrowid)

    def price_history(self, product_id: str, limit: Optional[int] = None) -> list[PriceRow]:
        sql = "SELECT * FROM price_history WHERE product_id = ? ORDER BY timestamp ASC"
        params: list[Any] = [product_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            PriceRow(
                product_id=r["product_id"],
                timestamp=r["timestamp"],
                selling_price=r["selling_price"],
                mrp=r["mrp"],
                effective_price=r["effective_price"],
                coupon_amount=r["coupon_amount"],
                availability=r["availability"] or "",
                in_stock=r["in_stock"] or 0,
                seller=r["seller"],
                fulfilled_by_amazon=r["fulfilled_by_amazon"] or 0,
                variant=r["variant"],
            )
            for r in rows
        ]

    def last_price_row(self, product_id: str) -> Optional[PriceRow]:
        rows = self.price_history(product_id, limit=1)
        # limit with ASC gives the first; we need the last -> re-query
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM price_history WHERE product_id = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (product_id,),
            ).fetchone()
        if not r:
            return None
        return PriceRow(
            product_id=r["product_id"], timestamp=r["timestamp"],
            selling_price=r["selling_price"], mrp=r["mrp"],
            effective_price=r["effective_price"], coupon_amount=r["coupon_amount"],
            availability=r["availability"] or "", in_stock=r["in_stock"] or 0,
            seller=r["seller"], fulfilled_by_amazon=r["fulfilled_by_amazon"] or 0,
            variant=r["variant"],
        )

    # ------------------------------------------------------------------
    # Offers history
    # ------------------------------------------------------------------
    def add_offers(self, product_id: str, timestamp: str, offers: list[Offer],
                   price_history_id: Optional[int] = None) -> None:
        with self._conn() as conn:
            for offer in offers:
                conn.execute(
                    "INSERT INTO offers_history(product_id, timestamp, offer_hash, offer_data, price_history_id) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (product_id, timestamp, offer.hash(),
                     json.dumps(offer.to_dict(), default=str), price_history_id),
                )

    def last_offers(self, product_id: str) -> list[Offer]:
        """Return the offers from the most recent check that recorded them.

        Groups by ``price_history_id`` so two checks within the same second
        cannot bleed together.
        """
        with self._conn() as conn:
            r = conn.execute(
                "SELECT MAX(price_history_id) AS pid FROM offers_history WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            if not r or r["pid"] is None:
                return []
            rows = conn.execute(
                "SELECT offer_data FROM offers_history "
                "WHERE product_id = ? AND price_history_id = ? ORDER BY id",
                (product_id, r["pid"]),
            ).fetchall()
        return [Offer.from_dict(json.loads(row["offer_data"])) for row in rows]

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def add_check(self, product_id: str, timestamp: str, status: str,
                  error: Optional[str], response_time: Optional[float],
                  changed: bool = False) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO checks(product_id, timestamp, status, error, response_time, changed) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (product_id, timestamp, status, error, response_time, int(changed)),
            )

    # ------------------------------------------------------------------
    # Generic key/value state (target-price crossing flags, etc.)
    # ------------------------------------------------------------------
    def get_state(self, product_id: str, key: str) -> Optional[str]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT value FROM product_state WHERE product_id = ? AND key = ?",
                (product_id, key),
            ).fetchone()
            return r["value"] if r else None

    def set_state(self, product_id: str, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO product_state(product_id, key, value, timestamp) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(product_id, key) DO UPDATE SET value=excluded.value, "
                "timestamp=excluded.timestamp",
                (product_id, key, value, utc_now()),
            )

    # ------------------------------------------------------------------
    def observation_count(self, product_id: str) -> int:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM price_history WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            return int(r["c"]) if r else 0
