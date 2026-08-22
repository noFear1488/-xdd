"""Хранилище на SQLite: кэш ответов API, накопленная база лидов и учёт квоты.

Кэш нужен не для скорости, а для экономии квоты: бесплатный ключ Яндекса даёт
500 запросов в сутки, и повторный запуск сканирования не должен их тратить.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import Business

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS businesses (
    company_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    address       TEXT,
    lon           REAL,
    lat           REAL,
    url           TEXT,
    links         TEXT,
    phones        TEXT,
    categories    TEXT,
    hours         TEXT,
    rating        REAL,
    reviews       INTEGER,
    closed        INTEGER DEFAULT 0,
    query         TEXT,
    site_status   TEXT,
    site_domain   TEXT,
    lead_score    INTEGER DEFAULT 0,
    score_reasons TEXT,
    region        TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_businesses_status ON businesses(site_status);
CREATE INDEX IF NOT EXISTS idx_businesses_score ON businesses(lead_score DESC);
CREATE INDEX IF NOT EXISTS idx_businesses_region ON businesses(region);

CREATE TABLE IF NOT EXISTS outreach (
    company_id TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'telegram',
    status     TEXT NOT NULL,
    note       TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (company_id, channel)
);

CREATE TABLE IF NOT EXISTS api_usage (
    day      TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0
);
"""


class Storage:
    """Единая точка доступа к локальной базе."""

    def __init__(self, path: str | Path = "leads.db", cache_ttl_days: int = 14) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_days = cache_ttl_days
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.commit()
        self.close()

    # -- кэш ответов API ------------------------------------------------------

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload, created_at FROM api_cache WHERE key = ?", (self._hash(key),)
        ).fetchone()
        if row is None:
            return None
        if self.cache_ttl_days > 0:
            created = _parse_ts(row["created_at"])
            if created is not None:
                age = dt.datetime.now(dt.timezone.utc) - created
                if age.days >= self.cache_ttl_days:
                    self.conn.execute(
                        "DELETE FROM api_cache WHERE key = ?", (self._hash(key),)
                    )
                    self.conn.commit()
                    return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, payload, created_at) VALUES (?, ?, ?)",
            (self._hash(key), json.dumps(payload, ensure_ascii=False), _now()),
        )
        self.conn.commit()

    def clear_cache(self) -> int:
        cursor = self.conn.execute("DELETE FROM api_cache")
        self.conn.commit()
        return cursor.rowcount

    # -- учёт расхода квоты ---------------------------------------------------

    def record_requests(self, count: int, day: str | None = None) -> None:
        if count <= 0:
            return
        key = day or dt.date.today().isoformat()
        self.conn.execute(
            "INSERT INTO api_usage (day, requests) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET requests = requests + excluded.requests",
            (key, count),
        )
        self.conn.commit()

    def requests_today(self, day: str | None = None) -> int:
        key = day or dt.date.today().isoformat()
        row = self.conn.execute(
            "SELECT requests FROM api_usage WHERE day = ?", (key,)
        ).fetchone()
        return int(row["requests"]) if row else 0

    def usage_history(self, limit: int = 14) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT day, requests FROM api_usage ORDER BY day DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(row["day"], int(row["requests"])) for row in rows]

    # -- учёт обращений -------------------------------------------------------

    def mark_outreach(
        self, company_id: str, status: str, note: str = "", channel: str = "telegram"
    ) -> None:
        self.conn.execute(
            "INSERT INTO outreach (company_id, channel, status, note, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(company_id, channel) DO UPDATE SET"
            " status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
            (company_id, channel, status, note, _now()),
        )
        self.conn.commit()

    def contacted_ids(self, channel: str = "telegram") -> set[str]:
        """Кому уже писали: повторное сообщение — верный способ получить жалобу."""
        rows = self.conn.execute(
            "SELECT company_id FROM outreach WHERE channel = ?", (channel,)
        ).fetchall()
        return {row["company_id"] for row in rows}

    def outreach_counts(self, channel: str = "telegram") -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outreach WHERE channel = ? GROUP BY status",
            (channel,),
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # -- база организаций -----------------------------------------------------

    def upsert(self, business: Business, region: str = "") -> bool:
        """Сохраняет организацию. Возвращает True, если запись новая."""
        now = _now()
        existing = self.conn.execute(
            "SELECT company_id FROM businesses WHERE company_id = ?",
            (business.company_id,),
        ).fetchone()
        payload = (
            business.company_id,
            business.name,
            business.address,
            business.lon,
            business.lat,
            business.url,
            json.dumps(business.links, ensure_ascii=False),
            json.dumps(business.phones, ensure_ascii=False),
            json.dumps(business.categories, ensure_ascii=False),
            business.hours,
            business.rating,
            business.reviews,
            int(business.closed),
            business.query,
            business.site_status,
            business.site_domain,
            business.lead_score,
            json.dumps(business.score_reasons, ensure_ascii=False),
            region,
            now,
        )
        if existing is None:
            self.conn.execute(
                "INSERT INTO businesses (company_id, name, address, lon, lat, url, links,"
                " phones, categories, hours, rating, reviews, closed, query, site_status,"
                " site_domain, lead_score, score_reasons, region, last_seen, first_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*payload, now),
            )
            return True
        self.conn.execute(
            "UPDATE businesses SET name=?, address=?, lon=?, lat=?, url=?, links=?,"
            " phones=?, categories=?, hours=?, rating=?, reviews=?, closed=?, query=?,"
            " site_status=?, site_domain=?, lead_score=?, score_reasons=?, region=?,"
            " last_seen=? WHERE company_id=?",
            (*payload[1:], business.company_id),
        )
        return False

    def save_many(self, businesses: Iterable[Business], region: str = "") -> tuple[int, int]:
        new_count = 0
        total = 0
        for business in businesses:
            total += 1
            if self.upsert(business, region):
                new_count += 1
        self.conn.commit()
        return new_count, total - new_count

    def known_ids(self, region: str | None = None) -> set[str]:
        if region:
            rows = self.conn.execute(
                "SELECT company_id FROM businesses WHERE region = ?", (region,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT company_id FROM businesses").fetchall()
        return {row["company_id"] for row in rows}

    def iter_businesses(
        self,
        *,
        statuses: Iterable[str] | None = None,
        region: str | None = None,
        min_score: int = 0,
        limit: int | None = None,
    ) -> Iterator[Business]:
        sql = "SELECT * FROM businesses WHERE lead_score >= ?"
        params: list[Any] = [min_score]
        statuses = list(statuses) if statuses else []
        if statuses:
            sql += f" AND site_status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if region:
            sql += " AND region = ?"
            params.append(region)
        sql += " ORDER BY lead_score DESC, reviews DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self.conn.execute(sql, params):
            yield _row_to_business(row)

    def counts_by_status(self, region: str | None = None) -> dict[str, int]:
        sql = "SELECT site_status, COUNT(*) AS n FROM businesses"
        params: list[Any] = []
        if region:
            sql += " WHERE region = ?"
            params.append(region)
        sql += " GROUP BY site_status"
        return {
            (row["site_status"] or "?"): int(row["n"])
            for row in self.conn.execute(sql, params)
        }

    def total(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()
        return int(row["n"])


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_ts(raw: str) -> dt.datetime | None:
    try:
        value = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _row_to_business(row: sqlite3.Row) -> Business:
    return Business(
        company_id=row["company_id"],
        name=row["name"],
        address=row["address"] or "",
        lon=row["lon"],
        lat=row["lat"],
        url=row["url"] or "",
        links=_json_list(row["links"]),
        phones=_json_list(row["phones"]),
        categories=_json_list(row["categories"]),
        hours=row["hours"] or "",
        rating=row["rating"],
        reviews=row["reviews"],
        closed=bool(row["closed"]),
        query=row["query"] or "",
        site_status=row["site_status"] or "",
        site_domain=row["site_domain"] or "",
        lead_score=int(row["lead_score"] or 0),
        score_reasons=_json_list(row["score_reasons"]),
    )


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
