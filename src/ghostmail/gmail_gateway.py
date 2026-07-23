"""Gmail Gateway - OAuth2 authentication and API client."""

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Any, Generator, Optional

import google.auth
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient import discovery
from googleapiclient.errors import HttpError

from .config import get_settings

logger = logging.getLogger(__name__)


class GmailAuthError(RuntimeError):
    """Gmail OAuth credentials are missing, expired, or revoked (invalid_grant)
    and interactive re-authentication is not allowed (e.g. a headless scheduled
    run). Messages are static, actionable text only - never token data."""


class RateLimiter:
    """Token bucket rate limiter for Gmail API quota units."""

    def __init__(self, units_per_minute: int = 15000):
        self.units_per_minute = units_per_minute
        self.tokens = float(units_per_minute)
        self.last_refill = time.time()
        self.refill_rate = units_per_minute / 60.0  # per second

    def consume(self, units: int) -> bool:
        """Try to consume tokens. Returns True if allowed."""
        self._refill()
        if self.tokens >= units:
            self.tokens -= units
            return True
        return False

    def wait_for(self, units: int) -> float:
        """Wait until tokens are available. Returns wait time."""
        while not self.consume(units):
            self._refill()
            sleep_time = (units - self.tokens) / self.refill_rate
            logger.debug(f"Rate limited, waiting {sleep_time:.2f}s")
            time.sleep(min(sleep_time, 1.0))  # Cap at 1 second
        return 0.0

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.units_per_minute, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now


class GmailGateway:
    """Gmail API gateway with OAuth2 and rate limiting."""

    def __init__(self, credentials_path: Optional[Path] = None, *,
                 allow_interactive: bool = True):
        self.settings = get_settings()
        self.credentials_path = credentials_path or self.settings.credentials_path
        self.allow_interactive = allow_interactive
        self._service = None
        self._rate_limiter = RateLimiter(self.settings.gmail_quota_units_per_minute)

    @property
    def service(self):
        """Lazy-load Gmail service."""
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build Gmail API service with OAuth2."""
        credentials = self._load_credentials()
        if not credentials or not credentials.valid:
            if not self.allow_interactive:
                raise GmailAuthError(
                    "Gmail OAuth credentials missing, expired, or revoked; "
                    "interactive re-authentication required"
                )
            credentials = self._get_new_credentials()

        return discovery.build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )

    def _load_credentials(self) -> Optional[Credentials]:
        """Load credentials from file if they exist."""
        if not self.credentials_path.exists():
            return None

        try:
            with open(self.credentials_path, "rb") as f:
                credentials = pickle.load(f)

            # Check if refresh is needed
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                self._save_credentials(credentials)

            return credentials
        except RefreshError:
            # invalid_grant: token expired or revoked. Never log the exception
            # body (it carries the raw OAuth response); static text only.
            logger.warning(
                "Gmail OAuth refresh rejected (invalid_grant: token expired or "
                "revoked); interactive re-authentication required"
            )
            return None
        except Exception as e:
            # Sanitized: exception type only, never the message body.
            logger.warning("Failed to load credentials (%s)", type(e).__name__)
            return None

    def _save_credentials(self, credentials: Credentials):
        """Save credentials to file."""
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.credentials_path, "wb") as f:
            pickle.dump(credentials, f)
        logger.info("Credentials saved")

    def _get_new_credentials(self) -> Credentials:
        """Get new credentials via OAuth2 flow."""
        # For desktop apps, we need a client secrets file
        # We'll create a minimal one or use the installed app flow

        # Check if we have client ID/secret from settings
        if self.settings.gmail_client_id and self.settings.gmail_client_secret:
            # Use settings-based OAuth
            client_config = {
                "web": {
                    "client_id": self.settings.gmail_client_id,
                    "client_secret": self.settings.gmail_client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        else:
            # Need to create client secrets or use default
            # For now, raise an error with instructions
            raise ValueError(
                "Gmail OAuth2 not configured. Please set:\n"
                "1. Go to Google Cloud Console (https://console.cloud.google.com)\n"
                "2. Create a project and enable Gmail API\n"
                "3. Create OAuth2 credentials (Desktop app)\n"
                "4. Set GHOSTMAIL_GMAIL_CLIENT_ID and GHOSTMAIL_GMAIL_CLIENT_SECRET\n"
                "   or download client_secrets.json and place it in the data directory"
            )

        flow = InstalledAppFlow.from_client_config(
            client_config,
            scopes=self.settings.gmail_scopes,
        )

        # Run local server for callback
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",
            access_type="offline",  # Get refresh token
        )

        self._save_credentials(credentials)  # type: ignore
        return credentials  # type: ignore

    def reconnect(self):
        """Force reconnection (after token refresh issues)."""
        self._service = None
        self._service = self._build_service()

    # ==================== Gmail API Operations ====================

    def list_messages(
        self,
        query: str = "",
        max_results: int = 100,
        page_token: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """
        List messages matching query.

        Returns: (messages, next_page_token)
        """
        self._rate_limiter.wait_for(5)  # messages.list = 5 units

        result = (
            self.service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token,
            )
            .execute()
        )

        return result.get("messages", []), result.get("nextPageToken")

    def get_message(
        self,
        msg_id: str,
        format: str = "full",
    ) -> dict[str, Any]:
        """
        Get a message by ID.

        format: "full", "metadata", "minimal"
        """
        self._rate_limiter.wait_for(5)  # messages.get = 5 units

        return self.service.users().messages().get(userId="me", id=msg_id, format=format).execute()

    def get_messages_batch(
        self,
        msg_ids: list[str],
        format: str = "metadata",
    ) -> Generator[dict[str, Any], None, None]:
        """
        Get multiple messages efficiently.

        Uses batch processing to stay within rate limits.
        """
        for msg_id in msg_ids:
            yield self.get_message(msg_id, format=format)
            # Small delay to avoid burst rate limiting
            time.sleep(0.01)

    def modify_message(
        self,
        msg_id: str,
        add_label_ids: Optional[list[str]] = None,
        remove_label_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Modify message labels."""
        self._rate_limiter.wait_for(5)  # messages.modify = 5 units

        body = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids

        return self.service.users().messages().modify(userId="me", id=msg_id, body=body).execute()

    def batch_modify_messages(
        self,
        msg_ids: list[str],
        add_label_ids: Optional[list[str]] = None,
        remove_label_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Batch modify up to 1000 messages.

        Ideal for labeling/organization operations.
        """
        self._rate_limiter.wait_for(50)  # messages.batchModify = 50 units

        body = {"ids": msg_ids}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids

        return self.service.users().messages().batchModify(userId="me", body=body).execute()

    def trash_message(self, msg_id: str) -> dict[str, Any]:
        """Move message to trash."""
        self._rate_limiter.wait_for(5)

        return self.service.users().messages().trash(userId="me", id=msg_id).execute()

    def delete_message(self, msg_id: str) -> dict[str, Any]:
        """
        Permanently delete message.

        Note: Only works on messages in TRASH or SPAM.
        """
        self._rate_limiter.wait_for(5)

        return self.service.users().messages().delete(userId="me", id=msg_id).execute()

    def batch_delete_messages(self, msg_ids: list[str]) -> dict[str, Any]:
        """
        Batch delete up to 1000 messages.

        Messages must be in TRASH first.
        """
        self._rate_limiter.wait_for(50)  # messages.batchDelete = 50 units

        return (
            self.service.users()
            .messages()
            .batchDelete(userId="me", body={"ids": msg_ids})
            .execute()
        )

    def create_draft(
        self,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a new draft."""
        self._rate_limiter.wait_for(100)  # drafts.create = 100 units

        return (
            self.service.users().drafts().create(userId="me", body={"message": message}).execute()
        )

    def send_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a message directly."""
        self._rate_limiter.wait_for(100)  # messages.send = 100 units

        return self.service.users().messages().send(userId="me", body=message).execute()

    # ==================== Labels ====================

    def list_labels(self) -> list[dict[str, Any]]:
        """List all labels."""
        self._rate_limiter.wait_for(5)

        result = self.service.users().labels().list(userId="me").execute()
        return result.get("labels", [])

    def create_label(
        self,
        name: str,
        label_list_visibility: str = "labelShow",
        message_list_visibility: str = "show",
    ) -> dict[str, Any]:
        """Create a new label."""
        self._rate_limiter.wait_for(5)

        body = {
            "name": name,
            "labelListVisibility": label_list_visibility,
            "messageListVisibility": message_list_visibility,
        }

        return self.service.users().labels().create(userId="me", body=body).execute()

    def get_or_create_label(self, name: str) -> dict[str, Any]:
        """Get existing label or create if it doesn't exist."""
        labels = self.list_labels()

        # Find exact match
        for label in labels:
            if label["name"] == name:
                return label

        # Create new label
        # Handle nested labels (e.g., "GhostMail/Work/Projects")
        parts = name.split("/")
        current_path = ""
        found = None

        for i, part in enumerate(parts):
            if current_path:
                current_path = f"{current_path}/{part}"
            else:
                current_path = part

            # Check if this path exists
            found = None
            for label in labels:
                if label["name"] == current_path:
                    found = label
                    break

            if not found:
                found = self.create_label(current_path)
                labels.append(found)  # Update local cache

            current_parent = found.get("id")

        if found is None:
            return {}
        return found  # Return the final label

    # ==================== History (for sync) ====================

    def get_history(
        self,
        start_history_id: str,
        history_types: Optional[list[str]] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """
        Get history of changes since start_history_id.

        history_types: "messageAdded", "messageDeleted", "labelAdded", "labelRemoved"
        """
        self._rate_limiter.wait_for(5)

        body: dict = {"startHistoryId": start_history_id}
        if history_types:
            body["historyTypes"] = history_types

        result = self.service.users().history().list(userId="me", **body).execute()

        return result.get("history", []), result.get("nextPageToken")

    def get_profile(self) -> dict[str, Any]:
        """Get current user's profile."""
        self._rate_limiter.wait_for(5)

        return self.service.users().getProfile(userId="me").execute()


def get_gateway() -> GmailGateway:
    """Get singleton GmailGateway instance."""
    return GmailGateway()
