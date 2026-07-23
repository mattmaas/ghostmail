"""Research module - Analyze emails to research topics."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..ai_engine import LLMError, get_router
from ..config import get_settings
from ..database import Database, get_database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)


RESEARCH_SYSTEM_PROMPT = """You are an email research assistant. Analyze the user's emails to answer research questions about a topic.

For each research query:
1. Extract relevant information from the emails
2. Identify key themes, patterns, and insights
3. Provide a comprehensive summary

Output your analysis as JSON:

{
  "summary": "2-3 sentence overview of what the emails reveal about this topic",
  "key_findings": [
    {"finding": "specific insight", "evidence": "email snippet or reference", "date": "approx date"}
  ],
  "entities": {"people": [], "organizations": [], "topics": []},
  "timeline": [{"date": "when", "event": "what happened"}],
  "sentiment": "positive | negative | neutral | mixed",
  "confidence": 0.0-1.0
}

Guidelines:
- Be specific with dates and evidence
- Only include findings supported by email evidence
- If emails don't contain relevant information, say so
- Respect privacy - don't reveal sensitive personal details

IMPORTANT: Respond with valid JSON only."""


@dataclass
class ResearchResult:
    """Research result from email analysis."""

    summary: str
    key_findings: list[dict]
    entities: dict
    timeline: list[dict]
    sentiment: str
    confidence: float
    emails_analyzed: int


class ResearchModule:
    """Research emails for a specific topic."""

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

    async def research(
        self,
        query: str,
        max_emails: int = 50,
        use_local: bool = False,
    ) -> dict[str, Any]:
        """
        Research a topic using emails.

        Args:
            query: Research topic/question
            max_emails: Maximum emails to analyze
            use_local: Force local processing (privacy mode)

        Returns:
            Research results dictionary
        """
        # First, search for relevant emails
        emails = await self._search_emails(query, max_emails)

        if not emails:
            return {
                "query": query,
                "summary": "No emails found matching this topic.",
                "emails_found": 0,
                "emails_analyzed": 0,
            }

        # Analyze emails
        if use_local:
            result = await self._analyze_locally(emails, query)
        else:
            result = await self._analyze_with_ai(emails, query)

        result["query"] = query
        result["emails_found"] = len(emails)

        return result

    async def _search_emails(
        self,
        query: str,
        max_emails: int,
    ) -> list[dict]:
        """Search Gmail for relevant emails."""
        # Build search query
        search_query = self._build_search_query(query)

        messages, _ = self.gateway.list_messages(
            query=search_query,
            max_results=max_emails,
        )

        # Get full content for each message
        emails = []
        for msg in messages:
            try:
                full = self.gateway.get_message(msg["id"], format="full")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                # Get body text
                body = self._extract_body(full)

                emails.append(
                    {
                        "id": full["id"],
                        "from": headers.get("From", ""),
                        "to": headers.get("To", ""),
                        "subject": headers.get("Subject", ""),
                        "date": headers.get("Date", ""),
                        "body": body[:2000],  # Limit body size
                        "snippet": full.get("snippet", ""),
                    }
                )
            except Exception as e:
                logger.debug(f"Error fetching {msg['id']}: {e}")
                continue

        return emails

    def _build_search_query(self, query: str) -> str:
        """Build Gmail search query from research topic."""
        # Simple approach: search for query terms in subject and body
        # Could be enhanced with more sophisticated query building
        terms = query.lower().split()
        query_parts = [f"({term})" for term in terms[:5]]  # Limit terms
        return " OR ".join(query_parts)

    def _extract_body(self, message: dict) -> str:
        """Extract plain text body from message."""
        payload = message.get("payload", {})
        body = ""

        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    if "data" in part.get("body", {}):
                        import base64

                        try:
                            body = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                                "utf-8", errors="ignore"
                            )
                        except Exception:
                            pass
                    break
        elif payload.get("body", {}).get("data"):
            import base64

            try:
                body = base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                    "utf-8", errors="ignore"
                )
            except Exception:
                pass

        return body

    async def _analyze_with_ai(
        self,
        emails: list[dict],
        query: str,
    ) -> dict[str, Any]:
        """Use AI to analyze emails for the research topic."""
        # Prepare email content for analysis
        email_contents = []
        for email in emails[:20]:  # Limit for token usage
            email_contents.append(
                f"From: {email['from']}\n"
                f"Date: {email['date']}\n"
                f"Subject: {email['subject']}\n"
                f"Body: {email['body'][:500]}..."
            )

        content_text = "\n\n---\n\n".join(email_contents)

        messages = [
            {
                "role": "user",
                "content": f"""Research topic: {query}

Analyze these emails and provide insights:

{content_text}

Output your analysis as JSON:""",
            }
        ]

        try:
            client = self.router.get_client(
                task_type="reasoning",
                prefer_reasoning=True,
            )

            parsed, response = await client.chat_with_json(
                messages=messages,
                system_prompt=RESEARCH_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=2048,
            )

            return {
                "summary": parsed.get("summary", "No summary available"),
                "key_findings": parsed.get("key_findings", []),
                "entities": parsed.get("entities", {}),
                "timeline": parsed.get("timeline", []),
                "sentiment": parsed.get("sentiment", "unknown"),
                "confidence": parsed.get("confidence", 0.0),
                "emails_analyzed": len(emails),
                "model_used": response.model,
            }

        except LLMError as e:
            logger.warning(f"AI analysis failed: {e}")
            # Fallback to local analysis
            return await self._analyze_locally(emails, query)

    async def _analyze_locally(
        self,
        emails: list[dict],
        query: str,
    ) -> dict[str, Any]:
        """Local (non-AI) analysis of emails."""
        # Simple keyword-based analysis
        query_terms = set(query.lower().split())

        findings = []
        for email in emails:
            # Check subject and body for query terms
            text = f"{email['subject']} {email['body']}".lower()
            matched = [term for term in query_terms if term in text]

            if matched:
                findings.append(
                    {
                        "finding": f"Email about {', '.join(matched)}",
                        "evidence": email["subject"],
                        "date": email["date"][:16],  # Just date part
                    }
                )

        return {
            "summary": f"Found {len(findings)} emails related to '{query}'. "
            "This is a basic local analysis.",
            "key_findings": findings[:10],
            "entities": {"people": [], "organizations": [], "topics": [query]},
            "timeline": [],
            "sentiment": "unknown",
            "confidence": 0.3,
            "emails_analyzed": len(emails),
            "analysis_type": "local",
        }

    async def quick_search(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict]:
        """
        Quick email search without deep analysis.

        Returns list of matching emails with summaries.
        """
        messages, _ = self.gateway.list_messages(
            query=query,
            max_results=max_results,
        )

        results = []
        for msg in messages:
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                results.append(
                    {
                        "id": full["id"],
                        "from": headers.get("From", ""),
                        "subject": headers.get("Subject", ""),
                        "date": headers.get("Date", ""),
                        "snippet": full.get("snippet", ""),
                    }
                )
            except Exception:
                continue

        return results
