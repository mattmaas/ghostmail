"""Archivist - Intelligent email organization."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from ..ai_engine import LLMError, get_router
from ..config import get_settings
from ..database import CachedEmail, Database, Rule, get_database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)


ARCHIVIST_SYSTEM_PROMPT = """You are an email archivist that organizes Gmail messages into a smart label hierarchy.

The user has this label structure:
- GhostMail/
  - Work/ (Active Projects, Clients, Internal, Contracts & Legal)
  - Finance/ (Banking, Invoices, Taxes, Subscriptions)
  - Personal/ (Family, Friends, Health, Travel)
  - Shopping/ (Orders & Tracking, Receipts, Returns)
  - Learning/ (Newsletters Worth Reading, Courses)
  - VIP/ (Auto-detected important senders)
  - Reference/ (Accounts & Passwords, Registrations, Warranties)

For each email, output a JSON decision:

{
  "labels": ["GhostMail/Work", "GhostMail/Finance/Invoices"],
  "confidence": 0.0-1.0,
  "reason": "why these labels apply"
}

Guidelines:
- Match sender patterns to appropriate categories when possible
- Use specific labels (e.g., "GhostMail/Finance/Invoices" not just "Finance")
- For ambiguous emails, use broader labels or multiple labels
- Consider the subject line for context
- If no clear label applies, return empty labels array

IMPORTANT: Respond with valid JSON only."""


@dataclass
class LabelSuggestion:
    """Suggested label for an email."""

    labels: list[str]
    confidence: float
    reason: str


class Archivist:
    """Intelligent email organization with dynamic label taxonomy."""

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

    async def organize_emails(
        self,
        since: Optional[str] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Organize emails with intelligent labels.

        Args:
            since: Optional date filter (YYYY-MM-DD)
            dry_run: If True, don't actually apply labels
        """
        # Build query
        query = self._build_query(since)

        # Get emails to process
        messages, _ = self.gateway.list_messages(
            query=query,
            max_results=500,
        )

        results = {
            "processed": 0,
            "labels_created": 0,
            "emails_labeled": 0,
            "dry_run": dry_run,
        }

        # Group by sender for efficiency
        sender_batches = self._group_by_sender(messages[:200])

        # Process each sender batch
        for sender, msgs in sender_batches.items():
            # Check learned rules first
            rules = self._get_rules_for_sender(sender)

            if rules:
                # Apply rules directly
                for rule in rules:
                    await self._apply_rule_to_messages(msgs, rule, dry_run)
                    results["emails_labeled"] += len(msgs)
                    self.db.increment_rule_hits(rule.id)
            else:
                # Use AI to classify
                suggestions = await self._classify_batch(sender, msgs)

                for msg, suggestion in zip(msgs, suggestions):
                    if (
                        suggestion.labels
                        and suggestion.confidence >= self.settings.auto_label_confidence
                    ):
                        if not dry_run:
                            await self._apply_labels(msg["id"], suggestion.labels)
                        results["emails_labeled"] += 1

                        # Learn from high confidence
                        if suggestion.confidence >= 0.9:
                            await self._learn_rule(sender, suggestion.labels)

                    results["processed"] += 1

        # Ensure label taxonomy exists
        if not dry_run:
            self._ensure_label_taxonomy()
            results["labels_created"] = len(self._get_standard_labels())

        return results

    def _build_query(self, since: Optional[str]) -> str:
        """Build Gmail search query."""
        parts = []

        if since:
            parts.append(f"after:{since}")
        else:
            # Default: last 2 years
            parts.append("newer_than:2y")

        # Exclude already well-organized
        parts.append("-label:GhostMail/*")

        return " ".join(parts)

    def _group_by_sender(self, messages: list[dict]) -> dict[str, list[dict]]:
        """Group messages by sender for batch processing."""
        batches = defaultdict(list)

        for msg in messages:
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                from_addr = headers.get("From", "")
                # Extract email
                if "<" in from_addr:
                    email = from_addr.split("<")[1].rstrip(">")
                else:
                    email = from_addr

                batches[email].append(msg)
            except Exception as e:
                logger.debug(f"Error processing {msg['id']}: {e}")
                continue

        return dict(batches)

    async def _classify_batch(
        self,
        sender: str,
        messages: list[dict],
    ) -> list[LabelSuggestion]:
        """Classify a batch of emails from the same sender."""
        # Get sample email details
        samples = []
        for msg in messages[:5]:  # Max 5 samples
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                samples.append(
                    {
                        "subject": headers.get("Subject", ""),
                        "snippet": full.get("snippet", ""),
                    }
                )
            except Exception:
                continue

        if not samples:
            return [LabelSuggestion(labels=[], confidence=0.0, reason="No samples")] * len(messages)

        # Build prompt
        sample_text = "\n".join(
            [f"- Subject: {s['subject']}, Snippet: {s['snippet']}" for s in samples]
        )

        messages_for_ai = [
            {
                "role": "user",
                "content": f"""Classify emails from {sender}:

{sample_text}

Output JSON array with one entry per email (use same order):""",
            }
        ]

        try:
            client = self.router.get_client(task_type="general")

            parsed, _ = await client.chat_with_json(
                messages=messages_for_ai,
                system_prompt=ARCHIVIST_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=1024,
            )

            # Parse response - could be array or single object
            if isinstance(parsed, list):
                return [
                    LabelSuggestion(
                        labels=s.get("labels", []),
                        confidence=s.get("confidence", 0.5),
                        reason=s.get("reason", ""),
                    )
                    for s in parsed
                ]
            else:
                # Single response - apply to all
                suggestion = LabelSuggestion(
                    labels=parsed.get("labels", []),
                    confidence=parsed.get("confidence", 0.5),
                    reason=parsed.get("reason", ""),
                )
                return [suggestion] * len(messages)

        except LLMError as e:
            logger.warning(f"AI classification failed: {e}")
            # Return default suggestions
            return [LabelSuggestion(labels=[], confidence=0.0, reason="AI failed")] * len(messages)

    def _get_rules_for_sender(self, sender: str) -> list[Rule]:
        """Get learned rules for a sender."""
        # Create a dummy email for matching
        email = CachedEmail(
            gmail_id="",
            thread_id="",
            from_addr=sender,
            to_addr="",
            subject="",
            snippet="",
            date="",
            labels=[],
            size_bytes=0,
            is_read=False,
        )

        return self.db.get_matching_rules(email)

    async def _apply_rule_to_messages(
        self,
        messages: list[dict],
        rule: Rule,
        dry_run: bool,
    ):
        """Apply a rule to messages."""
        if dry_run:
            return

        action = rule.action
        label_names = action.get("labels", [])

        if not label_names:
            return

        # Get label IDs
        label_ids = []
        for label_name in label_names:
            label = self.gateway.get_or_create_label(label_name)
            label_ids.append(label["id"])

        # Batch apply
        msg_ids = [m["id"] for m in messages]
        self.gateway.batch_modify_messages(msg_ids, add_label_ids=label_ids)

    async def _apply_labels(self, msg_id: str, label_names: list[str]):
        """Apply labels to a message."""
        label_ids = []
        for label_name in label_names:
            label = self.gateway.get_or_create_label(label_name)
            label_ids.append(label["id"])

        self.gateway.modify_message(msg_id, add_label_ids=label_ids)

    async def _learn_rule(self, sender: str, label_names: list[str]):
        """Learn a rule from high-confidence classification."""
        rule = Rule(
            condition={"from_contains": sender},
            action={"labels": label_names},
            hit_count=0,
            created_from="learned",
        )
        self.db.add_rule(rule)
        logger.info(f"Learned rule: {sender} -> {label_names}")

    def _ensure_label_taxonomy(self):
        """Ensure standard label hierarchy exists."""
        for label_name in self._get_standard_labels():
            try:
                self.gateway.get_or_create_label(label_name)
            except Exception as e:
                logger.debug(f"Failed to create label {label_name}: {e}")

    def _get_standard_labels(self) -> list[str]:
        """Get standard label hierarchy."""
        return [
            "GhostMail/Work/Active Projects",
            "GhostMail/Work/Clients",
            "GhostMail/Work/Internal",
            "GhostMail/Work/Contracts & Legal",
            "GhostMail/Finance/Banking",
            "GhostMail/Finance/Invoices",
            "GhostMail/Finance/Taxes",
            "GhostMail/Finance/Subscriptions",
            "GhostMail/Personal/Family",
            "GhostMail/Personal/Friends",
            "GhostMail/Personal/Health",
            "GhostMail/Personal/Travel",
            "GhostMail/Shopping/Orders & Tracking",
            "GhostMail/Shopping/Receipts",
            "GhostMail/Shopping/Returns",
            "GhostMail/Learning/Newsletters Worth Reading",
            "GhostMail/Learning/Courses",
            "GhostMail/VIP",
            "GhostMail/Reference/Accounts & Passwords",
            "GhostMail/Reference/Registrations",
            "GhostMail/Reference/Warranties",
        ]

    async def retroactive_organize(
        self,
        years_back: int = 2,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Organize entire mailbox history (retroactive)."""
        # Calculate date
        cutoff = (datetime.now() - timedelta(days=years_back * 365)).strftime("%Y-%m-%d")

        return await self.organize_emails(since=cutoff, dry_run=dry_run)
