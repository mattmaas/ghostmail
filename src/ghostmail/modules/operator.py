"""Operator - AI-powered email triage."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..ai_engine import LLMError, ModelProvider, get_router
from ..config import get_settings
from ..database import CachedEmail, Database, Decision, get_database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)


TRIAGE_SYSTEM_PROMPT = """You are an email triage assistant for Gmail.

For each email, analyze and output a JSON decision:

{
  "action": "archive" | "delete" | "respond" | "flag" | "label" | "keep_inbox",
  "priority": "urgent" | "important" | "normal" | "low",
  "labels": ["label_name"],  // e.g., "GhostMail/Work", "GhostMail/Finance"
  "reason": "brief justification",
  "draft_response": "..."  // only if action is "respond"
  "confidence": 0.0-1.0
}

Guidelines:
- If confidence < 0.7, use "flag" action for human review
- Never suggest "respond" for newsletters, promotions, or automated emails
- Classify newsletters and promotional emails as "low" priority
- For action "label", provide appropriate labels from: Work, Finance, Personal, Shopping, Learning, VIP, Reference
- Learn from user's past preferences (provided in context)
- Be conservative - when in doubt, flag for review

IMPORTANT: Respond with valid JSON only. No explanations."""


@dataclass
class EmailAnalysis:
    """Analysis result for an email."""

    email: CachedEmail
    action: str
    priority: str
    labels: list[str]
    reason: str
    confidence: float
    draft_response: str = ""


class Operator:
    """AI-powered email triage operator."""

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

    async def triage_inbox(
        self,
        limit: int = 10,
        auto_execute: bool = False,
    ) -> dict[str, Any]:
        """
        Triage recent inbox emails.

        Returns summary of actions taken/suggested.
        """
        # Fetch recent inbox emails
        messages, _ = self.gateway.list_messages(
            query="in:inbox is:unread",
            max_results=limit,
        )

        results = {
            "auto_labeled": 0,
            "need_review": 0,
            "flagged": 0,
            "review_emails": [],
        }

        for msg in messages:
            try:
                # Get full message
                full = self.gateway.get_message(msg["id"], format="full")

                # Extract headers
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                # Create email object
                email = CachedEmail(
                    gmail_id=full["id"],
                    thread_id=full.get("threadId", ""),
                    from_addr=headers.get("From", ""),
                    to_addr=headers.get("To", ""),
                    subject=headers.get("Subject", ""),
                    snippet=full.get("snippet", ""),
                    date=headers.get("Date", ""),
                    labels=full.get("labelIds", []),
                    size_bytes=full.get("sizeEstimate", 0),
                    is_read=True,  # We're processing it
                )

                # Check for sensitive content
                content = f"{email.subject} {email.snippet}"
                if self._is_sensitive(content):
                    # Route to privacy-safe processing
                    analysis = await self._triage_with_local(email)
                else:
                    # Use AI
                    analysis = await self._triage_with_ai(email)

                # Decide what to do
                if (
                    analysis.confidence >= self.settings.auto_archive_confidence
                    and analysis.action == "archive"
                ):
                    if auto_execute:
                        await self._execute_action(email, analysis)
                        results["auto_labeled"] += 1
                elif analysis.confidence < 0.7 or analysis.action == "flag":
                    results["flagged"] += 1
                    results["review_emails"].append(
                        {
                            "gmail_id": email.gmail_id,
                            "subject": email.subject,
                            "from": email.from_addr,
                            "suggested_action": analysis.action,
                            "confidence": analysis.confidence,
                        }
                    )
                else:
                    results["need_review"] += 1
                    results["review_emails"].append(
                        {
                            "gmail_id": email.gmail_id,
                            "subject": email.subject,
                            "from": email.from_addr,
                            "suggested_action": analysis.action,
                            "confidence": analysis.confidence,
                        }
                    )

                # Record decision
                decision = Decision(
                    gmail_id=email.gmail_id,
                    module="operator",
                    suggested_action={
                        "action": analysis.action,
                        "priority": analysis.priority,
                        "labels": analysis.labels,
                        "confidence": analysis.confidence,
                    },
                    final_action={},
                    was_auto_executed=False,
                    user_approved=False,
                )
                self.db.add_decision(decision)

            except Exception as e:
                logger.error(f"Error processing {msg['id']}: {e}")
                continue

        return results

    async def _triage_with_ai(self, email: CachedEmail) -> EmailAnalysis:
        """Use AI to triage an email."""
        # Get user preferences for learning
        preferences = self.db.get_user_preferences("operator")

        # Build context
        messages = [
            {
                "role": "user",
                "content": f"""Analyze this email:

From: {email.from_addr}
To: {email.to_addr}
Subject: {email.subject}
Snippet: {email.snippet}

Labels currently: {", ".join(email.labels)}

User preferences: {preferences}

Output your decision as JSON:""",
            }
        ]

        try:
            # Get appropriate client
            client = self.router.get_client(task_type="general")

            parsed, response = await client.chat_with_json(
                messages=messages,
                system_prompt=TRIAGE_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=512,
            )

            return EmailAnalysis(
                email=email,
                action=parsed.get("action", "keep_inbox"),
                priority=parsed.get("priority", "normal"),
                labels=parsed.get("labels", []),
                reason=parsed.get("reason", ""),
                confidence=parsed.get("confidence", 0.5),
                draft_response=parsed.get("draft_response", ""),
            )

        except LLMError as e:
            logger.warning(f"AI triage failed, using fallback: {e}")
            return await self._triage_fallback(email)

    async def _triage_with_local(self, email: CachedEmail) -> EmailAnalysis:
        """Use local-only processing for sensitive emails."""
        # Simple rule-based fallback
        subject_lower = email.subject.lower()
        from_lower = email.from_addr.lower()

        # Check for known patterns
        if any(x in from_lower for x in ["newsletter", "promo", "deal", "offer"]):
            return EmailAnalysis(
                email=email,
                action="archive",
                priority="low",
                labels=["GhostMail/Shopping"],
                reason="Promotional email",
                confidence=0.9,
            )

        if any(x in subject_lower for x in ["urgent", "asap", "important"]):
            return EmailAnalysis(
                email=email,
                action="flag",
                priority="urgent",
                labels=[],
                reason="Potentially urgent",
                confidence=0.6,
            )

        return EmailAnalysis(
            email=email,
            action="keep_inbox",
            priority="normal",
            labels=[],
            reason="No clear action needed",
            confidence=0.5,
        )

    async def _triage_fallback(self, email: CachedEmail) -> EmailAnalysis:
        """Fallback triage when AI is unavailable."""
        return await self._triage_with_local(email)

    def _is_sensitive(self, content: str) -> bool:
        """Check if content contains sensitive keywords."""
        content_lower = content.lower()
        return any(keyword in content_lower for keyword in self.settings.sensitive_keywords)

    async def _execute_action(self, email: CachedEmail, analysis: EmailAnalysis):
        """Execute the triage action on Gmail."""
        if analysis.action == "archive":
            self.gateway.modify_message(
                email.gmail_id,
                remove_label_ids=["INBOX"],
            )
        elif analysis.action == "label" and analysis.labels:
            add_labels = []
            for label_name in analysis.labels:
                label = self.gateway.get_or_create_label(label_name)
                add_labels.append(label["id"])

            self.gateway.modify_message(
                email.gmail_id,
                add_label_ids=add_labels,
            )

        logger.info(f"Executed {analysis.action} on {email.gmail_id}")
