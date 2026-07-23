"""Tests for action_triage reliability behaviors.

Covers: stale-action expiration (>14d, testable cleanup path), dry-run purity
(no mutation of live action data), marketing false-positive guard, and the
same company+action open-item suppression cap.

Run: python -m pytest tests/test_action_triage.py -q

All company names are fictional. Marketing-sender fixtures use domains from the
shipped classifier deny-list (that coupling is the point of the guard tests).
"""
import json
from datetime import datetime, timedelta, timezone

from ghostmail import action_triage as at

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _item(email_id="e1", action="interview", company="Acme", sender="Jane <jane@acme.com>",
          age_days=0, notified=False, resolved=False):
    return {
        "email_id": email_id,
        "action": action,
        "company": company,
        "sender": sender,
        "subject": "hello",
        "recorded_at": (NOW - timedelta(days=age_days)).isoformat(),
        "notified": notified,
        "resolved": resolved,
    }


# ---- stale expiration -------------------------------------------------------

def test_expire_marks_items_older_than_14_days():
    items = [_item("old", age_days=15, notified=True)]
    _, n = at._expire_stale_actions(items, now=NOW)
    assert n == 1
    assert items[0]["resolved"] is True
    assert "auto-expired" in items[0]["resolved_reason"]


def test_expire_keeps_items_at_or_under_14_days():
    items = [_item("b14", age_days=14), _item("b0", age_days=0)]
    _, n = at._expire_stale_actions(items, now=NOW)
    assert n == 0
    assert all(not i["resolved"] for i in items)


def test_expire_covers_never_notified_items_and_skips_resolved():
    items = [
        _item("unnotified-stale", age_days=30, notified=False),   # must expire
        _item("already", age_days=30, resolved=True),             # untouched, not counted
    ]
    _, n = at._expire_stale_actions(items, now=NOW)
    assert n == 1
    assert items[0]["resolved"] is True
    assert "resolved_reason" not in items[1]


def test_cleanup_stale_actions_dry_run_does_not_write(tmp_path):
    f = tmp_path / "actions.json"
    f.write_text(json.dumps([_item("old", age_days=20)]), encoding="utf-8")
    before = f.read_bytes()
    res = at.cleanup_stale_actions(actions_path=f, now=NOW, dry_run=True)
    assert res["expired"] == 1 and res["dry_run"] is True
    assert f.read_bytes() == before  # untouched


def test_cleanup_stale_actions_persists_when_not_dry_run(tmp_path):
    f = tmp_path / "actions.json"
    f.write_text(json.dumps([_item("old", age_days=20), _item("new", age_days=1)]),
                 encoding="utf-8")
    res = at.cleanup_stale_actions(actions_path=f, now=NOW, dry_run=False)
    assert res["expired"] == 1
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert saved[0]["resolved"] is True
    assert saved[1]["resolved"] is False


# ---- run_digest dry-run purity ----------------------------------------------

def test_run_digest_dry_run_does_not_mutate_action_file(tmp_path):
    f = tmp_path / "actions.json"
    f.write_text(json.dumps([_item("stale", age_days=60), _item("fresh", age_days=0)]),
                 encoding="utf-8")
    before = f.read_bytes()
    res = at.run_digest(dry_run=True, actions_path=f)
    assert res["draft"] == "(dry-run)"
    assert f.read_bytes() == before  # no expiration write, no notified write


# ---- record_action_items guards ----------------------------------------------

def test_record_dedupes_by_email_id(tmp_path):
    f = tmp_path / "actions.json"
    assert at.record_action_items([_item("e1")], actions_path=f) == 1
    assert at.record_action_items([_item("e1")], actions_path=f) == 0
    assert len(json.loads(f.read_text(encoding="utf-8"))) == 1


def test_record_blocks_marketing_senders(tmp_path):
    f = tmp_path / "actions.json"
    marketing = [
        _item("m1", action="offer", company="Sofi",
              sender="SoFi <SoFi@r.sofi.com>"),
        _item("m2", sender="Tavus Team <hello@tavus.io>"),
        _item("m3", sender="Robert Half <no.reply@email.roberthalf.com>"),
    ]
    assert at.record_action_items(marketing, actions_path=f) == 0
    assert json.loads(f.read_text(encoding="utf-8")) == []


def test_record_allows_real_recruiter_at_guarded_domain(tmp_path):
    f = tmp_path / "actions.json"
    # role-based address at the guarded domain (no person-specific fixture)
    real = _item("r1", company="Roberthalf",
                 sender="RH Recruiting <recruiting@roberthalf.com>")
    assert at.record_action_items([real], actions_path=f) == 1


def test_record_caps_open_items_per_company_action(tmp_path):
    f = tmp_path / "actions.json"
    existing = [_item(f"e{i}", action="assessment", company="Example Mutual")
                for i in range(at.MAX_OPEN_PER_COMPANY_ACTION)]
    f.write_text(json.dumps(existing), encoding="utf-8")
    # 4th open item for the same company+action is suppressed
    assert at.record_action_items(
        [_item("e_new", action="assessment", company="Example Mutual")],
        actions_path=f) == 0
    # a different action or company still records
    assert at.record_action_items(
        [_item("e_new2", action="interview", company="Example Mutual")],
        actions_path=f) == 1
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert len(saved) == at.MAX_OPEN_PER_COMPANY_ACTION + 1


def test_record_cap_ignores_resolved_items(tmp_path):
    f = tmp_path / "actions.json"
    existing = [_item(f"e{i}", action="interview", company="Contoso", resolved=True)
                for i in range(5)]
    f.write_text(json.dumps(existing), encoding="utf-8")
    assert at.record_action_items([_item("e_new", action="interview", company="Contoso")],
                                  actions_path=f) == 1


def test_record_cap_counts_within_batch(tmp_path):
    f = tmp_path / "actions.json"
    batch = [_item(f"b{i}", action="interview", company="Hireloop") for i in range(6)]
    added = at.record_action_items(batch, actions_path=f)
    assert added == at.MAX_OPEN_PER_COMPANY_ACTION


def test_record_creates_parent_dirs(tmp_path):
    f = tmp_path / "nested" / "dir" / "actions.json"
    assert at.record_action_items([_item("e1")], actions_path=f) == 1
    assert f.exists()


# ---- unicode safety ------------------------------------------------------------

def test_safe_never_raises_on_non_cp1252_text():
    s = at._safe("Interview \U0001f4c5 r\u00e9sum\u00e9 \u0393\u0394")
    assert isinstance(s, str)
