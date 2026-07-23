"""Database layer - SQLite schema and data models."""

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

from .config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class CachedEmail:
    """Cached email metadata."""

    gmail_id: str
    thread_id: str
    from_addr: str
    to_addr: str
    subject: str
    snippet: str
    date: datetime
    labels: list[str]
    size_bytes: int
    is_read: bool
    sync_history_id: Optional[str] = None
    last_synced: datetime = field(default_factory=datetime.now)


@dataclass
class Decision:
    """AI decision record."""

    id: Optional[int] = None
    gmail_id: str = ""
    module: str = ""  # 'operator', 'curator', 'archivist'
    suggested_action: dict = field(default_factory=dict)
    final_action: dict = field(default_factory=dict)
    was_auto_executed: bool = False
    user_approved: bool = False
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class Rule:
    """Learned rule from user feedback."""

    id: Optional[int] = None
    condition: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    hit_count: int = 0
    created_from: str = "learned"  # 'user' or 'learned'
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ShapingSession:
    """Profile shaping session record."""

    id: Optional[int] = None
    audit_snapshot: dict = field(default_factory=dict)
    actions_taken: list = field(default_factory=list)
    result_snapshot: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


class Database:
    """SQLite database manager for GhostMail."""

    def __init__(self, db_path: Optional[Path] = None):
        self.settings = get_settings()
        self.db_path = db_path or self.settings.database_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Cached email metadata
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS emails (
                    gmail_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    from_addr TEXT,
                    to_addr TEXT,
                    subject TEXT,
                    snippet TEXT,
                    date TEXT,
                    labels TEXT,
                    size_bytes INTEGER,
                    is_read INTEGER,
                    sync_history_id TEXT,
                    last_synced TEXT
                )
            """)

            # AI decisions + user feedback
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_id TEXT,
                    module TEXT,
                    suggested_action TEXT,
                    final_action TEXT,
                    was_auto_executed INTEGER,
                    user_approved INTEGER,
                    created_at TEXT
                )
            """)

            # User-created rules
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition TEXT,
                    action TEXT,
                    hit_count INTEGER DEFAULT 0,
                    created_from TEXT,
                    created_at TEXT
                )
            """)

            # Curator sessions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS shaping_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_snapshot TEXT,
                    actions_taken TEXT,
                    result_snapshot TEXT,
                    created_at TEXT
                )
            """)

            # Label taxonomy
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS label_taxonomy (
                    gmail_label_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    parent_label TEXT,
                    auto_classify INTEGER DEFAULT 1,
                    created_at TEXT
                )
            """)

            # Sync state
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)

            # Create indexes
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_addr)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_gmail ON decisions(gmail_id)
            """)

            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ==================== Email Operations ====================

    def upsert_email(self, email: CachedEmail):
        """Insert or update email."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO emails (
                    gmail_id, thread_id, from_addr, to_addr, subject, snippet,
                    date, labels, size_bytes, is_read, sync_history_id, last_synced
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email.gmail_id,
                    email.thread_id,
                    email.from_addr,
                    email.to_addr,
                    email.subject,
                    email.snippet,
                    email.date.isoformat(),
                    json.dumps(email.labels),
                    email.size_bytes,
                    1 if email.is_read else 0,
                    email.sync_history_id,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def get_email(self, gmail_id: str) -> Optional[CachedEmail]:
        """Get email by ID."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM emails WHERE gmail_id = ?", (gmail_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_email(row)
            return None

    def get_emails_by_sender(self, sender: str, limit: int = 100) -> list[CachedEmail]:
        """Get emails from specific sender."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM emails WHERE from_addr LIKE ? ORDER BY date DESC LIMIT ?",
                (f"%{sender}%", limit),
            )
            return [self._row_to_email(row) for row in cursor.fetchall()]

    def get_emails_by_label(self, label: str, limit: int = 100) -> list[CachedEmail]:
        """Get emails with specific label."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM emails WHERE labels LIKE ? ORDER BY date DESC LIMIT ?",
                (f"%{label}%", limit),
            )
            return [self._row_to_email(row) for row in cursor.fetchall()]

    def get_recent_emails(self, days: int = 7, limit: int = 100) -> list[CachedEmail]:
        """Get recent emails."""
        with self.get_connection() as conn:
            from datetime import timedelta

            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.execute(
                "SELECT * FROM emails WHERE date >= ? ORDER BY date DESC LIMIT ?",
                (cutoff, limit),
            )
            return [self._row_to_email(row) for row in cursor.fetchall()]

    def _row_to_email(self, row: sqlite3.Row) -> CachedEmail:
        """Convert database row to CachedEmail."""
        return CachedEmail(
            gmail_id=row["gmail_id"],
            thread_id=row["thread_id"],
            from_addr=row["from_addr"],
            to_addr=row["to_addr"],
            subject=row["subject"],
            snippet=row["snippet"],
            date=datetime.fromisoformat(row["date"]),
            labels=json.loads(row["labels"]),
            size_bytes=row["size_bytes"],
            is_read=bool(row["is_read"]),
            sync_history_id=row["sync_history_id"],
            last_synced=datetime.fromisoformat(row["last_synced"]),
        )

    # ==================== Decision Operations ====================

    def add_decision(self, decision: Decision) -> int:
        """Add a new decision record."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO decisions (
                    gmail_id, module, suggested_action, final_action,
                    was_auto_executed, user_approved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.gmail_id,
                    decision.module,
                    json.dumps(decision.suggested_action),
                    json.dumps(decision.final_action),
                    1 if decision.was_auto_executed else 0,
                    1 if decision.user_approved else 0,
                    decision.created_at.isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_decisions_for_email(self, gmail_id: str) -> list[Decision]:
        """Get all decisions for an email."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM decisions WHERE gmail_id = ? ORDER BY created_at DESC",
                (gmail_id,),
            )
            return [self._row_to_decision(row) for row in cursor.fetchall()]

    def get_user_preferences(self, module: str) -> dict:
        """Get aggregated user preferences from decisions."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT suggested_action, final_action, user_approved
                FROM decisions
                WHERE module = ? AND user_approved = 1
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (module,),
            )

            preferences = {
                "preferred_actions": {},
                "rejected_actions": {},
            }

            for row in cursor.fetchall():
                action = json.loads(row["suggested_action"])
                final = json.loads(row["final_action"])

                # Track accepted suggestions
                action_key = action.get("action", "unknown")
                preferences["preferred_actions"][action_key] = (
                    preferences["preferred_actions"].get(action_key, 0) + 1
                )

            return preferences

    def _row_to_decision(self, row: sqlite3.Row) -> Decision:
        """Convert database row to Decision."""
        return Decision(
            id=row["id"],
            gmail_id=row["gmail_id"],
            module=row["module"],
            suggested_action=json.loads(row["suggested_action"]),
            final_action=json.loads(row["final_action"]),
            was_auto_executed=bool(row["was_auto_executed"]),
            user_approved=bool(row["user_approved"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ==================== Rule Operations ====================

    def add_rule(self, rule: Rule) -> int:
        """Add a new rule."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rules (condition, action, hit_count, created_from, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    json.dumps(rule.condition),
                    json.dumps(rule.action),
                    rule.hit_count,
                    rule.created_from,
                    rule.created_at.isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_matching_rules(self, email: CachedEmail) -> list[Rule]:
        """Get rules that match an email."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM rules")
            matching = []

            for row in cursor.fetchall():
                rule = self._row_to_rule(row)
                if self._rule_matches(rule, email):
                    matching.append(rule)

            return matching

    def _rule_matches(self, rule: Rule, email: CachedEmail) -> bool:
        """Check if a rule matches an email."""
        cond = rule.condition

        if "from_contains" in cond:
            if cond["from_contains"].lower() not in email.from_addr.lower():
                return False

        if "subject_contains" in cond:
            if cond["subject_contains"].lower() not in email.subject.lower():
                return False

        if "label_is" in cond:
            if cond["label_is"] not in email.labels:
                return False

        return True

    def increment_rule_hits(self, rule_id: int):
        """Increment hit count for a rule."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE rules SET hit_count = hit_count + 1 WHERE id = ?",
                (rule_id,),
            )
            conn.commit()

    def _row_to_rule(self, row: sqlite3.Row) -> Rule:
        """Convert database row to Rule."""
        return Rule(
            id=row["id"],
            condition=json.loads(row["condition"]),
            action=json.loads(row["action"]),
            hit_count=row["hit_count"],
            created_from=row["created_from"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ==================== Shaping Operations ====================

    def save_shaping_session(self, session: ShapingSession) -> int:
        """Save a shaping session."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO shaping_sessions (
                    audit_snapshot, actions_taken, result_snapshot, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    json.dumps(session.audit_snapshot),
                    json.dumps(session.actions_taken),
                    json.dumps(session.result_snapshot),
                    session.created_at.isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_shaping_history(self, limit: int = 10) -> list[ShapingSession]:
        """Get recent shaping sessions."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM shaping_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [self._row_to_shaping(row) for row in cursor.fetchall()]

    def _row_to_shaping(self, row: sqlite3.Row) -> ShapingSession:
        """Convert database row to ShapingSession."""
        return ShapingSession(
            id=row["id"],
            audit_snapshot=json.loads(row["audit_snapshot"]),
            actions_taken=json.loads(row["actions_taken"]),
            result_snapshot=json.loads(row["result_snapshot"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ==================== Sync State ====================

    def get_sync_state(self, key: str) -> Optional[str]:
        """Get sync state value."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

    def set_sync_state(self, key: str, value: str):
        """Set sync state value."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_state (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, datetime.now().isoformat()),
            )
            conn.commit()


# Global database instance
_db: Optional[Database] = None


def get_database() -> Database:
    """Get singleton Database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db
