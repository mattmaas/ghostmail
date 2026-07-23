"""Political unsubscribe module - Identify and unsubscribe from political campaign emails."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from ..ai_engine import LLMError, get_router
from ..config import get_settings
from ..database import Database, get_database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)


POLITICAL_KEYWORDS = [
    # Campaign terms
    "campaign",
    "election",
    "vote",
    "ballot",
    "candidate",
    "midterms",
    "congress",
    "senate",
    "house",
    "gubernatorial",
    "primaries",
    "polling",
    "polls",
    "electoral",
    " precinct",
    " GOTV",
    "get out the vote",
    # Political organizations
    "democratic",
    "republican",
    "libertarian",
    "green party",
    "PAC",
    "super PAC",
    "political action committee",
    "DNC",
    "RNC",
    "GOP",
    # Fundraising
    "contribute",
    "donation",
    "donate",
    "campaign contribution",
    "support our campaign",
    "help us win",
    "fight back",
    # Generic political
    "political",
    "politics",
    "voted",
    "voter",
]


# Common political senders (domains)
POLITICAL_DOMAINS = [
    "actblue.com",
    "winred.com",
    "democratic",
    "republican",
    "gop.com",
    "dccc.org",
    "nrcctp.org",
    "victory.org",
    "democrats.org",
    "berniesanders",
    "ocasiocortez",
    "campaign",
]


@dataclass
class PoliticalEmail:
    """A political email identified for potential unsubscription."""

    gmail_id: str
    from_email: str
    from_name: str
    subject: str
    unsubscribe_url: Optional[str]
    date: str


class PoliticalUnsubModule:
    """Identify and unsubscribe from political campaign emails."""

    def __init__(
        self,
        gateway: GmailGateway,
        router,  # ModelRouter
        db: Database,
    ):
        self.gateway = gateway
        self.router = router
        self.db = db
        self.settings = get_settings()

    async def find_political_emails(
        self,
        max_emails: int = 200,
    ) -> dict[str, Any]:
        """
        Find political emails in the inbox.

        Returns summary of found political emails.
        """
        # Search for likely political emails
        political_senders = []

        # Strategy 1: Search by common political keywords
        keyword_query = " OR ".join([f"subject:{kw}" for kw in POLITICAL_KEYWORDS[:10]])

        messages, _ = self.gateway.list_messages(
            query=f"({keyword_query}) newer_than:1y",
            max_results=max_emails,
        )

        # Also get recent political senders
        all_messages, _ = self.gateway.list_messages(
            query="newer_than:1y",
            max_results=1000,
        )

        # Combine messages
        seen_ids = set()
        combined_messages = []
        for msg in messages + all_messages:
            if msg["id"] not in seen_ids:
                seen_ids.add(msg["id"])
                combined_messages.append(msg)

        # Analyze senders for political content
        sender_scores = {}

        for msg in combined_messages:
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                from_addr = headers.get("From", "")
                if "<" in from_addr:
                    email = from_addr.split("<")[1].rstrip(">")
                    name = from_addr.split("<")[0].strip().rstrip(">")
                else:
                    email = from_addr
                    name = ""

                # Score this sender
                sender_lower = from_addr.lower()

                # Check keywords in name/email
                score = sum(1 for kw in POLITICAL_KEYWORDS if kw.lower() in sender_lower)

                # Check known domains
                if any(domain in sender_lower for domain in POLITICAL_DOMAINS):
                    score += 5  # High confidence if domain matches

                if score > 0:
                    if email not in sender_scores:
                        sender_scores[email] = {"name": name, "score": 0, "count": 0}
                    sender_scores[email]["score"] += score
                    sender_scores[email]["count"] += 1

            except Exception:
                continue

        # Sort by score
        political_senders = sorted(
            sender_scores.items(), key=lambda x: x[1]["score"] * x[1]["count"], reverse=True
        )[:30]

        return {
            "total_political_senders": len(political_senders),
            "senders": [
                {
                    "email": email,
                    "name": data["name"],
                    "score": data["score"],
                    "count": data["count"],
                }
                for email, data in political_senders
            ],
        }

    async def unsubscribe_from_sender(
        self,
        sender_email: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Unsubscribe from a specific sender.

        Args:
            sender_email: Email address to unsubscribe from
            dry_run: If True, don't actually unsubscribe

        Returns:
            Result of unsubscription attempt
        """
        # Find recent emails from this sender
        messages, _ = self.gateway.list_messages(
            query=f"from:{sender_email}",
            max_results=10,
        )

        if not messages:
            return {
                "success": False,
                "reason": "No emails found from this sender",
                "sender": sender_email,
            }

        # Try to find unsubscribe URL in most recent email
        unsubscribe_url = None

        for msg in messages[:3]:
            try:
                full = self.gateway.get_message(msg["id"], format="full")
                unsubscribe_url = self._find_unsubscribe_url(full)
                if unsubscribe_url:
                    break
            except Exception:
                continue

        if not unsubscribe_url:
            return {
                "success": False,
                "reason": "No unsubscribe link found",
                "sender": sender_email,
                "suggestion": "Try marking emails as spam instead",
            }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "unsubscribe_url": unsubscribe_url,
                "sender": sender_email,
                "message": "Would have clicked unsubscribe link",
            }

        if unsubscribe_url.startswith("mailto:"):
            return {
                "success": False,
                "error": "Unsubscribe requires sending an email (mailto link). Not supported yet.",
                "unsubscribe_url": unsubscribe_url,
                "sender": sender_email,
            }

        # Actually try to unsubscribe
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(unsubscribe_url)

                result = {
                    "success": response.status_code < 400,
                    "status_code": response.status_code,
                    "unsubscribe_url": unsubscribe_url,
                    "sender": sender_email,
                }

                if not result["success"]:
                    result["error"] = f"HTTP Error {response.status_code}"

                return result
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "unsubscribe_url": unsubscribe_url,
                "sender": sender_email,
            }

    async def bulk_unsubscribe(
        self,
        sender_emails: list[str],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Unsubscribe from multiple senders.

        Args:
            sender_emails: List of email addresses to unsubscribe from
            dry_run: If True, don't actually unsubscribe

        Returns:
            Summary of bulk unsubscription results
        """
        results = {
            "total": len(sender_emails),
            "successful": 0,
            "failed": 0,
            "dry_run": dry_run,
            "details": [],
        }

        for sender in sender_emails:
            result = await self.unsubscribe_from_sender(sender, dry_run=dry_run)

            if result.get("success"):
                results["successful"] += 1
            else:
                results["failed"] += 1

            results["details"].append(result)

            # Rate limiting
            await asyncio.sleep(1)

        return results

    async def auto_identify_and_unsubscribe(
        self,
        dry_run: bool = True,
        min_score: int = 3,
    ) -> dict[str, Any]:
        """
        Automatically identify political senders and unsubscribe.

        Args:
            dry_run: If True, don't actually unsubscribe
            min_score: Minimum political score to consider

        Returns:
            Summary of actions taken
        """
        # Find political emails
        political_data = await self.find_political_emails(max_emails=200)

        # Filter to high-confidence political senders
        targets = [s["email"] for s in political_data.get("senders", []) if s["score"] >= min_score]

        if not targets:
            return {
                "message": "No political senders found above threshold",
                "senders_found": political_data.get("total_political_senders", 0),
            }

        # Unsubscribe from each
        results = await self.bulk_unsubscribe(targets, dry_run=dry_run)

        # Fallback to spam if unsubscribe fails
        if not dry_run:
            failed_targets = [
                detail["sender"]
                for detail in results.get("details", [])
                if not detail.get("success")
            ]
            if failed_targets:
                spam_results = await self.mark_as_spam(failed_targets)
                results["spam_fallback"] = spam_results

        return {
            "identified_senders": len(targets),
            "results": results,
        }

    def _find_unsubscribe_url(self, message: dict) -> Optional[str]:
        """Extract unsubscribe URL from email headers or body."""
        # Check headers first
        headers = message.get("payload", {}).get("headers", [])

        for header in headers:
            if header.get("name", "").lower() == "list-unsubscribe":
                value = header.get("value", "")

                # First try to find an http/https URL
                http_match = re.search(r"<(https?://.+?)>", value)
                if http_match:
                    return http_match.group(1)

                # Fallback to mailto if it's the only option
                mailto_match = re.search(r"<(mailto:.+?)>", value)
                if mailto_match:
                    return mailto_match.group(1)

        # Try to find in body (simplified - real implementation would parse HTML)
        # This is a placeholder - real parsing would need proper email body extraction
        return None

    async def mark_as_spam(
        self,
        sender_emails: list[str],
    ) -> dict[str, Any]:
        """
        Mark emails from sender as spam (fallback if unsubscribe not available).

        Args:
            sender_emails: List of sender emails to mark as spam

        Returns:
            Result of spam marking
        """
        results = {"marked": 0, "failed": 0}

        for sender in sender_emails:
            try:
                messages, _ = self.gateway.list_messages(
                    query=f"from:{sender}",
                    max_results=50,
                )

                for msg in messages[:50]:
                    # Add SPAM label
                    self.gateway.modify_message(
                        msg["id"],
                        add_label_ids=["SPAM"],
                    )
                    results["marked"] += 1

            except Exception as e:
                logger.debug(f"Failed to mark {sender} as spam: {e}")
                results["failed"] += 1

        return results
