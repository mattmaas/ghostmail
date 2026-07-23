#!/usr/bin/env python3
"""
Action triage — the capstone to job-auto's outbound auto-apply.

Pipeline:
  1) Stage-1 deterministic classification (job_classifier.classify_job_email)
  2) Stage-2 DeepSeek confirm — ONLY for emails Stage-1 flags needs_llm=True
     (genuine recruiter / ambiguous action). Keeps LLM cost to a handful/sweep.
  3) Cross-link the sender to job-auto's applied companies (jobs.json), so a
     recruiter reply reads as: "You applied to {Company} {role} on {date} via {ATS}."
  4) Record action items to ~/.ghostmail/data/action_required.json
  5) Daily digest: draft an email to self + fire a desktop notification.

Public entry points used by batch_sorter and the scheduler:
  - confirm_with_llm(emails)            -> {email_id: verdict}
  - crosslink_jobauto(sender, company)  -> matched applied job or None
  - record_action_items(items)          -> appends/dedupes action_required.json
  - run_digest(dry_run=False)           -> builds draft + desktop notification
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from ghostmail.config import settings
from ghostmail.job_classifier import MARKETING_SENDERS

DATA = settings.data_dir
ACTIONS_F = DATA / "action_required.json"
# Optional JobAuto cross-link (GHOSTMAIL_JOBAUTO_JOBS_PATH). None -> disabled.
JOBAUTO_JOBS = settings.jobauto_jobs_path
# Digest sender/recipient (GHOSTMAIL_SELF_EMAIL). "" -> digest draft skipped.
SELF_ADDR = settings.self_email

# Stale-action hygiene: unresolved items older than this are auto-resolved.
STALE_ACTION_DAYS = 14
# Same company+action spam guard: keep at most this many OPEN items per
# (action, company). Suppresses reminder storms (e.g. 8x the same video-
# interview invite) without ever hiding the first ones.
MAX_OPEN_PER_COMPANY_ACTION = 3

# --- Unicode-safe stdout/stderr (fixes cp1252 console crash on Windows) ---
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ENC = sys.stdout.encoding or "utf-8"


def _safe(t):
    return str(t or "").encode(ENC, errors="replace").decode(ENC)


# =========================================================================
# Stage-2: DeepSeek confirm (cheap deepseek-chat, batched)
# =========================================================================
LLM_SYSTEM = """You triage inbound job/recruiting emails for a software engineer (C#/.NET/AI).
For EACH email decide the single best action category:
  "interview"  = a human (recruiter/hiring mgr) wants to talk, schedule a call, phone screen,
                 or gauges interest in a SPECIFIC role for THIS candidate.
  "assessment" = candidate must complete a coding test, technical/skills assessment, questionnaire,
                 or screening exercise to be considered (HackerRank/Codility/HireVue/etc).
  "offer"      = a real job offer or offer letter.
  "none"       = automated application receipt, rejection, job-alert digest, newsletter,
                 marketing, or anything that needs no action from the recipient.
Extract company, role, and any deadline if present.
Respond with ONLY a JSON object: {"results":[{"i":<index>,"action":"interview|assessment|offer|none",
"confidence":0.0-1.0,"company":"","role":"","deadline":"","reason":"<=12 words"}]}"""


async def _confirm_async(emails: list[dict]) -> dict[str, dict]:
    """emails: [{id, from, subject, snippet}]. Returns {id: verdict}."""
    if not emails:
        return {}
    try:
        from ghostmail.ai_engine import DeepSeekClient
        client = DeepSeekClient(model="deepseek-chat")
    except Exception as e:  # no key / import problem -> graceful skip
        return {e_id: {"action": "none", "confidence": 0.0, "reason": f"llm-unavailable:{e}"}
                for e_id in [m["id"] for m in emails]}

    out: dict[str, dict] = {}
    BATCH = 8
    try:
        for start in range(0, len(emails), BATCH):
            chunk = emails[start:start + BATCH]
            lines = []
            for i, m in enumerate(chunk):
                lines.append(
                    f"[{i}] From: {_safe(m.get('from',''))[:120]}\n"
                    f"    Subject: {_safe(m.get('subject',''))[:160]}\n"
                    f"    Snippet: {_safe(m.get('snippet',''))[:300]}"
                )
            user = "Classify these emails:\n\n" + "\n\n".join(lines)
            try:
                parsed, _ = await client.chat_with_json(
                    messages=[{"role": "user", "content": user}],
                    system_prompt=LLM_SYSTEM, temperature=0.1, max_tokens=900,
                )
                for r in parsed.get("results", []):
                    idx = r.get("i")
                    if isinstance(idx, int) and 0 <= idx < len(chunk):
                        out[chunk[idx]["id"]] = {
                            "action": r.get("action", "none"),
                            "confidence": float(r.get("confidence", 0.5) or 0.5),
                            "company": r.get("company", ""),
                            "role": r.get("role", ""),
                            "deadline": r.get("deadline", ""),
                            "reason": r.get("reason", ""),
                        }
            except Exception as e:
                for m in chunk:
                    out.setdefault(m["id"], {"action": "none", "confidence": 0.0,
                                            "reason": f"llm-error:{str(e)[:60]}"})
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return out


def confirm_with_llm(emails: list[dict]) -> dict[str, dict]:
    """Sync wrapper. emails flagged needs_llm only. Returns {id: verdict}."""
    try:
        return asyncio.run(_confirm_async(emails))
    except RuntimeError:
        # already in an event loop (rare) -> new loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_confirm_async(emails))
        finally:
            loop.close()


# =========================================================================
# job-auto cross-link
# =========================================================================
_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|co|company|gmbh|technologies|technology|"
                     r"solutions|systems|labs|group|consulting|software|staffing)\b\.?")


def _norm_company(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = _SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _domain_of(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return ""
    d = m.group(1).lower()
    parts = d.split(".")
    return parts[-2] if len(parts) >= 2 else d  # 2nd-level label


_APPLIED_CACHE = None


def _load_applied():
    global _APPLIED_CACHE
    if _APPLIED_CACHE is not None:
        return _APPLIED_CACHE
    idx_company: dict[str, list] = {}
    idx_domain: dict[str, list] = {}
    ENGAGED = {"applied", "interview", "offer", "apply_failed", "rejected", "ghosted"}
    try:
        if not JOBAUTO_JOBS:
            raise FileNotFoundError("GHOSTMAIL_JOBAUTO_JOBS_PATH not set")
        data = json.load(open(JOBAUTO_JOBS, encoding="utf-8"))
        for j in data:
            if (j.get("status") or "") not in ENGAGED:
                continue
            rec = {"company": j.get("company", ""), "title": j.get("title", ""),
                   "status": j.get("status", ""), "appliedAt": j.get("appliedAt", ""),
                   "source": j.get("source", ""), "url": j.get("url", "")}
            cn = _norm_company(j.get("company", ""))
            if cn:
                idx_company.setdefault(cn, []).append(rec)
            dom = _domain_of(j.get("url", ""))
            if dom:
                idx_domain.setdefault(dom, []).append(rec)
    except Exception as e:
        sys.stderr.write(f"[crosslink] could not load jobs.json: {e}\n")
    _APPLIED_CACHE = (idx_company, idx_domain)
    return _APPLIED_CACHE


def crosslink_jobauto(sender: str, company_guess: str = "") -> dict | None:
    """Match an inbound email to a job-auto applied record. Returns rec or None."""
    idx_company, idx_domain = _load_applied()
    dom = _domain_of(sender)
    if dom and dom in idx_domain:
        return idx_domain[dom][0]
    cn = _norm_company(company_guess)
    if cn and cn in idx_company:
        return idx_company[cn][0]
    if cn:  # token-overlap fuzzy
        ctoks = set(cn.split())
        for key, recs in idx_company.items():
            ktoks = set(key.split())
            if ctoks and ktoks and len(ctoks & ktoks) / max(len(ctoks), 1) >= 0.6:
                return recs[0]
    return None


# =========================================================================
# Recording action items
# =========================================================================
def _load_actions(actions_path: Path = ACTIONS_F) -> list:
    if actions_path.exists():
        try:
            return json.load(open(actions_path, encoding="utf-8"))
        except Exception:
            return []
    return []


def _expire_stale_actions(actions: list, max_age_days: int = STALE_ACTION_DAYS,
                          now: datetime | None = None) -> tuple[list, int]:
    """Mark unresolved items older than max_age_days as resolved.

    Testable: pass `now` for deterministic age math. Returns (actions, num_expired).
    """
    now = now or datetime.now(timezone.utc)
    expired = 0
    for a in actions:
        if a.get("resolved"):
            continue
        recorded = a.get("recorded_at", "")
        if not recorded:
            continue
        try:
            age = (now - datetime.fromisoformat(str(recorded).replace("Z", "+00:00"))).days
        except Exception:
            continue
        if age > max_age_days:
            a["resolved"] = True
            a["resolved_reason"] = f"auto-expired ({age}d old)"
            expired += 1
    return actions, expired


def cleanup_stale_actions(actions_path: Path = ACTIONS_F,
                          max_age_days: int = STALE_ACTION_DAYS,
                          now: datetime | None = None,
                          dry_run: bool = False) -> dict:
    """Testable cleanup path: auto-resolve stale items in the action file.

    dry_run=True reports what WOULD change and never writes the file.
    """
    actions = _load_actions(actions_path)
    actions, expired = _expire_stale_actions(actions, max_age_days=max_age_days, now=now)
    if expired and not dry_run:
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        json.dump(actions, open(actions_path, "w", encoding="utf-8"), indent=2)
    return {"expired": expired, "total": len(actions), "dry_run": dry_run,
            "path": str(actions_path)}


def _is_marketing_sender(sender: str) -> bool:
    """Precise non-action/marketing senders (owned by job_classifier.MARKETING_SENDERS)."""
    s = (sender or "").lower()
    return any(m in s for m in MARKETING_SENDERS)


def _dedup_key(item: dict) -> tuple[str, str]:
    """Suppression key: action + normalized company (sender domain as fallback)."""
    comp = _norm_company(item.get("company", "")) or _domain_of(item.get("sender", ""))
    return (item.get("action") or "", comp)


def record_action_items(items: list[dict], actions_path: Path = ACTIONS_F) -> int:
    """Append new action items. Returns #new.

    Guards:
      - dedupe by email_id (same Gmail message never recorded twice)
      - marketing/non-action senders are never recorded (defense-in-depth behind
        the Stage-1 classifier, which already routes them away from actions)
      - per-(action, company) OPEN-item cap (MAX_OPEN_PER_COMPANY_ACTION)
        suppresses reminder storms without hiding the first items
    """
    existing = _load_actions(actions_path)
    seen = {a.get("email_id") for a in existing}
    open_counts: dict[tuple[str, str], int] = {}
    for a in existing:
        if not a.get("resolved"):
            k = _dedup_key(a)
            open_counts[k] = open_counts.get(k, 0) + 1
    added = suppressed = 0
    for it in items:
        eid = it.get("email_id")
        if eid in seen:
            continue
        seen.add(eid)
        if _is_marketing_sender(it.get("sender", "")):
            suppressed += 1
            continue
        key = _dedup_key(it)
        if open_counts.get(key, 0) >= MAX_OPEN_PER_COMPANY_ACTION:
            suppressed += 1
            continue
        open_counts[key] = open_counts.get(key, 0) + 1
        it.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        it.setdefault("notified", False)
        it.setdefault("resolved", False)
        existing.insert(0, it)
        added += 1
    if suppressed:
        print(f"[record] suppressed {suppressed} item(s) "
              f"(marketing sender or >= {MAX_OPEN_PER_COMPANY_ACTION} open per company+action)")
    actions_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(existing, open(actions_path, "w", encoding="utf-8"), indent=2)
    return added


# =========================================================================
# Daily digest: draft to self + desktop notification
# =========================================================================
_ICON = {"offer": "[OFFER]", "interview": "[INTERVIEW]", "assessment": "[ASSESSMENT]"}
_ORDER = {"offer": 0, "interview": 1, "assessment": 2}


def _build_digest_text(items: list[dict]) -> tuple[str, str, str]:
    n = len(items)
    today = datetime.now().strftime("%a %b %d")
    subject = f"GhostMail - {n} job action item{'s' if n != 1 else ''} need you ({today})"
    items = sorted(items, key=lambda x: _ORDER.get(x.get("action"), 9))
    lines = [f"GhostMail action digest - {today}",
             f"{n} inbound email(s) need action. (auto-sorted; reply/act in Gmail)\n"]
    html = [f"<h2>GhostMail action digest &mdash; {today}</h2>",
            f"<p><b>{n}</b> inbound email(s) need action.</p><ul>"]
    for it in items:
        tag = _ICON.get(it.get("action"), "[ACTION]")
        comp = it.get("company") or "?"
        role = it.get("role") or it.get("subject", "")
        frm = it.get("sender", "")
        link = it.get("crosslink")
        ln = f"{tag} {comp} - {role}\n      from: {frm}"
        h = f"<li><b>{tag} {comp}</b> &mdash; {role}<br><small>from: {frm}"
        if it.get("deadline"):
            ln += f"\n      DEADLINE: {it['deadline']}"
            h += f" &middot; <b>DEADLINE: {it['deadline']}</b>"
        if link:
            ln += (f"\n      job-auto: you applied to {link.get('company')} "
                   f"({link.get('title')}) status={link.get('status')} "
                   f"on {str(link.get('appliedAt'))[:10]} via {link.get('source')}")
            h += (f"<br><small>job-auto: applied to {link.get('company')} "
                  f"({link.get('title')}) &middot; {link.get('status')} via {link.get('source')}</small>")
        if it.get("reason"):
            ln += f"\n      why: {it['reason']}"
        lines.append(ln + "\n")
        html.append(h + "</small></li>")
    html.append("</ul>")
    return subject, "\n".join(lines), "".join(html)


def create_digest_draft(items: list[dict]) -> str | None:
    if not items:
        return None
    if not SELF_ADDR:
        sys.stderr.write("[digest] GHOSTMAIL_SELF_EMAIL not set; skipping draft\n")
        return None
    from ghostmail.gmail_gateway import get_gateway
    subject, text, html = _build_digest_text(items)
    msg = MIMEText(html, "html", "utf-8")
    msg["to"] = SELF_ADDR
    msg["from"] = SELF_ADDR
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        d = get_gateway().create_draft({"raw": raw})
        return d.get("id")
    except Exception as e:
        sys.stderr.write(f"[digest] draft failed: {e}\n")
        return None


def notify_desktop(title: str, text: str) -> None:
    """Best-effort Windows balloon notification (PS 5.1, no extra modules)."""
    title = (title or "").replace('"', "'")[:120]
    text = (text or "").replace('"', "'")[:240]
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        f'$n.BalloonTipTitle="{title}";$n.BalloonTipText="{text}";'
        "$n.Visible=$true;$n.ShowBalloonTip(10000);Start-Sleep -Seconds 7;$n.Dispose()"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        sys.stderr.write(f"[notify] failed: {e}\n")


def run_digest(dry_run: bool = False, actions_path: Path = ACTIONS_F) -> dict:
    """Build a digest from un-notified action items; draft + notify; mark notified.

    dry_run=True is pure: no file writes, no Gmail calls, no notifications.
    """
    actions = _load_actions(actions_path)
    # Expire stale items (action-state hygiene)
    actions, num_expired = _expire_stale_actions(actions)
    if num_expired and not dry_run:
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        json.dump(actions, open(actions_path, "w", encoding="utf-8"), indent=2)
        print(f"[hygiene] auto-resolved {num_expired} stale action item(s) "
              f">{STALE_ACTION_DAYS} days old")
    elif num_expired:
        print(f"[hygiene] DRY-RUN: would auto-resolve {num_expired} stale action item(s) "
              f">{STALE_ACTION_DAYS} days old (no writes)")
    pending = [a for a in actions if not a.get("notified") and not a.get("resolved")]
    # collapse near-duplicates (same action+company+role) for a clean digest
    seen_keys, deduped = set(), []
    for a in pending:
        key = (a.get("action"), (a.get("company") or "").lower(),
               (a.get("role") or "")[:40].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(a)
    print(f"Action digest: {len(deduped)} new ({len(pending)} incl. dupes) / {len(actions)} total tracked")
    if not pending:
        return {"new": 0, "total": len(actions), "draft": None}
    for a in deduped:
        tag = _ICON.get(a.get("action"), "[ACTION]")
        print(f"  {tag} {a.get('company','?')} - {a.get('role') or a.get('subject','')[:50]}")
    if dry_run:
        return {"new": len(deduped), "total": len(actions), "draft": "(dry-run)"}

    draft_id = create_digest_draft(deduped)
    counts: dict[str, int] = {}
    for a in deduped:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: _ORDER.get(x[0], 9)))
    notify_desktop(f"GhostMail: {len(deduped)} job action item(s)",
                   f"{summary}. Review the draft in Gmail.")
    for a in pending:
        a["notified"] = True
    json.dump(actions, open(actions_path, "w", encoding="utf-8"), indent=2)
    return {"new": len(deduped), "total": len(actions), "draft": draft_id}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(json.dumps(run_digest(dry_run=dry), indent=2))
