#!/usr/bin/env python3
"""
Job-world email classifier (deterministic Stage-1).

Separates inbound job/recruiting mail into ACTION-REQUIRED vs NOISE, which is the
capstone to job-auto's outbound auto-apply pipeline:

  - job_alert     -> NOISE   (Lensa/Recruit.net/LinkedIn job-alert digests, etc.)
  - app_update    -> NOISE   ("application received", "thanks for applying", rejections)
  - assessment    -> ACTION  (HackerRank/Codility/etc. or "complete your assessment")
  - interview     -> ACTION  (recruiter asking to talk / phone screen / availability)
  - offer         -> ACTION  (offer letter / job offer)
  - recruiter     -> review  (genuine human recruiter, action unclear -> let LLM decide)
  - none          -> not job-world; caller falls back to general heuristics

Pure functions only (no I/O, no network) so this is unit-testable and cheap.
The optional DeepSeek Stage-2 confirm lives in action_triage.py and only runs on
emails this module flags `needs_llm=True`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---- Gmail label routing ---------------------------------------------------
GMAIL_LABEL = {
    "job_alert": "GM/Job-Alerts",
    "app_update": "GM/Job-Apps/Updates",
    "assessment": "GM/Action/Assessment",
    "interview": "GM/Action/Interview",
    "offer": "GM/Action/Offer",
    "scheduling": "GM/Action/Interview",
    "recruiter": "GM/Recruiter",
}
ACTION_CATEGORIES = {"assessment", "interview", "offer", "scheduling"}
NOISE_CATEGORIES = {"job_alert", "app_update"}


# ---- Sender lists ----------------------------------------------------------
# Job-ALERT digests: subscriptions, NOT human recruiters. Checked FIRST so a job
# title like "DevOps Test Engineer" inside a digest never reads as an assessment.
JOB_ALERT_SENDERS = (
    "lensa.com", "recruit.net", "jobleads.com", "email.jobleads.com",
    "jobs-listings@linkedin.com", "jobalerts-noreply@linkedin.com",
    "alerts@ziprecruiter.com", "ziprecruiter.com",
    "alert@indeed.com", "alerts@indeed.com", "jobalerts@indeed.com",
    "builtin.com", "email.nexxt.com", "wayup.com", "noreply@get.it",
    "alerts.jobot.com", "em.linkedin.com", "messages-noreply@linkedin.com",
    "alerts@dice.com", "dice.com", "glassdoor.com", "talent.com", "jooble",
    "theladders.com", "monster.com", "ladders.com", "getro.com", "otta.com",
    "jobcase.com", "snagajob", "joinrise", "ihire", "simplyhired",
)

# Assessment / screening PLATFORMS (presence in From = high-confidence action).
ASSESSMENT_SENDERS = (
    "hackerrank.com", "codility.com", "codesignal.com", "hackerearth.com",
    "coderbyte.com", "testgorilla.com", "vervoe.com", "wonderlic.com",
    "criteriacorp.com", "predictiveindex.com", "plum.io", "harver.com",
    "modernhire.com", "hirevue.com", "karat.io", "karat.com", "woven",
    "qualified.io", "devskiller.com", "imocha.io", "mettl.com", "glider.ai",
    "byteboard.dev", "coderpad.io", "filtered.ai", "canditech", "testdome.com",
    "skillsurvey", "berke", "tg.testgorilla.com", "alooba", "hackajob",
)

# Staffing agencies / consultancies (carried over + extended). These are humans
# pitching specific reqs; they are NOT noise, but action depends on content.
AGENCY_SENDERS = (
    "cerebra-consulting", "nityo.com", "tcs.com", "infosys.com", "wipro.com",
    "roberthalf", "teksystems", "insightglobal", "cybercoders", "randstad",
    "jobot.com", "aerotek", "kforce", "neuralssinc.com", "absoluting.com",
    "plexusrs.com", "hcl.com", "cognizant.com", "techmahindra",
    "ustechsolutionsinc.com", "1pointsys.com", "diceemail", "apexsystems",
    "motionrecruitment", "signaturecg", "collabera", "mindlance", "artech",
    "judge.com", "modis", "experis", "nttdata", "ltimindtree", "virtusa",
)

# Real human via LinkedIn InMail -> almost always worth a look.
INMAIL_SENDERS = ("inmail-hit-reply@linkedin.com",)

# Pure marketing / non-job sources whose mail mimics action phrases ("offer",
# "complete your profile", meeting links) -> treat as non-action, never a lead.
# Precise sender matching only, so real recruiter mail is never hidden (e.g. a
# human @roberthalf.com is unaffected by the no.reply@ marketing rule below).
MARKETING_SENDERS = (
    "wonsulting.com",
    "sofi.com",                      # loan/credit 'excited to offer you' promos
    "e-mail.amtrak.com",             # Guest Rewards loyalty promos
    "@tavus.io",                     # product marketing carrying meeting links
    "email.meetup.com",              # Meetup event-marketing relay
    "ziprent.com",                   # property-mgmt robocall notices
    "no.reply@email.roberthalf.com",  # Robert Half content marketing only
    "m.weworkremotely.com",          # WWR onboarding/profile nudges
    "career.io",                     # Career.io resume-platform marketing/profile nudges
    "jobright.ai",                   # JobRight AI-copilot marketing/job-match nudges
)

NOREPLY_TOKENS = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "notifications@", "notification@", "alerts@", "alert@", "mailer@",
    "automated", "jobalert", "job-alert", "jobalerts",
)


# ---- Keyword/phrase signals (matched on subject + snippet, lowercased) ------
RX_OFFER = re.compile(
    r"\b(offer letter|job offer|offer of employment|we(?:'| a)?re? (?:pleased|excited|delighted) to offer|"
    r"would like to (?:extend|offer)|formal offer|verbal offer)\b"
)
RX_ASSESSMENT = re.compile(
    r"\b((?:complete|finish|take|start) (?:your |the |an? )?(?:assessment|coding|online|technical|skills)|"
    r"(?:would like you to|requested you(?:'?ve)?|asked you to|invite[sd]? you to|need you to) (?:please )?(?:to )?complete|"
    r"(?:coding|technical|online|skills?|take[- ]?home|pre[- ]?employment|aptitude) (?:assessment|test|challenge|exercise|evaluation)|"
    r"assessment (?:invitation|link|invite|request)|invited to complete|complete the following|"
    r"questionnaire|screening questions|additional questions|short (?:quiz|test|assessment)|"
    r"hackerrank|codility|codesignal|hirevue|video (?:interview|assessment))\b"
)
RX_INTERVIEW = re.compile(
    r"\b(interview|phone screen|phone call|screening call|schedule a (?:call|time|chat|conversation)|"
    r"set up a (?:time|call)|are you (?:available|interested|open)|would you be (?:available|interested|open)|"
    r"availability|next steps?|let'?s (?:connect|chat|talk)|hop on a (?:call|quick call)|"
    r"speak with you|chat about|book a time|grab (?:15|30) minutes|i came across your|"
    r"your (?:profile|background|experience)|reaching out (?:about|regarding)|"
    r"opportunity (?:that|i think)|think you(?:'| woul)d be (?:a )?(?:great|good|strong) fit)\b"
)
RX_SCHEDULING = re.compile(
    r"(calendly\.com|when2meet|\binvitation:|\baccepted:|\bcalendar invite|\.ics\b|"
    r"\bgoogle meet\b|\bzoom\.us/j/|\bteams\.microsoft\.com/l/meetup)"
)
RX_RECEIPT = re.compile(
    r"\b(application (?:received|submitted|confirmation|complete)|thank(?:s| you) for (?:applying|your application|your interest)|"
    r"we(?:'| ha)?ve received your application|your application (?:to|for|has been|was (?:sent|submitted|received))|"
    r"application (?:was|has been) (?:sent|submitted|received)|successfully (?:applied|submitted)|"
    r"viewed your application|your application was viewed|update on your application|"
    r"we received your)\b"
)
RX_REJECTION = re.compile(
    r"\b(unfortunately|we (?:have |regret to |'ve )?(?:decided|inform)|move forward with other|"
    r"not (?:moving|move) forward|no longer (?:under consideration|being considered)|"
    r"(?:position|role|req) (?:has been|is) (?:filled|closed|no longer)|pursue other candidates|"
    r"will not be moving forward|after (?:careful |much )?(?:consideration|review)|"
    r"not (?:selected|be progressing)|decided to proceed with other)\b"
)
RX_INTERVIEW_REQUEST_SUBJECT = re.compile(
    r"\b(invitation to interview|interview (?:invitation|request|invite)|"
    r"request(?:ing)? (?:an |a )?interview)\b"
)

PERSON_NAME_RX = re.compile(r"^\s*\"?([A-Z][a-z]+)\s+([A-Z][a-z'][\w'-]+)\b")


@dataclass
class JobClass:
    category: str           # job_alert|app_update|assessment|interview|offer|scheduling|recruiter|none
    action: str             # interview|assessment|offer|none
    subtype: str = ""       # receipt|rejection|viewed|inmail|agency|human|platform|...
    priority: str = "normal"  # urgent|high|normal|low
    confidence: float = 0.0
    needs_llm: bool = False
    signals: list[str] = field(default_factory=list)

    @property
    def gmail_label(self) -> str:
        return GMAIL_LABEL.get(self.category, "GM/Uncategorized")

    @property
    def is_action(self) -> bool:
        return self.category in ACTION_CATEGORIES

    @property
    def is_noise(self) -> bool:
        return self.category in NOISE_CATEGORIES


def _has(senders, frm: str) -> bool:
    return any(s in frm for s in senders)


def _looks_human(frm: str) -> bool:
    """Display name looks like a real person and address isn't a no-reply bot."""
    if any(tok in frm for tok in NOREPLY_TOKENS):
        return False
    return bool(PERSON_NAME_RX.match(frm))


def classify_job_email(frm: str, subject: str, snippet: str = "") -> JobClass:
    """Deterministic Stage-1 classification. Returns category 'none' if not job-world."""
    frm_raw = frm or ""          # keep original case for person-name detection
    frm = frm_raw.lower()
    sub = (subject or "").lower()
    snip = (snippet or "").lower()
    text = f"{sub} {snip}"

    # 1) Job-ALERT digests first (they contain job titles that mimic other signals)
    #    GUARD: a real offer/assessment/explicit-interview can arrive via a board
    #    relay, so don't demote those to noise.
    is_alert_sender = _has(JOB_ALERT_SENDERS, frm) or any(
        t in frm for t in ("jobalert", "job-alert", "jobalerts"))
    if is_alert_sender and not (
        RX_OFFER.search(text) or RX_ASSESSMENT.search(text)
        or RX_INTERVIEW_REQUEST_SUBJECT.search(sub)
    ):
        return JobClass("job_alert", "none", "digest", "low", 0.95,
                        signals=["alert-sender"])

    # 1b) Known marketing-dressed-as-recruiting -> bail out before any action match
    #     ("I'm guaranteeing your next job offer" etc. is a newsletter, not an offer)
    if _has(MARKETING_SENDERS, frm):
        return JobClass("none", "none", "marketing", "low", 0.0)

    # 2) Offer (rare, highest value)
    if RX_OFFER.search(text):
        return JobClass("offer", "offer", "offer", "urgent", 0.9, signals=["offer-phrase"])

    # 3) Assessment / screening test
    if _has(ASSESSMENT_SENDERS, frm):
        return JobClass("assessment", "assessment", "platform", "high", 0.95,
                        signals=["assessment-sender"])
    if RX_ASSESSMENT.search(text):
        return JobClass("assessment", "assessment", "keyword", "high", 0.8,
                        needs_llm=True, signals=["assessment-phrase"])

    # 4) Application receipts / rejections -> NOISE (catch before interview to avoid
    #    "thank you for applying, we'll be in touch about next steps" false positives).
    #    BUT if it comes from a human (not a board/ATS no-reply), a phrase like
    #    "application for <role>" may be recruiter outreach -> let the LLM rescue it.
    human_sender = _looks_human(frm_raw)
    board_bot = any(t in frm for t in NOREPLY_TOKENS) or "@linkedin.com" in frm or "greenhouse" in frm or "lever" in frm or "ashby" in frm or "myworkday" in frm
    if RX_REJECTION.search(text):
        return JobClass("app_update", "none", "rejection", "low",
                        0.6 if (human_sender and not board_bot) else 0.85,
                        needs_llm=human_sender and not board_bot,
                        signals=["rejection-phrase"])
    if RX_RECEIPT.search(text):
        return JobClass("app_update", "none", "receipt", "low",
                        0.6 if (human_sender and not board_bot) else 0.9,
                        needs_llm=human_sender and not board_bot,
                        signals=["receipt-phrase"])

    # 5) Scheduling / calendar
    if RX_SCHEDULING.search(text):
        return JobClass("scheduling", "interview", "calendar", "high", 0.85,
                        signals=["calendar-link"])

    # 6) Interview request — explicit subject is high confidence; otherwise need a
    #    human-ish sender OR a reply thread before trusting generic "next steps".
    if RX_INTERVIEW_REQUEST_SUBJECT.search(sub):
        return JobClass("interview", "interview", "explicit", "high", 0.9,
                        signals=["interview-subject"])
    if RX_INTERVIEW.search(text):
        human = _looks_human(frm_raw)
        inmail = _has(INMAIL_SENDERS, frm)
        is_reply = sub.startswith("re:")
        conf = 0.8 if (human or inmail) else (0.6 if is_reply else 0.45)
        return JobClass(
            "interview" if conf >= 0.6 else "recruiter",
            "interview" if conf >= 0.6 else "none",
            "human" if human else ("inmail" if inmail else "signal"),
            "high" if conf >= 0.6 else "normal",
            conf,
            needs_llm=conf < 0.8,
            signals=["interview-phrase"] + (["human-sender"] if human else []),
        )

    # 7) LinkedIn InMail without an obvious signal -> real human, let LLM judge.
    if _has(INMAIL_SENDERS, frm):
        return JobClass("recruiter", "none", "inmail", "high", 0.7,
                        needs_llm=True, signals=["linkedin-inmail"])

    # 8) Staffing agency / consultancy human
    if _has(AGENCY_SENDERS, frm):
        return JobClass("recruiter", "none", "agency", "normal", 0.65,
                        needs_llm=True, signals=["agency-sender"])

    # 9) Fallback: a human-looking sender pitching a role at an unknown domain
    #    (recovers genuine small-agency recruiters the lists don't know yet).
    job_title = re.search(
        r"\b(developer|engineer|architect|programmer|\.net|c#|software|devops|full[- ]stack|"
        r"back[- ]?end|front[- ]?end|data|cloud|python|java|ml|ai)\b", sub)
    opportunity = re.search(
        r"\b(opportunity|position|hiring|role|opening|req\b|requirement|contract|w2|c2c|"
        r"immediate|urgent)\b", sub)
    if job_title and opportunity and _looks_human(frm_raw):
        return JobClass("recruiter", "none", "human", "normal", 0.55,
                        needs_llm=True, signals=["human-job-pitch"])

    # 9b) A reply thread about a role from a human is likely an active conversation
    #     -> let the LLM read it rather than dropping to uncategorized.
    if sub.startswith("re:") and job_title and _looks_human(frm_raw):
        return JobClass("recruiter", "none", "reply-thread", "normal", 0.5,
                        needs_llm=True, signals=["reply-job-thread"])

    return JobClass("none", "none", "", "normal", 0.0)
