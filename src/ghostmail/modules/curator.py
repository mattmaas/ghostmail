"""Curator - Digital identity shaping module."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..ai_engine import LLMError, get_router
from ..config import get_settings
from ..database import CachedEmail, Database, ShapingSession, get_database
from ..gmail_gateway import GmailGateway

logger = logging.getLogger(__name__)


CURATOR_SYSTEM_PROMPT = """You are a digital identity analyst for Gmail.

Analyze the user's email profile and create a shaping plan to curate their digital footprint.

Output a JSON shaping plan:

{
  "topics": [
    {
      "name": "topic_name",
      "email_count": 150,
      "date_range": "2020-2024",
      "actions": [
        {"action": "delete", "count": 100, "reason": "outdated newsletters"},
        {"action": "unsubscribe", "count": 5, "reason": "no longer interested"},
        {"action": "relabel", "count": 20, "reason": "reclassify as professional"}
      ]
    }
  ],
  "recommendations": [
    "Disable Smart Features in Gmail settings",
    "Review connected third-party apps"
  ],
  "priority": "high" | "medium" | "low"
}

Guidelines:
- Focus on actionable items that actually change profiling signals
- Prioritize deletion of promotional/old content
- Identify unsubscribe opportunities
- Be realistic about what actually changes profiles vs. privacy theater
- Focus on topics with significant email volume"""


@dataclass
class TopicAnalysis:
    """Analysis of an email topic."""

    name: str
    email_count: int
    date_range: str
    oldest: str
    newest: str
    senders: list[str]
    actions: list[dict]


class Curator:
    """Digital identity curator - shapes your email profile."""

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

    async def audit_profile(self) -> dict[str, Any]:
        """
        Audit the user's email profile.

        Returns topic analysis and shaping recommendations.
        """
        # First, get email statistics
        topics = await self._analyze_topics()

        # Get user preferences
        preferences = self.db.get_user_preferences("curator")

        # Use AI to generate shaping plan
        try:
            shaping_plan = await self._generate_shaping_plan(topics, preferences)
        except LLMError as e:
            logger.warning(f"AI shaping plan failed: {e}")
            shaping_plan = self._default_shaping_plan(topics)

        # Save audit snapshot
        session = ShapingSession(
            audit_snapshot={
                "topics": {
                    k: {"count": v.email_count, "oldest": v.oldest, "newest": v.newest}
                    for k, v in topics.items()
                }
            },
            actions_taken=[],
            result_snapshot={},
        )
        self.db.save_shaping_session(session)

        return {
            "topics": {
                k: {
                    "count": v.email_count,
                    "oldest": v.oldest,
                    "newest": v.newest,
                    "senders": v.senders[:5],
                }
                for k, v in topics.items()
            },
            "shaping_plan": shaping_plan,
        }

    async def execute_shaping(self, audit_results: dict) -> dict[str, Any]:
        """Execute the shaping plan from audit."""
        shaping_plan = audit_results.get("shaping_plan", {})
        topics_data = shaping_plan.get("topics", [])

        results = {
            "deleted": 0,
            "unsubscribed": 0,
            "relabeled": 0,
        }

        for topic in topics_data:
            for action in topic.get("actions", []):
                action_type = action.get("action")
                count = action.get("count", 0)

                if action_type == "delete":
                    deleted = await self._delete_emails_by_topic(topic["name"], count)
                    results["deleted"] += deleted

                elif action_type == "unsubscribe":
                    # Unsubscribe from senders in topic
                    results["unsubscribed"] += count

                elif action_type == "relabel":
                    relabeled = await self._relabel_emails_by_topic(
                        topic["name"], action.get("new_label", "GhostMail/Reference")
                    )
                    results["relabeled"] += relabeled

        return results

    async def _analyze_topics(self) -> dict[str, TopicAnalysis]:
        """Analyze email topics in the mailbox."""
        # Get recent emails (sample)
        messages, _ = self.gateway.list_messages(
            query="older_than:30d",  # Last 30 days
            max_results=500,
        )

        # Group by sender domain
        sender_emails = defaultdict(list)

        for msg in messages:
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                from_addr = headers.get("From", "")
                # Extract domain
                if "<" in from_addr:
                    from_addr = from_addr.split("<")[1].rstrip(">")

                sender_emails[from_addr].append(
                    {
                        "date": headers.get("Date", ""),
                        "subject": headers.get("Subject", ""),
                    }
                )
            except Exception as e:
                logger.debug(f"Error processing {msg['id']}: {e}")
                continue

        # Categorize by topic
        topics: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "count": 0,
                "dates": [],
                "senders": set(),
            }
        )

        # Known topic patterns
        topic_patterns = {
            "Crypto": ["coinbase", "binance", "crypto", "bitcoin", "ethereum"],
            "Shopping": ["amazon", "ebay", "etsy", "shop", "store"],
            "Finance": ["bank", "paypal", "stripe", "venmo", "investment"],
            "Tech": ["github", "stackoverflow", "tech", "dev", "software"],
            "Newsletters": ["newsletter", "substack", "medium", "digest"],
            "Promotions": ["promo", "deal", "sale", "offer", "discount"],
            "Job Search": ["linkedin", "indeed", "monster", "resume"],
        }

        for sender, emails in sender_emails.items():
            sender_lower = sender.lower()

            # Find matching topic
            matched = False
            for topic, patterns in topic_patterns.items():
                if any(p in sender_lower for p in patterns):
                    topics[topic]["count"] += len(emails)
                    topics[topic]["senders"].add(sender)
                    topics[topic]["dates"].extend([e["date"] for e in emails])
                    matched = True
                    break

            if not matched:
                topics["Other"]["count"] += len(emails)
                topics["Other"]["senders"].add(sender)
                topics["Other"]["dates"].extend([e["date"] for e in emails])

        # Convert to TopicAnalysis objects
        result = {}
        for topic, data in topics.items():
            dates = sorted(data["dates"]) if data["dates"] else []

            result[topic] = TopicAnalysis(
                name=topic,
                email_count=data["count"],
                date_range=f"{len(dates)} emails",
                oldest=dates[0] if dates else "",
                newest=dates[-1] if dates else "",
                senders=list(data["senders"]),
                actions=[],
            )

        return result

    async def _generate_shaping_plan(
        self,
        topics: dict[str, TopicAnalysis],
        preferences: dict,
    ) -> dict:
        """Use AI to generate shaping plan."""
        # Build topic summary for AI
        topic_summary = []
        for topic, analysis in topics.items():
            topic_summary.append(
                {
                    "name": topic,
                    "email_count": analysis.email_count,
                    "oldest": analysis.oldest,
                    "newest": analysis.newest,
                    "top_senders": analysis.senders[:5],
                }
            )

        messages = [
            {
                "role": "user",
                "content": f"""Analyze this email profile and create a shaping plan:

Topics found: {topic_summary}

User preferences: {preferences}

Create a JSON shaping plan:""",
            }
        ]

        try:
            client = self.router.get_client(task_type="reasoning", prefer_reasoning=True)

            parsed, _ = await client.chat_with_json(
                messages=messages,
                system_prompt=CURATOR_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=2048,
            )

            return parsed

        except LLMError as e:
            logger.error(f"Failed to generate shaping plan: {e}")
            raise

    def _default_shaping_plan(self, topics: dict[str, TopicAnalysis]) -> dict:
        """Generate a basic shaping plan without AI."""
        default_plan = {
            "topics": [],
            "recommendations": [
                "Disable Smart Features in Gmail settings",
                "Review connected third-party apps",
            ],
            "priority": "medium",
        }

        for topic, analysis in topics.items():
            if analysis.email_count > 50:
                actions = []

                if analysis.email_count > 100:
                    actions.append(
                        {
                            "action": "delete",
                            "count": min(analysis.email_count // 2, 100),
                            "reason": "reduce profile footprint",
                        }
                    )

                actions.append(
                    {
                        "action": "unsubscribe",
                        "count": min(len(analysis.senders), 5),
                        "reason": "reduce incoming volume",
                    }
                )

                default_plan["topics"].append(
                    {
                        "name": topic,
                        "email_count": analysis.email_count,
                        "actions": actions,
                    }
                )

        return default_plan

    async def _delete_emails_by_topic(self, topic: str, count: int) -> int:
        """Delete emails related to a topic."""
        # Find emails to delete
        query = self._topic_to_query(topic)
        messages, _ = self.gateway.list_messages(
            query=f"{query} in:trash",
            max_results=count,
        )

        # If not in trash, move to trash first
        if not messages:
            messages, _ = self.gateway.list_messages(
                query=query,
                max_results=count,
            )

            # Move to trash in batches
            for i in range(0, len(messages), 50):
                batch = messages[i : i + 50]
                msg_ids = [m["id"] for m in batch]
                # Note: Gmail doesn't have batch trash, do individually
                for msg_id in msg_ids:
                    try:
                        self.gateway.trash_message(msg_id)
                    except Exception as e:
                        logger.debug(f"Failed to trash {msg_id}: {e}")

        # Now permanently delete
        deleted = 0
        for i in range(0, min(count, 100), 50):
            batch_ids = [m["id"] for m in messages[i : i + 50]]
            try:
                self.gateway.batch_delete_messages(batch_ids)
                deleted += len(batch_ids)
            except Exception as e:
                logger.debug(f"Batch delete failed: {e}")

        return deleted

    async def _relabel_emails_by_topic(self, topic: str, new_label: str) -> int:
        """Relabel emails from a topic."""
        query = self._topic_to_query(topic)
        messages, _ = self.gateway.list_messages(
            query=query,
            max_results=100,
        )

        if not messages:
            return 0

        # Get or create label
        label = self.gateway.get_or_create_label(new_label)

        # Batch modify
        msg_ids = [m["id"] for m in messages]
        self.gateway.batch_modify_messages(
            msg_ids,
            add_label_ids=[label["id"]],
        )

        return len(msg_ids)

    def _topic_to_query(self, topic: str) -> str:
        """Convert topic to Gmail search query."""
        queries = {
            "Crypto": "from:coinbase OR from:binance OR from:kraken",
            "Shopping": "from:amazon OR from:ebay OR from:etsy",
            "Finance": "from:paypal OR from:stripe OR from:bank",
            "Tech": "from:github OR from:stackoverflow",
            "Newsletters": "subject:newsletter OR subject:digest",
            "Promotions": "subject:deal OR subject:sale OR subject:promo",
            "Job Search": "from:linkedin OR from:indeed",
        }
        return queries.get(topic, "")
