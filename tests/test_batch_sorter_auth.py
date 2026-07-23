"""Tests for batch_sorter Gmail auth-failure handling (invalid_grant).

Run: python -m pytest tests/test_batch_sorter_auth.py -q
"""
import pytest

from ghostmail import batch_sorter as bs
from ghostmail.gmail_gateway import GmailAuthError, GmailGateway


def test_headless_gateway_raises_auth_error_instead_of_browser_flow(tmp_path):
    gw = GmailGateway(credentials_path=tmp_path / "nope.json", allow_interactive=False)
    with pytest.raises(GmailAuthError):
        _ = gw.service  # lazy build -> must raise, never launch run_local_server


def test_interactive_gateway_still_allowed_to_reauth(tmp_path):
    # default mode keeps the legacy interactive path (we only assert it does NOT
    # raise GmailAuthError; it will fail later on missing client config)
    gw = GmailGateway(credentials_path=tmp_path / "nope.json")
    assert gw.allow_interactive is True


def test_is_auth_failure_detection():
    assert bs._is_auth_failure(GmailAuthError("x")) is True
    assert bs._is_auth_failure(Exception("invalid_grant: Token has been expired or revoked.")) is True
    assert bs._is_auth_failure(RuntimeError("connection reset by peer")) is False
    assert bs._is_auth_failure(ValueError("bad label name")) is False


def test_auth_report_is_actionable_and_never_leaks_error_body(capsys):
    secretish = "invalid_grant response body: access_token=ya29.SUPERSECRET refresh=1//SENSITIVE"
    bs._report_auth_failure(GmailAuthError(secretish))
    out_text = capsys.readouterr().out
    assert "ya29" not in out_text
    assert "SUPERSECRET" not in out_text
    assert "SENSITIVE" not in out_text
    assert "invalid_grant" in out_text          # actionable fixed text is present
    assert "re-authenticate" in out_text
    assert "credentials.json" in out_text       # points at the fix location
    assert "GmailAuthError" in out_text         # exception type name only
