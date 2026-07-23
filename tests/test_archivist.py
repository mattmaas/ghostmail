"""Synthetic tests for Archivist.retroactive_organize (no Gmail/network/live state).

Run: python -m pytest tests/test_archivist.py -q
"""
from datetime import datetime, timedelta

from ghostmail.modules.archivist import Archivist


class _FakeArchivist:
    """Stand-in providing only what retroactive_organize actually calls."""

    def __init__(self):
        self.calls = []

    async def organize_emails(self, since=None, dry_run=True):
        self.calls.append({"since": since, "dry_run": dry_run})
        return {"processed": 0, "labels_created": 0, "emails_labeled": 0, "dry_run": dry_run}


async def test_retroactive_organize_computes_cutoff_and_delegates():
    fake = _FakeArchivist()
    result = await Archivist.retroactive_organize(fake, years_back=2, dry_run=True)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["dry_run"] is True
    assert result["dry_run"] is True

    since = call["since"]
    parsed = datetime.strptime(since, "%Y-%m-%d")  # raises if format drifts
    expected = datetime.now() - timedelta(days=2 * 365)
    assert abs((parsed - expected).days) <= 1  # slack for midnight rollover


async def test_retroactive_organize_respects_years_back_and_dry_run_false():
    fake = _FakeArchivist()
    await Archivist.retroactive_organize(fake, years_back=5, dry_run=False)

    call = fake.calls[0]
    assert call["dry_run"] is False
    parsed = datetime.strptime(call["since"], "%Y-%m-%d")
    expected = datetime.now() - timedelta(days=5 * 365)
    assert abs((parsed - expected).days) <= 1
