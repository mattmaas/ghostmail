"""Regression tests for the job-world classifier (Stage-1, deterministic).

Run: python -m pytest tests/test_job_classifier.py -q

All fixtures are synthetic. Sender domains that appear here do so because they
are entries in the shipped classifier lists (job-alert relays, assessment
platforms, staffing agencies, marketing deny-list) — no real mail is required.
"""
from ghostmail.job_classifier import classify_job_email as C


def test_job_alert_digests_are_noise():
    assert C("Lensa <jobalert@lensa.com>", "Don't miss these new Net Developer jobs").category == "job_alert"
    assert C('"Recruit.net Job Alert" <jobalert@lb2.recruit.net>', "20 New Engineer Jobs in Franklin TN").category == "job_alert"
    assert C("LinkedIn <jobs-listings@linkedin.com>", "DevOps Engineer role at Wave Reaction is available").category == "job_alert"


def test_application_receipts_and_rejections_are_noise():
    assert C("no-reply@greenhouse.io", "Application Received for Sr Integration Engineer").is_noise
    assert C("careers@examplecorp.com", "Thank you for your application to ExampleCorp").category == "app_update"
    assert C("no-reply@us.greenhouse-mail.io", "Update on your application").category == "app_update"
    # alert-domain senders (e.g. ZipRecruiter) are also noise, just a different bucket
    assert C("notifications@ziprecruiter.com", "your application was sent to ExampleCorp").is_noise


def test_assessment_requests_are_action():
    assert C("no-reply@hackerrank.com", "HackerRank test for Backend role").action == "assessment"
    assert C("recruiting@company.com", "Next Step: Online Questionnaire for Data Scientist").action == "assessment"
    # 'complete an assessment' (article 'an') must match
    assert C("noreply@examplemutual.com", "Example Mutual has requested you complete an assessment").action == "assessment"


def test_assessment_survives_alert_relay():
    # a real assessment arriving via a board relay must NOT be demoted to job_alert
    j = C("jobalerts-noreply@indeed.com", "would like you to complete a coding assessment")
    assert j.category == "assessment"


def test_interview_requests_are_action():
    # explicit interview subject is deterministic
    assert C("recruiter@firm.com", "Invitation to interview for Senior Engineer").action == "interview"
    # subject with a real interview phrase + human sender
    assert C("Jane Doe <jane@firm.com>", "Are you available for a phone screen this week?").action == "interview"
    # an InMail with only a role title has no keyword -> deferred to the LLM (Stage-2)
    j = C("Riley Carter <inmail-hit-reply@linkedin.com>", "Software Engineer position (C#, .Net) Fully Remote")
    assert j.needs_llm and j.subtype == "inmail"


def test_offer_is_action_but_marketing_is_not():
    assert C("hr@company.com", "We are pleased to offer you the position").action == "offer"
    # Wonsulting marketing must not read as a real offer
    assert C("Wonsulting <hello@wonsulting.com>", "I'm guaranteeing your next job offer").category != "offer"


def test_genuine_human_recruiter_kept_not_dropped():
    j = C("Recruiter One <recruiter.one@1pointsys.com>", "Hiring for EPIC Analyst || Philadelphia, PA(Hybrid)")
    assert j.category == "recruiter"
    j2 = C("Recruiter Two <recruiter.two@ustechsolutionsinc.com>", "We are hiring for the position of Frontend Engineer")
    assert j2.category == "recruiter"


def test_non_job_mail_falls_through():
    assert C("receipts@example-pay.io", "Your ExamplePay, Inc receipt [#0000-0000]").category == "none"
    assert C("noreply@example-rideshare.com", "Your ride with Alex").category == "none"


# ---- False-positive guard: marketing/non-action mail must never read as action.
# Fixtures pair the shipped deny-list senders with typical promo phrasing.

def test_marketing_offer_promos_are_not_offers():
    # loan promo matched the "offer" phrase -> must be non-action
    j = C("SoFi <SoFi@r.sofi.com>",
          "Hi: We are excited to offer you a fixed-rate personal loan")
    assert j.category == "none" and j.action == "none"
    # Robert Half content marketing (precise no.reply address only)
    j = C("Robert Half <no.reply@email.roberthalf.com>",
          "How to spot a fake job offer")
    assert j.category == "none"


def test_loyalty_and_product_marketing_are_not_interviews():
    assert C("Amtrak Guest Rewards <amtrak@e-mail.amtrak.com>",
             "Complete your profile to be able to redeem points.").category == "none"
    assert C("Tavus Team <hello@tavus.io>",
             "Tavus PALs Can Now Join Your Google Meet + 50% Off").category == "none"
    assert C("Tech Meetup Group <info@email.meetup.com>",
             "Just scheduled: summer meetup announcements").category == "none"
    assert C("Ziprent New Business <start@ziprent.com>",
             "Ziprent Property Management | Outgoing Phone Call To (555) 010-0100").category == "none"
    assert C("We Work Remotely <hello@m.weworkremotely.com>",
             "Four steps left on your WWR profile").category == "none"


def test_marketing_rules_do_not_hide_real_recruiter_mail():
    # a HUMAN at roberthalf.com (not the no.reply marketing address) still gets through
    j = C("Jane Doe <jane.doe@roberthalf.com>",
          "Are you available for a phone screen this week?")
    assert j.action == "interview"
    # agency humans at other guarded-adjacent domains are untouched
    j = C("Recruiter Three <recruiter.three@cerebra-consulting.com>",
          "NET Systems Developer (only W2) Location: Franklin, TN")
    assert j.category == "recruiter"


def test_real_action_mail_still_action_after_guard():
    # assessment request via an HR-platform sender must stay an action
    j = C("noreply@example-hr.com", "Example Mutual has requested you complete an assessment.")
    assert j.action == "assessment"
    # a bare "reminder" subject from an interview-scheduling address is
    # intentionally routed to the Stage-2 LLM, never dropped to none.
    j = C("ExampleCorp <scheduling@interview.examplecorp.com>",
          "Interview Reminder || Jordan Doe - Full Stack .NET Developer")
    assert j.category in ("interview", "recruiter")
    assert j.action == "interview" or j.needs_llm


def test_denylist_career_io_and_jobright_are_not_actions():
    # Career.io resume-platform nudges must never reach the LLM or actions
    j = C("Career.io <hello@career.io>",
          "Your profile is getting noticed - complete it now")
    assert j.category == "none" and j.action == "none" and not j.needs_llm
    j = C("Career.io <noreply@mail.career.io>",
          "Boost your resume with our premium offer")
    assert j.category == "none"
    # JobRight AI-copilot job-match marketing likewise
    j = C("JobRight <team@jobright.ai>",
          "New jobs matched your profile - connect with us")
    assert j.category == "none" and j.action == "none" and not j.needs_llm
    j = C("JobRight <noreply@mail.jobright.ai>",
          "You have 5 new matches for your background")
    assert j.category == "none"


def test_denylist_glassdoor_and_builtin_stay_noise():
    assert C("Glassdoor <noreply@glassdoor.com>",
             "New .NET Developer jobs for you").category == "job_alert"
    assert C("Built In <hello@builtin.com>",
             ".NET jobs you might like").category == "job_alert"
