"""tools/store_ext.py - Front Desk AI's own tables, layered on core.store.Store.

The generic `items` table (core/store.py) is the review queue: one row per
inbound message, confirmation or reminder waiting on a human or a send. It is
not a booking ledger. This module adds the tables a hotel actually needs to
query - room/table/spa bookings, the experience catalogue and its live
inventory, and escalations - and a couple of pure helper functions the engine
and the tests both use.

Call :func:`ensure_schema` once per `Store` before touching any of these
tables; every tool in this repo does it right after constructing its `Store`.
Nothing here replaces `core.store` - it is additive, using the same
connection (`store.db`), the same `utcnow()` timestamp convention, and the
same JSON-column convention as the tables `core.store` itself defines.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from core.store import Store, utcnow

from pricing import config_price

SCHEMA = """
CREATE TABLE IF NOT EXISTS room_bookings (
  id            TEXT PRIMARY KEY,
  ref           TEXT NOT NULL UNIQUE,
  item_id       TEXT,
  guest_name    TEXT,
  room_type     TEXT NOT NULL,
  checkin       TEXT NOT NULL,
  checkout      TEXT NOT NULL,
  pax           INTEGER NOT NULL DEFAULT 2,
  total_eur     REAL NOT NULL,
  channel       TEXT,
  status        TEXT NOT NULL DEFAULT 'confirmed',
  notes         TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS table_bookings (
  id               TEXT PRIMARY KEY,
  ref              TEXT NOT NULL UNIQUE,
  item_id          TEXT,
  guest_name       TEXT,
  party_size       INTEGER NOT NULL DEFAULT 2,
  date             TEXT NOT NULL,
  time             TEXT NOT NULL,
  dietary_notes    TEXT,
  special_requests TEXT,
  channel          TEXT,
  status           TEXT NOT NULL DEFAULT 'confirmed',
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spa_bookings (
  id            TEXT PRIMARY KEY,
  ref           TEXT NOT NULL UNIQUE,
  item_id       TEXT,
  guest_name    TEXT,
  treatment_id  TEXT NOT NULL,
  treatment     TEXT NOT NULL,
  date          TEXT NOT NULL,
  time          TEXT NOT NULL,
  party_size    INTEGER NOT NULL DEFAULT 1,
  price_eur     REAL NOT NULL,
  reservation_ref TEXT,
  channel       TEXT,
  status        TEXT NOT NULL DEFAULT 'confirmed',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_sessions (
  slug          TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  schedule_label TEXT NOT NULL,
  next_date     TEXT NOT NULL,
  start_time    TEXT NOT NULL,
  price_eur     REAL NOT NULL,
  capacity      INTEGER NOT NULL,
  booked        INTEGER NOT NULL DEFAULT 0,
  venue         TEXT,
  host          TEXT
);

CREATE TABLE IF NOT EXISTS experience_bookings (
  id            TEXT PRIMARY KEY,
  ref           TEXT NOT NULL UNIQUE,
  item_id       TEXT,
  session_slug  TEXT NOT NULL,
  guest_name    TEXT,
  party_size    INTEGER NOT NULL DEFAULT 1,
  date          TEXT NOT NULL,
  total_eur     REAL NOT NULL,
  occasion      TEXT,
  channel       TEXT,
  status        TEXT NOT NULL DEFAULT 'confirmed',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
  id            TEXT PRIMARY KEY,
  item_id       TEXT NOT NULL,
  category      TEXT NOT NULL,
  reason        TEXT NOT NULL,
  ai_suggested_reply TEXT,
  resolution_note TEXT,
  resolved_by   TEXT,
  resolved_at   TEXT,
  improvement_suggestion TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations (status, created_at);

CREATE TABLE IF NOT EXISTS coach_proposals (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  pattern       TEXT NOT NULL,
  intent        TEXT,
  cluster_size  INTEGER NOT NULL,
  example_before TEXT,
  example_after  TEXT,
  suggested_fix TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  decided_at    TEXT,
  applied_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON coach_proposals (status, created_at);
"""


def ensure_schema(store: Store) -> None:
    """Create every table above if it does not already exist. Idempotent."""
    store.db.executescript(SCHEMA)


@dataclass
class ExperienceSession:
    slug: str
    title: str
    schedule_label: str
    next_date: str
    start_time: str
    price_eur: float
    capacity: int
    booked: int
    venue: str = ""
    host: str = ""

    @property
    def spots_left(self) -> int:
        return max(0, self.capacity - self.booked)


def seed_experience_sessions(store: Store, sessions: list[dict]) -> int:
    """Insert the catalogue rows once. Returns how many were newly inserted.

    Safe to call every run: an existing slug is left untouched so live
    `booked` counts are never reset by a re-seed.
    """
    inserted = 0
    for s in sessions:
        row = store.db.execute(
            "SELECT 1 FROM experience_sessions WHERE slug=?", (s["slug"],)).fetchone()
        if row is not None:
            continue
        store.db.execute(
            "INSERT INTO experience_sessions (slug, title, schedule_label, next_date, "
            "start_time, price_eur, capacity, booked, venue, host) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s["slug"], s["title"], s["schedule_label"], s["next_date"], s["start_time"],
             config_price(s, label=s.get("title") or s["slug"]), int(s["capacity"]),
             int(s.get("booked", 0)), s.get("venue", ""), s.get("host", "")))
        inserted += 1
    return inserted


def get_experience_session(store: Store, slug: str) -> ExperienceSession | None:
    row = store.db.execute("SELECT * FROM experience_sessions WHERE slug=?", (slug,)).fetchone()
    if row is None:
        return None
    return ExperienceSession(**{k: row[k] for k in row.keys()})


def list_experience_sessions(store: Store) -> list[ExperienceSession]:
    rows = store.db.execute(
        "SELECT * FROM experience_sessions ORDER BY next_date ASC").fetchall()
    return [ExperienceSession(**{k: r[k] for k in r.keys()}) for r in rows]


def new_id() -> str:
    return uuid.uuid4().hex


def record_escalation(store: Store, item_id: str, category: str, reason: str,
                      ai_suggested_reply: str = "") -> str:
    """Open an escalation row for an item. Returns the escalation id."""
    esc_id = new_id()
    store.db.execute(
        "INSERT INTO escalations (id, item_id, category, reason, ai_suggested_reply, "
        "status, created_at) VALUES (?,?,?,?,?,?,?)",
        (esc_id, item_id, category, reason, ai_suggested_reply, "open", utcnow()))
    return esc_id


def resolve_escalation(store: Store, escalation_id: str, *, resolved_by: str,
                       resolution_note: str = "") -> None:
    store.db.execute(
        "UPDATE escalations SET status='resolved', resolved_by=?, resolution_note=?, "
        "resolved_at=? WHERE id=?",
        (resolved_by, resolution_note, utcnow(), escalation_id))


def open_escalations(store: Store, limit: int = 50) -> list[dict]:
    rows = store.db.execute(
        "SELECT * FROM escalations WHERE status='open' ORDER BY created_at ASC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def resolved_escalations_missing_suggestion(store: Store, limit: int = 50) -> list[dict]:
    rows = store.db.execute(
        "SELECT * FROM escalations WHERE status='resolved' AND "
        "(improvement_suggestion IS NULL OR improvement_suggestion='') "
        "ORDER BY resolved_at ASC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_improvement_suggestion(store: Store, escalation_id: str, text: str) -> None:
    store.db.execute("UPDATE escalations SET improvement_suggestion=? WHERE id=?",
                     (text, escalation_id))
