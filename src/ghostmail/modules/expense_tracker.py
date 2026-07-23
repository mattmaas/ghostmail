"""Expense Tracker - Extract business expenses for tax prep."""

import csv
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, List, Optional
from pathlib import Path

from ..ai_engine import LLMError, get_router
from ..config import get_settings
from ..database import Database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)

EXPENSE_SYSTEM_PROMPT = """You are an expert tax accountant and expense extractor.
Analyze the email text and extract receipt/invoice details.
Determine if this is a business expense for a software/AI startup.

Extract into this exact JSON schema:
{
  "vendor": "Company Name",
  "amount": 20.00,
  "currency": "USD",
  "date": "YYYY-MM-DD",
  "category": "startup_costs" | "organizational_costs" | "capital_assets" | "operating_expenses" | "not_business",
  "subcategory": "string (e.g. ai_cloud_services)",
  "description": "brief description of what was purchased",
  "confidence": 0.95,
  "deductible": true,
  "irs_section": "195",
  "notes": "any tax notes"
}

Categories explained:
- startup_costs (195): market research, training, software, API keys bought BEFORE formation.
- organizational_costs (248): filing fees, legal fees, publication costs.
- capital_assets (179): hardware, equipment (depreciation candidates).
- operating_expenses: ongoing subscriptions, cloud services AFTER formation.
- not_business: personal expenses (Netflix, groceries, personal Amazon).

Return ONLY the JSON object.
"""


@dataclass
class ExpenseRecord:
    vendor: str
    amount: float
    currency: str
    date: str
    category: str
    subcategory: str
    description: str
    formation_status: str
    confidence: float
    source_email_id: str
    source_subject: str
    deductible: bool
    irs_section: str
    notes: str


class ExpenseTracker:
    """Extracts and tracks business expenses from Gmail."""

    FORMATION_DATE = "2026-02-25"

    def __init__(self, gateway: GmailGateway, router, db: Database):
        self.gateway = gateway
        self.router = router
        self.db = db
        self.settings = get_settings()

    def get_search_queries(self) -> dict[str, str]:
        """Return search queries for different expense categories."""
        return {
            "ai_services": "from:(openai.com OR anthropic.com OR deepseek.com OR together.ai OR replicate.com OR huggingface.co OR aws.amazon.com OR cloud.google.com OR azure.microsoft.com OR digitalocean.com OR vercel.com OR netlify.com OR cloudflare.com OR github.com OR railway.app OR render.com OR fly.io)",
            "hardware": "subject:(order confirmation OR shipping confirmation OR receipt) from:(amazon.com OR newegg.com OR bestbuy.com OR bhphoto.com OR apple.com OR dell.com OR lenovo.com OR adorama.com)",
            "software": "subject:(subscription OR invoice OR receipt OR payment OR billing) from:(stripe.com OR paypal.com OR gumroad.com OR paddle.com)",
            "domain_hosting": "from:(namecheap.com OR godaddy.com OR cloudflare.com OR hover.com OR google.com) subject:(domain OR hosting)",
            "education": "from:(udemy.com OR coursera.org OR pluralsight.com OR oreilly.com OR linkedin.com) subject:(receipt OR enrollment OR course)",
        }

    async def scan_expenses(
        self,
        category: Optional[str] = None,
        before_date: Optional[str] = None,
        after_date: Optional[str] = None,
        max_results: int = 100,
    ) -> List[ExpenseRecord]:
        """Scan mailbox for expenses."""
        queries = self.get_search_queries()

        if category and category in queries:
            search_query = queries[category]
        else:
            # Combine all queries
            search_query = " OR ".join(f"({q})" for q in queries.values())

        if before_date:
            search_query += f" before:{before_date}"
        if after_date:
            search_query += f" after:{after_date}"

        logger.info(f"Scanning with query: {search_query[:100]}...")

        messages, _ = self.gateway.list_messages(query=search_query, max_results=max_results)

        expenses = []
        for msg in messages:
            try:
                full_msg = self.gateway.get_message(msg["id"], format="full")
                expense = await self._analyze_email_for_expense(full_msg)
                if expense and expense.confidence > 0.6 and expense.category != "not_business":
                    expenses.append(expense)
            except Exception as e:
                logger.error(f"Error processing email {msg['id']}: {e}")

        return expenses

    async def _analyze_email_for_expense(self, email_data: dict) -> Optional[ExpenseRecord]:
        """Use AI to extract expense details from email."""
        headers = {h["name"]: h["value"] for h in email_data.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")

        # Simple extraction of text body
        body_text = ""
        payload = email_data.get("payload", {})
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    import base64

                    if "data" in part["body"]:
                        body_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                        break
        elif payload.get("mimeType") == "text/plain":
            import base64

            if "data" in payload["body"]:
                body_text = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")

        if not body_text:
            body_text = email_data.get("snippet", "")

        if not body_text or len(body_text) < 20:
            return None

        # Truncate text to avoid token limits
        body_text = body_text[:3000]

        messages = [{"role": "user", "content": f"Subject: {subject}\n\nEmail body:\n{body_text}"}]

        try:
            client = self.router.get_client(task_type="reasoning", prefer_reasoning=False)
            parsed, _ = await client.chat_with_json(
                messages=messages,
                system_prompt=EXPENSE_SYSTEM_PROMPT,
                temperature=0.1,
            )

            # Determine pre/post formation
            expense_date = parsed.get("date", "")
            formation_status = "unknown"
            if expense_date:
                try:
                    if expense_date < self.FORMATION_DATE:
                        formation_status = "pre_formation"
                        if parsed.get("category") == "operating_expenses":
                            parsed["category"] = "startup_costs"
                    else:
                        formation_status = "post_formation"
                except Exception:
                    pass

            return ExpenseRecord(
                vendor=parsed.get("vendor", "Unknown"),
                amount=float(parsed.get("amount", 0.0)),
                currency=parsed.get("currency", "USD"),
                date=expense_date,
                category=parsed.get("category", "not_business"),
                subcategory=parsed.get("subcategory", ""),
                description=parsed.get("description", ""),
                formation_status=formation_status,
                confidence=float(parsed.get("confidence", 0.0)),
                source_email_id=email_data["id"],
                source_subject=subject,
                deductible=bool(parsed.get("deductible", False)),
                irs_section=parsed.get("irs_section", ""),
                notes=parsed.get("notes", ""),
            )
        except LLMError as e:
            logger.debug(f"Failed to extract expense: {e}")
            return None

    def export_csv(self, expenses: List[ExpenseRecord], filepath: str):
        """Export expenses to CSV."""
        if not expenses:
            return

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=asdict(expenses[0]).keys())
            writer.writeheader()
            for exp in expenses:
                writer.writerow(asdict(exp))

    def export_json(self, expenses: List[ExpenseRecord], filepath: str):
        """Export expenses to JSON."""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([asdict(exp) for exp in expenses], f, indent=2)

    def print_summary(self, expenses: List[ExpenseRecord]):
        """Print a summary of expenses."""
        totals = {}
        for exp in expenses:
            totals[exp.category] = totals.get(exp.category, 0.0) + exp.amount

        print("\nExpense Summary:")
        print("================")
        for cat, total in totals.items():
            print(f"{cat}: ${total:.2f}")
        print(f"Total: ${sum(totals.values()):.2f}")
