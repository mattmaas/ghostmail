#!/usr/bin/env python3
"""
Email Batch Sorter — labels Gmail and feeds the action-triage capstone.

MODES
  --mode incremental   (default) forward sweep of NOT-yet-sorted recent mail
                       (query: -label:GM/Sorted newer_than:Nd). This is what runs
                       on schedule and fixes the "new mail never gets sorted" gap.
  --mode backfill      legacy backward paging through deep history (before: cursor).

Every processed email gets the GM/Sorted marker label, so "unsorted" is a clean,
self-correcting query. Job-world mail is classified by job_classifier (Stage-1) +
optional DeepSeek (Stage-2); ACTION items (interview/assessment/offer) are starred,
kept in inbox, cross-linked to job-auto, and recorded for the daily digest.
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime

from ghostmail.config import settings

ENC = sys.stdout.encoding or "utf-8"


def safe(text):
    return str(text or "").encode(ENC, errors="replace").decode(ENC)


def out(*args):
    sys.stdout.write(" ".join(safe(a) for a in args) + "\n")
    sys.stdout.flush()


STATE_F = settings.data_dir / "sort_state.json"

SORTED_MARKER = "GM/Sorted"

# Non-job-world heuristics (job-world now owned by job_classifier).
# Self-sent mail is handled separately via GHOSTMAIL_SELF_EMAIL / _SELF_ALIASES.
HEURISTIC = [
    ("from", "@substack.com", "newsletter", "substack"),
    ("from", "80000hours", "newsletter", "80k-hours"),
    ("from", "noreply@medium.com", "newsletter", "medium"),
    ("from", "@mail.nytimes.com", "newsletter", "nytimes"),
    ("from", "digest@", "newsletter", "digest"),
    ("from", "@actblue.com", "political", "actblue"),
    ("from", "@winred.com", "political", "winred"),
    ("from", "@dccc.org", "political", "dccc"),
    ("from", "@nrsc.org", "political", "nrsc"),
    ("from", "@democrats.org", "political", "democrats"),
    ("from", "change.org", "political", "changeorg"),
    ("from", "@mercury.com", "financial", "mercury"),
    ("from", "@stripe.com", "financial", "stripe"),
    ("from", "@paypal.com", "financial", "paypal"),
    ("subject", "invoice", "financial", "invoice"),
    ("subject", "receipt", "financial", "receipt"),
    ("subject", "billing", "financial", "billing"),
    ("from", "@r.sofi.com", "financial", "sofi"),
    ("from", "@sofi.com", "financial", "sofi"),
    ("from", "@amazon.com", "shopping", "amazon"),
    ("from", "@ebay.com", "shopping", "ebay"),
    ("subject", "order confirm", "shopping", "order"),
    ("subject", "shipped", "shopping", "shipped"),
    ("from", "@github.com", "subscription", "github"),
    ("from", "@gitlab.com", "subscription", "gitlab"),
    ("subject", "password reset", "subscription", "pwd-reset"),
    ("from", "@n8n.io", "n8n-alert", "n8n"),
    ("from", "@cigna.com", "health", "insurance"),
    ("from", "@aetna.com", "health", "insurance"),
    ("subject", "appointment", "health", "appt"),
    ("from", "@expedia.com", "travel", "expedia"),
    ("from", "@booking.com", "travel", "booking"),
    ("from", "@delta.com", "travel", "airline"),
    ("subject", "itinerary", "travel", "itinerary"),
]

GMAIL_LABELS = {
    "newsletter": "GM/Newsletters",
    "political": "GM/Political",
    "financial": "GM/Financial",
    "shopping": "GM/Shopping",
    "subscription": "GM/Subscriptions",
    "personal": "GM/Personal",
    "travel": "GM/Travel",
    "health": "GM/Health",
    "alumni": "GM/Alumni",
    "n8n-alert": "GM/N8N",
    "uncategorized": "GM/Uncategorized",
}

# Buckets that are safe to pull out of the inbox when --archive-noise is on.
ARCHIVE_NOISE = {"job_alert", "app_update"}
ACTION_CATS = {"interview", "assessment", "offer", "scheduling"}


def _self_addresses() -> list[str]:
    """Configured self addresses (GHOSTMAIL_SELF_EMAIL + _SELF_ALIASES)."""
    return settings.self_addresses()


def _self_exclusion() -> str:
    """Gmail query fragment excluding self-sent mail ('' if not configured)."""
    return " ".join(f"-from:{a}" for a in _self_addresses())


def heuristic_classify(frm, sub):
    frm = (frm or "").lower()
    sub = (sub or "").lower()
    if any(a in frm for a in _self_addresses()):
        return "personal", "self-sent"
    for field, pattern, label, reason in HEURISTIC:
        hay = frm if field == "from" else sub
        if pattern.lower() in hay:
            return label, reason
    return None, ""


def _extract_role(subject):
    m = re.search(r"^(.*?)(?:\s*(?:[-\u2013:|]|opportunity|position|role|\())", subject or "", re.I)
    return (m.group(1).strip() if m else (subject or "")[:80])[:90]


def _company_from_sender(sender):
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return ""
    d = m.group(1).lower().split(".")
    return d[-2].title() if len(d) >= 2 else d[0].title()


def load_json(path, default):
    if path.exists():
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"), indent=2)


def fetch_unsorted(gw, days, limit):
    """Forward sweep: recent mail lacking the GM/Sorted marker, newest first."""
    parts = [f"-label:{SORTED_MARKER}", _self_exclusion(), f"newer_than:{days}d"]
    query = " ".join(p for p in parts if p)
    ids, token = [], None
    while len(ids) < limit:
        page, token = gw.list_messages(query=query, max_results=min(500, limit - len(ids)),
                                       page_token=token)
        ids.extend(m["id"] for m in page)
        if not token:
            break
    emails = []
    for mid in ids[:limit]:
        try:
            full = gw.get_message(mid, format="metadata")
            h = {x["name"].lower(): x["value"] for x in full.get("payload", {}).get("headers", [])}
            emails.append({"id": full["id"], "from": h.get("from", ""),
                           "subject": h.get("subject", ""), "date": h.get("date", ""),
                           "snippet": full.get("snippet", "")})
        except Exception:
            continue
    return emails


def fetch_backfill(gw, cursor_date, batch):
    """Legacy backward paging for deep history."""
    from email.utils import parsedate_to_datetime
    query = _self_exclusion()
    if cursor_date:
        try:
            dt = parsedate_to_datetime(cursor_date)
            query += f' before:{dt.strftime("%Y/%m/%d")}'
        except Exception:
            pass
    query = query.strip()
    page, _ = gw.list_messages(query=query, max_results=batch)
    emails = []
    for m in page:
        try:
            full = gw.get_message(m["id"], format="metadata")
            h = {x["name"].lower(): x["value"] for x in full.get("payload", {}).get("headers", [])}
            emails.append({"id": full["id"], "from": h.get("from", ""),
                           "subject": h.get("subject", ""), "date": h.get("date", ""),
                           "snippet": full.get("snippet", "")})
        except Exception:
            continue
    return emails


def run(args):
    from ghostmail.gmail_gateway import GmailGateway
    from ghostmail.job_classifier import classify_job_email, GMAIL_LABEL, ACTION_CATEGORIES
    from ghostmail import action_triage as at

    out("=" * 64)
    out(f"Email Batch Sorter | mode={args.mode} | {datetime.now().isoformat()}")
    out("=" * 64)

    state = load_json(STATE_F, {"processed": 0, "last_date": None, "by_label": {}, "seen_ids": []})
    # Scheduled/headless context: never launch a browser flow here. Expired or
    # revoked OAuth surfaces as GmailAuthError and is handled in main().
    gw = GmailGateway(allow_interactive=False)

    if args.mode == "incremental":
        out(f"Fetching unsorted mail (newer_than:{args.days}d, limit {args.limit})...")
        emails = fetch_unsorted(gw, args.days, args.limit)
    else:
        out("Backfill batch (backward paging)...")
        emails = fetch_backfill(gw, state.get("last_date"), args.limit)
    out(f"Fetched {len(emails)} emails")
    if not emails:
        out("Nothing to sort.")
        return

    seen = set(state.get("seen_ids", []))
    # ---- Stage 1: classify all -------------------------------------------
    decided = []          # (email, final_category, action, meta)
    llm_queue = []
    for e in emails:
        if e["id"] in seen and args.mode == "backfill":
            continue
        jc = classify_job_email(e["from"], e["subject"], e["snippet"])
        if jc.category == "none":
            lbl, reason = heuristic_classify(e["from"], e["subject"])
            decided.append((e, "_h:" + (lbl or "uncategorized"), "none",
                            {"reason": reason or "no-rule"}))
        elif jc.needs_llm and not args.no_llm:
            llm_queue.append(e)
            decided.append((e, None, None, {"jc": jc}))  # resolve after LLM
        else:
            decided.append((e, jc.category, jc.action,
                            {"reason": ";".join(jc.signals), "conf": jc.confidence}))

    # ---- Stage 2: DeepSeek confirm for the small ambiguous set -----------
    verdicts = {}
    if llm_queue:
        out(f"LLM confirm on {len(llm_queue)} ambiguous job email(s)...")
        verdicts = at.confirm_with_llm(llm_queue)

    final = []  # (email, gmail_label, category, action, meta)
    for e, cat, action, meta in decided:
        if cat is None:  # LLM-pending
            jc = meta["jc"]
            v = verdicts.get(e["id"], {})
            vact = v.get("action", "none")
            if vact in ACTION_CATEGORIES:
                cat, action = vact, vact
                meta = {"company": v.get("company"), "role": v.get("role"),
                        "deadline": v.get("deadline"), "reason": v.get("reason"),
                        "conf": v.get("confidence")}
            else:  # LLM says no action -> keep as recruiter if it was a pitch
                cat = "recruiter" if (jc.category in ACTION_CATEGORIES or jc.category == "recruiter") else jc.category
                action = "none"
                meta = {"reason": v.get("reason") or ";".join(jc.signals)}
        if cat.startswith("_h:"):
            internal = cat[3:]
            label = GMAIL_LABELS.get(internal, "GM/Uncategorized")
            final.append((e, label, internal, "none", meta))
        else:
            label = GMAIL_LABEL.get(cat, "GM/Uncategorized")
            final.append((e, label, cat, action, meta))

    # ---- Report + collect action items -----------------------------------
    action_items = []
    cat_counts = {}
    for e, label, cat, action, meta in final:
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        flag = " *ACTION*" if cat in ACTION_CATS else ""
        out(f"  {cat:12s} {label:22s} | {safe(e['subject'])[:52]}{flag}")
        if cat in ACTION_CATEGORIES:
            company = meta.get("company") or _company_from_sender(e["from"])
            link = at.crosslink_jobauto(e["from"], company)
            action_items.append({
                "email_id": e["id"], "action": "interview" if cat == "scheduling" else cat,
                "category": cat, "sender": safe(e["from"]), "subject": safe(e["subject"]),
                "date": e.get("date", ""), "company": company,
                "role": meta.get("role") or _extract_role(e["subject"]),
                "deadline": meta.get("deadline", ""), "reason": meta.get("reason", ""),
                "confidence": meta.get("conf"), "crosslink": link,
            })

    out("\n-- category counts --")
    for k, v in sorted(cat_counts.items(), key=lambda x: -x[1]):
        out(f"  {v:4d}  {k}")

    if args.dry_run:
        out(f"\n[DRY RUN] would label {len(final)} emails, record {len(action_items)} action items.")
        for it in action_items:
            out(f"  ACTION {it['action']:10s} {it['company']:18s} {it['role'][:40]}"
                + (f"  <linked: {it['crosslink']['status']}>" if it.get("crosslink") else ""))
        return

    # ---- Apply Gmail labels (batch per label) + GM/Sorted marker ---------
    out("\nApplying Gmail labels...")
    want = {SORTED_MARKER} | {lbl for _, lbl, _, _, _ in final}
    label_ids = {name: gw.get_or_create_label(name)["id"] for name in want}
    by_label = {}
    star_ids, archive_ids, sorted_ids = [], [], []
    for e, label, cat, action, meta in final:
        by_label.setdefault(label, []).append(e["id"])
        sorted_ids.append(e["id"])
        if cat in ACTION_CATS:
            star_ids.append(e["id"])
        elif args.archive_noise and cat in ARCHIVE_NOISE:
            archive_ids.append(e["id"])

    applied = 0
    for label, eids in by_label.items():
        for i in range(0, len(eids), 900):
            gw.batch_modify_messages(eids[i:i + 900], add_label_ids=[label_ids[label]])
            applied += len(eids[i:i + 900])
    # marker on everything
    for i in range(0, len(sorted_ids), 900):
        gw.batch_modify_messages(sorted_ids[i:i + 900], add_label_ids=[label_ids[SORTED_MARKER]])
    # star actions
    for i in range(0, len(star_ids), 900):
        gw.batch_modify_messages(star_ids[i:i + 900], add_label_ids=["STARRED"])
    # archive noise
    if archive_ids:
        for i in range(0, len(archive_ids), 900):
            gw.batch_modify_messages(archive_ids[i:i + 900], remove_label_ids=["INBOX"])
    out(f"  labeled {applied} | starred {len(star_ids)} | archived {len(archive_ids)}")

    # ---- Record action items for the digest ------------------------------
    new_actions = at.record_action_items(action_items) if action_items else 0
    if action_items:
        out(f"  recorded {new_actions} new action item(s) -> {at.ACTIONS_F}")

    # ---- Update state -----------------------------------------------------
    for _, _, cat, _, _ in final:
        state["by_label"][cat] = state.get("by_label", {}).get(cat, 0) + 1
    state["processed"] = state.get("processed", 0) + len(final)
    seen.update(e["id"] for e, *_ in final)
    state["seen_ids"] = list(seen)
    if args.mode == "backfill" and emails:
        state["last_date"] = emails[-1].get("date", "") or state.get("last_date")
    save_json(STATE_F, state)

    out(f"\n{'=' * 64}")
    out(f"Done: {len(final)} sorted this run | {len(action_items)} action items "
        f"({new_actions} new) | total processed {state['processed']}")


AUTH_EXIT_CODE = 3  # distinct, non-crashing exit for the scheduled task


def _is_auth_failure(exc: BaseException) -> bool:
    """True for OAuth invalid_grant / expired-or-revoked / unauthorized failures."""
    try:
        from google.auth.exceptions import RefreshError
        from ghostmail.gmail_gateway import GmailAuthError
        if isinstance(exc, (RefreshError, GmailAuthError)):
            return True
    except Exception:
        pass
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) == 401:
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return "invalid_grant" in msg or "expired or revoked" in msg


def _report_auth_failure(exc: BaseException) -> None:
    """Sanitized, actionable auth-failure report.

    NEVER prints the exception body (OAuth errors can carry raw response
    details); static guidance + exception type name only. No token data.
    """
    out("=" * 64)
    out("[AUTH] Gmail OAuth token expired or revoked (invalid_grant).")
    out("[AUTH] Run aborted BEFORE any Gmail writes; sort state and action data unchanged.")
    out("[AUTH] Fix: re-authenticate once INTERACTIVELY (opens a browser consent):")
    out("[AUTH]   python -m ghostmail.batch_sorter --mode incremental --days 1 --limit 1")
    out("[AUTH] If no consent is offered, delete the cached credentials file and retry:")
    out(f"[AUTH]   {settings.credentials_path}")
    out(f"[AUTH] detail: {type(exc).__name__} (error body suppressed - may contain OAuth internals)")


def main():
    p = argparse.ArgumentParser(description="GhostMail batch sorter")
    p.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    p.add_argument("--days", type=int, default=45, help="incremental: look-back window")
    p.add_argument("--limit", type=int, default=400, help="max emails per run")
    p.add_argument("--no-llm", action="store_true", help="skip DeepSeek Stage-2")
    p.add_argument("--no-archive", dest="archive_noise", action="store_false",
                   help="do not pull noise out of inbox (label only)")
    p.add_argument("--dry-run", action="store_true", help="classify + report, no writes")
    p.set_defaults(archive_noise=True)
    args = p.parse_args()
    try:
        run(args)
    except Exception as e:
        if _is_auth_failure(e):
            _report_auth_failure(e)
            sys.exit(AUTH_EXIT_CODE)
        raise


if __name__ == "__main__":
    main()
