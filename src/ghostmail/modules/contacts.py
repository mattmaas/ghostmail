"""Contact Intelligence - Unified contact graph for BizMail."""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, List, Optional
import aiosqlite
from pathlib import Path

from ..config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Company:
    id: Optional[int]
    name: str
    domain: Optional[str]
    industry: Optional[str]
    size: Optional[str]
    ats_type: Optional[str]
    board_url: Optional[str]
    relationship: str
    funding_stage: Optional[str]
    tech_stack: Optional[str]
    last_interaction: Optional[str]
    interaction_count: int


@dataclass
class Contact:
    id: Optional[int]
    company_id: Optional[int]
    name: str
    email: str
    phone: Optional[str]
    linkedin_url: Optional[str]
    role_title: Optional[str]
    contact_type: str
    source: str
    first_seen: str
    last_interaction: str
    interaction_count: int
    sentiment_avg: float
    warmth_score: float
    notes: Optional[str]


@dataclass
class Interaction:
    id: Optional[int]
    contact_id: Optional[int]
    company_id: Optional[int]
    interaction_type: str
    timestamp: str
    channel: str
    summary: str
    email_id: Optional[str]
    job_id: Optional[str]
    sentiment: Optional[float]


class ContactIntelligence:
    """Manages the unified contact graph (BizMail + job-auto)."""

    def __init__(self, db_path: Optional[Path] = None):
        self.settings = get_settings()
        self.db_path = db_path or self.settings.data_dir_expanded / "contacts.db"
        self._conn = None

    async def init_db(self):
        """Initialize the database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                domain TEXT,
                industry TEXT,
                size TEXT,
                ats_type TEXT,
                board_url TEXT,
                relationship TEXT DEFAULT 'prospect',
                funding_stage TEXT,
                tech_stack TEXT,
                last_interaction DATETIME,
                interaction_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                name TEXT NOT NULL,
                email TEXT UNIQUE,
                phone TEXT,
                linkedin_url TEXT,
                role_title TEXT,
                contact_type TEXT DEFAULT 'prospect',
                source TEXT DEFAULT 'email',
                first_seen DATETIME,
                last_interaction DATETIME,
                interaction_count INTEGER DEFAULT 0,
                sentiment_avg REAL DEFAULT 0.0,
                warmth_score REAL DEFAULT 0.0,
                notes TEXT,
                FOREIGN KEY (company_id) REFERENCES companies (id)
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER,
                company_id INTEGER,
                interaction_type TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                channel TEXT NOT NULL,
                summary TEXT,
                email_id TEXT,
                job_id TEXT,
                sentiment REAL,
                FOREIGN KEY (contact_id) REFERENCES contacts (id),
                FOREIGN KEY (company_id) REFERENCES companies (id)
            );

            CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
            CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions(contact_id);
            CREATE INDEX IF NOT EXISTS idx_interactions_company ON interactions(company_id);
        """)
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    # --- Companies ---

    async def upsert_company(self, company: Company) -> int:
        """Insert or update a company."""
        await self.init_db()
        if not self._conn:
            raise RuntimeError("Database connection not initialized")
        cursor = await self._conn.execute(
            "SELECT id, interaction_count FROM companies WHERE name = ?", (company.name,)
        )
        existing = await cursor.fetchone()

        if existing:
            # Update
            await self._conn.execute(
                """
                UPDATE companies SET
                    domain = COALESCE(?, domain),
                    industry = COALESCE(?, industry),
                    size = COALESCE(?, size),
                    ats_type = COALESCE(?, ats_type),
                    board_url = COALESCE(?, board_url),
                    relationship = COALESCE(?, relationship),
                    funding_stage = COALESCE(?, funding_stage),
                    tech_stack = COALESCE(?, tech_stack),
                    last_interaction = COALESCE(?, last_interaction)
                WHERE id = ?
            """,
                (
                    company.domain,
                    company.industry,
                    company.size,
                    company.ats_type,
                    company.board_url,
                    company.relationship,
                    company.funding_stage,
                    company.tech_stack,
                    company.last_interaction,
                    existing["id"],
                ),
            )
            await self._conn.commit()
            return int(existing["id"])
        else:
            # Insert
            cursor = await self._conn.execute(
                """
                INSERT INTO companies (
                    name, domain, industry, size, ats_type, board_url,
                    relationship, funding_stage, tech_stack, last_interaction, interaction_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    company.name,
                    company.domain,
                    company.industry,
                    company.size,
                    company.ats_type,
                    company.board_url,
                    company.relationship,
                    company.funding_stage,
                    company.tech_stack,
                    company.last_interaction,
                    company.interaction_count,
                ),
            )
            await self._conn.commit()
            return int(cursor.lastrowid) if cursor.lastrowid else 0

    # --- Contacts ---

    async def upsert_contact(self, contact: Contact) -> int:
        """Insert or update a contact."""
        await self.init_db()
        if not self._conn:
            raise RuntimeError("Database connection not initialized")
        cursor = await self._conn.execute(
            "SELECT id, interaction_count FROM contacts WHERE email = ?", (contact.email,)
        )
        existing = await cursor.fetchone()

        if existing:
            # Update
            await self._conn.execute(
                """
                UPDATE contacts SET
                    company_id = COALESCE(?, company_id),
                    name = COALESCE(?, name),
                    phone = COALESCE(?, phone),
                    linkedin_url = COALESCE(?, linkedin_url),
                    role_title = COALESCE(?, role_title),
                    contact_type = COALESCE(?, contact_type),
                    last_interaction = COALESCE(?, last_interaction),
                    warmth_score = COALESCE(?, warmth_score),
                    notes = COALESCE(?, notes)
                WHERE id = ?
            """,
                (
                    contact.company_id,
                    contact.name,
                    contact.phone,
                    contact.linkedin_url,
                    contact.role_title,
                    contact.contact_type,
                    contact.last_interaction,
                    contact.warmth_score,
                    contact.notes,
                    existing["id"],
                ),
            )
            await self._conn.commit()
            return int(existing["id"])
        else:
            # Insert
            cursor = await self._conn.execute(
                """
                INSERT INTO contacts (
                    company_id, name, email, phone, linkedin_url, role_title,
                    contact_type, source, first_seen, last_interaction,
                    interaction_count, sentiment_avg, warmth_score, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    contact.company_id,
                    contact.name,
                    contact.email,
                    contact.phone,
                    contact.linkedin_url,
                    contact.role_title,
                    contact.contact_type,
                    contact.source,
                    contact.first_seen,
                    contact.last_interaction,
                    contact.interaction_count,
                    contact.sentiment_avg,
                    contact.warmth_score,
                    contact.notes,
                ),
            )
            await self._conn.commit()
            return int(cursor.lastrowid) if cursor.lastrowid else 0

    # --- Interactions ---

    async def log_interaction(self, interaction: Interaction) -> int:
        """Log a new interaction and update aggregate counts."""
        await self.init_db()
        if not self._conn:
            raise RuntimeError("Database connection not initialized")
        cursor = await self._conn.execute(
            """
            INSERT INTO interactions (
                contact_id, company_id, interaction_type, timestamp,
                channel, summary, email_id, job_id, sentiment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                interaction.contact_id,
                interaction.company_id,
                interaction.interaction_type,
                interaction.timestamp,
                interaction.channel,
                interaction.summary,
                interaction.email_id,
                interaction.job_id,
                interaction.sentiment,
            ),
        )
        interaction_id = cursor.lastrowid

        # Update counts
        if interaction.contact_id:
            await self._conn.execute(
                """
                UPDATE contacts SET
                    interaction_count = interaction_count + 1,
                    last_interaction = ?
                WHERE id = ?
            """,
                (interaction.timestamp, interaction.contact_id),
            )

        if interaction.company_id:
            await self._conn.execute(
                """
                UPDATE companies SET
                    interaction_count = interaction_count + 1,
                    last_interaction = ?
                WHERE id = ?
            """,
                (interaction.timestamp, interaction.company_id),
            )

        await self._conn.commit()
        return int(interaction_id) if interaction_id else 0

    async def get_warm_companies(self, limit: int = 10) -> List[dict]:
        """Get companies ordered by interaction count/recency."""
        await self.init_db()
        if not self._conn:
            raise RuntimeError("Database connection not initialized")
        cursor = await self._conn.execute(
            """
            SELECT * FROM companies
            ORDER BY interaction_count DESC, last_interaction DESC
            LIMIT ?
        """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_contacts_for_company(self, company_id: int) -> List[dict]:
        """Get all contacts for a specific company."""
        await self.init_db()
        if not self._conn:
            raise RuntimeError("Database connection not initialized")
        cursor = await self._conn.execute(
            "SELECT * FROM contacts WHERE company_id = ?", (company_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
