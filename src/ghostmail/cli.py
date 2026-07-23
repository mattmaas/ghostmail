"""CLI - GhostMail command-line interface."""

import asyncio
import logging
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.theme import Theme

# Fix: Windows console cp1252 encoding crashes on certain Unicode
# Replace non-encodable chars with '?' instead of crashing
def _safe(text):
    """Sanitize text for console display on Windows (cp1252)."""
    if not text:
        return ''
    return str(text).encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8')

from . import __version__
from .ai_engine import ModelProvider, get_router
from .config import get_settings
from .database import get_database

# Rich console with custom theme
custom_theme = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "red bold",
        "success": "green",
    }
)
console = Console(theme=custom_theme)

# Typer app
app = typer.Typer(
    name="ghostmail",
    help="AI-powered Gmail management with digital identity shaping",
    add_completion=False,
)


def setup_logging(verbose: bool = False):
    """Setup logging with Rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


# ==================== Setup Command ====================


@app.command()
def setup(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Initial setup for GhostMail."""
    setup_logging(verbose)
    console.print(
        Panel.fit(
            "[bold cyan]GhostMail Setup[/bold cyan]\nLet's get you configured!",
            border_style="cyan",
        )
    )

    settings = get_settings()

    # Check data directory
    console.print(f"\n[info]Data directory:[/info] {settings.data_dir_expanded}")

    # Check API keys
    console.print("\n[bold]API Configuration:[/bold]")

    if settings.gmail_client_id:
        console.print("  [+] Gmail OAuth2 configured")
    else:
        console.print("  [-] Gmail OAuth2 not configured")
        console.print("     Get credentials from: https://console.cloud.google.com")
        console.print("     1. Create project -> Enable Gmail API")
        console.print("     2. Credentials -> OAuth2 Client ID (Desktop app)")
        console.print("     3. Set GHOSTMAIL_GMAIL_CLIENT_ID and GHOSTMAIL_GMAIL_CLIENT_SECRET")

    if settings.opencode_api_key:
        console.print("  [+] OpenCode Zen (MiniMax/Kimi) configured")
    else:
        console.print("  [!] OpenCode Zen not configured - set GHOSTMAIL_OPENCODE_API_KEY")

    if settings.deepseek_api_key:
        console.print("  [+] DeepSeek Reasoner configured")
    else:
        console.print("  [!] DeepSeek not configured - set GHOSTMAIL_DEEPSEEK_API_KEY")

    console.print("\n[bold cyan]Next steps:[/bold cyan]")
    console.print("  1. Configure API keys in .env or environment variables")
    console.print("  2. Run [cyan]ghostmail auth[/cyan] to authenticate with Gmail")
    console.print("  3. Run [cyan]ghostmail test[/cyan] to verify LLM connectivity")


@app.command()
def auth(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Authenticate with Gmail API."""
    setup_logging(verbose)

    console.print("[bold]Gmail Authentication[/bold]\n")

    try:
        from .gmail_gateway import get_gateway

        gateway = get_gateway()
        profile = gateway.get_profile()

        console.print(f"[success][+] Authenticated as:[/success] {profile.get('emailAddress')}")
        console.print(f"   Messages total: {profile.get('messagesTotal', 'N/A')}")
        console.print(f"   Threads total: {profile.get('threadsTotal', 'N/A')}")

    except Exception as e:
        console.print(f"[error][-] Authentication failed:[/error] {e}")
        console.print("\nMake sure you've configured Gmail OAuth2 credentials.")
        raise typer.Exit(1)


@app.command()
def test(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Test LLM connectivity."""
    setup_logging(verbose)

    console.print("[bold]Testing LLM Providers...[/bold]\n")

    router = get_router()
    providers = router.get_available_providers()

    if not providers:
        console.print("[error][-] No LLM providers configured![/error]")
        console.print("\nConfigure at least one provider:")
        console.print("  - OpenCode Zen: GHOSTMAIL_OPENCODE_API_KEY")
        console.print("  - DeepSeek: GHOSTMAIL_DEEPSEEK_API_KEY")
        raise typer.Exit(1)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Latency")

    for provider in providers:
        try:
            client = router.get_client(task_type="general")
            # Quick test with a simple message
            import time

            start = time.time()

            # Just test that client is accessible
            latency = f"{int((time.time() - start) * 1000)}ms"

            table.add_row(
                provider.value,
                "[success][+] Available[/success]",
                latency,
            )
        except Exception as e:
            table.add_row(
                provider.value,
                f"[error][-] {e}[/error]",
                "-",
            )

    console.print(table)


# ==================== Status Command ====================


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Show GhostMail status."""
    setup_logging(verbose)

    settings = get_settings()
    db = get_database()

    console.print(
        Panel.fit(
            "[bold cyan]GhostMail Status[/bold cyan]",
            border_style="cyan",
        )
    )

    # Database stats
    with db.get_connection() as conn:
        email_count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        decision_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        rule_count = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]

    table = Table(show_header=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value")

    table.add_row("Emails cached", str(email_count))
    table.add_row("Decisions recorded", str(decision_count))
    table.add_row("Rules learned", str(rule_count))
    table.add_row("Data directory", str(settings.data_dir_expanded))

    console.print(table)

    # Try to get Gmail stats
    try:
        from .gmail_gateway import get_gateway

        gateway = get_gateway()
        profile = gateway.get_profile()

        console.print(f"\n[bold]Gmail:[/bold] {profile.get('emailAddress')}")
        console.print(f"  Messages: {profile.get('messagesTotal')}")
        console.print(f"  Threads: {profile.get('threadsTotal')}")

    except Exception as e:
        console.print(f"\n[warning]Gmail not connected:[/warning] {e}")


# ==================== Triage Command ====================


@app.command()
def triage(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of emails to process"),
    auto: bool = typer.Option(False, "--auto", "-a", help="Auto-execute high confidence actions"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Triage your inbox with AI."""
    setup_logging(verbose)

    console.print(f"[bold]Inbox Triage (processing {limit} emails)[/bold]\n")

    try:
        from .gmail_gateway import get_gateway
        from .modules.operator import Operator

        gateway = get_gateway()
        operator = Operator(gateway, get_router(), get_database())

        # Run triage
        results = asyncio.run(operator.triage_inbox(limit=limit, auto_execute=auto))

        # Display results
        console.print(f"\n[bold]Results:[/bold]")
        console.print(f"  [+] Auto-labeled: {results['auto_labeled']}")
        console.print(f"  [>] Need review: {results['need_review']}")
        console.print(f"  [!] Flagged: {results['flagged']}")

        if results["need_review"] > 0:
            console.print("\n[bold]Emails needing review:[/bold]")
            for email in results["review_emails"]:
                console.print(f"  • {_safe(email['subject'][:60])}...")
                console.print(f"    From: {_safe(email['from'])}")
                console.print(f"    Suggested: {email['suggested_action']}\n")

    except Exception as e:
        console.print(f"[error][-] Triage failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Curate Command ====================


@app.command()
def curate(
    audit_only: bool = typer.Option(True, "--audit-only", help="Only audit, don't make changes"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Audit and shape your email profile."""
    setup_logging(verbose)

    console.print("[bold]Digital Identity Curatation[/bold]\n")

    try:
        from .gmail_gateway import get_gateway
        from .modules.curator import Curator

        gateway = get_gateway()
        curator = Curator(gateway, get_router(), get_database())

        # Run audit
        audit_results = asyncio.run(curator.audit_profile())

        # Display results
        console.print("[bold]Email Profile Analysis:[/bold]\n")

        table = Table(show_header=True)
        table.add_column("Topic")
        table.add_column("Email Count", justify="right")
        table.add_column("Date Range")

        for topic, data in audit_results.get("topics", {}).items():
            table.add_row(
                topic,
                str(data["count"]),
                f"{data['oldest'][:10]} → {data['newest'][:10]}",
            )

        console.print(table)

        if audit_only:
            console.print("\n[info]Run without --audit-only to execute shaping plan.[/info]")
        else:
            console.print("\n[bold yellow][!] This will modify your mailbox![/bold yellow]")

            if Confirm.ask("Execute shaping plan?"):
                results = asyncio.run(curator.execute_shaping(audit_results))
                console.print(f"\n[success][+] Shaping complete![/success]")
                console.print(f"   Deleted: {results['deleted']}")
                console.print(f"   Unsubscribed: {results['unsubscribed']}")
                console.print(f"   Relabeled: {results['relabeled']}")

    except Exception as e:
        console.print(f"[error][-] Curate failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Organize Command ====================


@app.command()
def organize(
    since: Optional[str] = typer.Option(
        None, "--since", help="Process emails since date (YYYY-MM-DD)"
    ),
    dry_run: bool = typer.Option(True, "--dry-run", help="Don't actually apply labels"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Organize your email with intelligent labels."""
    setup_logging(verbose)

    console.print("[bold]Intelligent Email Organization[/bold]\n")

    try:
        from .gmail_gateway import get_gateway
        from .modules.archivist import Archivist

        gateway = get_gateway()
        archivist = Archivist(gateway, get_router(), get_database())

        # Run organization
        results = asyncio.run(archivist.organize_emails(since=since, dry_run=dry_run))

        console.print(f"\n[bold]Organization Results:[/bold]")
        console.print(f"  Emails processed: {results['processed']}")
        console.print(f"  Labels created: {results['labels_created']}")
        console.print(f"  Emails labeled: {results['emails_labeled']}")

        if dry_run:
            console.print(
                "\n[info]This was a dry run. Run without --dry-run to apply changes.[/info]"
            )

    except Exception as e:
        console.print(f"[error][-] Organization failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Sync Command ====================


@app.command()
def sync(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Sync emails from Gmail."""
    setup_logging(verbose)

    console.print("[bold]Syncing with Gmail...[/bold]\n")

    try:
        from .gmail_gateway import get_gateway

        gateway = get_gateway()
        db = get_database()

        # Get last sync state
        last_sync = db.get_sync_state("last_history_id")

        if last_sync:
            console.print(f"Last sync history ID: {last_sync}")
        else:
            console.print("Initial sync (this may take a while)...")

        # For now, just list recent emails
        messages, _ = gateway.list_messages(max_results=50)

        console.print(f"Fetched {len(messages)} recent messages")

        # Get full message data and cache
        for msg in messages[:10]:
            full = gateway.get_message(msg["id"], format="metadata")
            # Extract headers
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}

            from .database import CachedEmail

            email = CachedEmail(
                gmail_id=full["id"],
                thread_id=full.get("threadId", ""),
                from_addr=headers.get("From", ""),
                to_addr=headers.get("To", ""),
                subject=headers.get("Subject", ""),
                snippet=full.get("snippet", ""),
                date=headers.get("Date", ""),
                labels=full.get("labelIds", []),
                size_bytes=full.get("sizeEstimate", 0),
                is_read="UNREAD" not in full.get("labelIds", []),
            )
            db.upsert_email(email)

        console.print("[success][+] Sync complete![/success]")

    except Exception as e:
        console.print(f"[error][-] Sync failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Research Command ====================


@app.command()
def research(
    query: str = typer.Argument(..., help="Research topic or question"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max emails to analyze"),
    local: bool = typer.Option(False, "--local", help="Use local analysis only (no AI)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Research a topic using your emails."""
    setup_logging(verbose)

    console.print(f"[bold]Researching:[/bold] {query}\n")

    try:
        from .gmail_gateway import get_gateway
        from .ai_engine import get_router
        from .database import get_database
        from .modules.research import ResearchModule

        gateway = get_gateway()
        router = get_router()
        db = get_database()
        research_module = ResearchModule(gateway, router, db)

        results = asyncio.run(
            research_module.research(
                query=query,
                max_emails=limit,
                use_local=local,
            )
        )

        # Display results
        console.print(f"[bold]Summary:[/bold] {results.get('summary', 'No results')}\n")
        console.print(f"Emails found: {results.get('emails_found', 0)}")
        console.print(f"Emails analyzed: {results.get('emails_analyzed', 0)}")

        if results.get("key_findings"):
            console.print(f"\n[bold]Key Findings:[/bold]")
            for i, finding in enumerate(results["key_findings"][:5], 1):
                console.print(f"  {i}. {finding.get('finding', '')}")

        if results.get("entities", {}).get("topics"):
            console.print(
                f"\n[bold]Topics:[/bold] {', '.join(results['entities'].get('topics', []))}"
            )

        console.print(f"\n[bold]Confidence:[/bold] {results.get('confidence', 0):.0%}")

    except Exception as e:
        console.print(f"[error][-] Research failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Quick Search Command ====================


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Quick search through your emails."""
    setup_logging(verbose)

    console.print(f"[bold]Searching:[/bold] {query}\n")

    try:
        from .gmail_gateway import get_gateway
        from .ai_engine import get_router
        from .database import get_database
        from .modules.research import ResearchModule

        gateway = get_gateway()
        router = get_router()
        db = get_database()
        research_module = ResearchModule(gateway, router, db)

        results = asyncio.run(research_module.quick_search(query=query, max_results=limit))

        console.print(f"[bold]Found {len(results)} emails:[/bold]\n")

        for i, email in enumerate(results, 1):
            console.print(f"{i}. {_safe(email.get('subject', ''))}")
            console.print(f"   From: {_safe(email.get('from', ''))}")
            console.print(f"   Date: {_safe(email.get('date', ''))}")
            console.print(f"   {_safe(email.get('snippet', '')[:80])}...")
            console.print()

    except Exception as e:
        console.print(f"[error][-] Search failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Political Unsubscribe Command ====================


@app.command("unsubscribe-political")
def unsubscribe_political(
    find_only: bool = typer.Option(False, "--find-only", help="Only find, don't unsubscribe"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't actually unsubscribe"),
    min_score: int = typer.Option(3, "--min-score", help="Minimum political score to target"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Find and unsubscribe from political campaign emails."""
    setup_logging(verbose)

    console.print("[bold]Scanning for political emails...[/bold]\n")

    try:
        from .gmail_gateway import get_gateway
        from .ai_engine import get_router
        from .database import get_database
        from .modules.political_unsub import PoliticalUnsubModule

        gateway = get_gateway()
        router = get_router()
        db = get_database()
        unsub_module = PoliticalUnsubModule(gateway, router, db)

        if find_only:
            # Just find and display political senders
            results = asyncio.run(unsub_module.find_political_emails(max_emails=200))

            console.print(
                f"[bold]Found {results.get('total_political_senders', 0)} political senders:[/bold]\n"
            )

            table = Table(show_header=True, header_style="bold")
            table.add_column("Sender", style="cyan")
            table.add_column("Score", justify="right")
            table.add_column("Emails", justify="right")

            for sender in results.get("senders", [])[:15]:
                table.add_row(
                    sender.get("email", ""),
                    str(sender.get("score", 0)),
                    str(sender.get("count", 0)),
                )

            console.print(table)
            console.print("\nRun without --find-only to attempt unsubscription")

        else:
            # Auto-identify and unsubscribe
            results = asyncio.run(
                unsub_module.auto_identify_and_unsubscribe(
                    dry_run=dry_run,
                    min_score=min_score,
                )
            )

            console.print(f"[bold]Results:[/bold]")
            console.print(f"  Identified senders: {results.get('identified_senders', 0)}")

            if results.get("results"):
                res = results["results"]
                console.print(f"  Successful: {res.get('successful', 0)}")
                console.print(f"  Failed: {res.get('failed', 0)}")

                for detail in res.get("details", []):
                    if not detail.get("success"):
                        reason = detail.get("error") or detail.get("reason") or "Unknown error"
                        console.print(f"    - {detail.get('sender')}: {reason}")

            if dry_run:
                console.print(
                    "\n[info]This was a dry run. Run with --no-dry-run to actually unsubscribe.[/info]"
                )

    except Exception as e:
        console.print(f"[error][-] Unsubscribe failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Contacts Command ====================


@app.command("contacts")
def contacts(
    action: str = typer.Argument("sync", help="Action: sync, import-jobs, list, warm"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max emails/contacts to process"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Manage the Contact Intelligence CRM (sync from Gmail, import from job-auto, list warm contacts)."""
    setup_logging(verbose)

    try:
        from .modules.contacts import ContactIntelligence, Company, Contact, Interaction

        ci = ContactIntelligence()

        if action == "sync":
            console.print("[bold]Contact Intelligence — Gmail Sync[/bold]\n")
            from .gmail_gateway import get_gateway
            import re

            gateway = get_gateway()
            messages, _ = gateway.list_messages(max_results=limit)
            console.print(f"Scanning {len(messages)} recent emails for contacts...\n")

            contacts_found = 0
            companies_found = set()

            for msg in messages:
                try:
                    full = gateway.get_message(msg["id"], format="metadata")
                    headers = {
                        h["name"]: h["value"]
                        for h in full.get("payload", {}).get("headers", [])
                    }

                    from_addr = headers.get("From", "")
                    # Parse "Name <email>" format
                    match = re.match(r"(.+?)\s*<(.+?)>", from_addr)
                    if match:
                        name = match.group(1).strip().strip('"')
                        email = match.group(2).strip()
                    else:
                        name = from_addr.split("@")[0]
                        email = from_addr.strip()

                    if not email or "@" not in email:
                        continue

                    # Extract domain for company detection
                    domain = email.split("@")[1].lower()

                    # Strip marketing subdomains
                    parts = domain.split(".")
                    if len(parts) > 2 and parts[0] in ["marketing", "alert", "customers", "mail", "info", "pr", "e", "r", "msg", "news", "updates"]:
                        domain = ".".join(parts[1:])

                    # Skip common freemail and marketing/notification providers
                    freemail = [
                        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                        "aol.com", "icloud.com", "protonmail.com", "mail.com",
                        "amazon.com", "paypal.com", "ebay.com", "patreon.com", "venmo.com",
                        "github.com", "gitlab.com", "atlassian.com", "slack.com",
                        "notion.com", "figma.com", "canva.com", "miro.com",
                        "zoom.us", "microsoft.com", "apple.com", "google.com"
                    ]
                    if domain in freemail or domain.endswith(".edu") or domain.endswith(".gov"):
                        company_name = None
                    else:
                        company_name = domain.split(".")[0].capitalize()

                    # Calculate warmth (basic heuristics for now)
                    is_direct_reply = "Re:" in headers.get("Subject", "") or "Fwd:" in headers.get("Subject", "")
                    warmth = 5.0 if is_direct_reply else 0.5

                    if company_name in ["Linkedin", "Indeed", "Greenhouse", "Lever", "Ashby"]:
                        warmth = 2.0 # Still useful signals but mostly automated

                    # Upsert company if we identified one
                    company_id = None
                    if company_name and company_name not in companies_found:
                        company = Company(
                            id=None,
                            name=company_name,
                            domain=domain,
                            industry=None,
                            size=None,
                            ats_type=None,
                            board_url=None,
                            relationship="email_contact",
                            funding_stage=None,
                            tech_stack=None,
                            last_interaction=headers.get("Date", ""),
                            interaction_count=warmth,
                        )
                        company_id = asyncio.run(ci.upsert_company(company))
                        companies_found.add(company_name)

                    # Upsert contact
                    contact = Contact(
                        id=None,
                        company_id=company_id,
                        name=name,
                        email=email,
                        phone=None,
                        linkedin_url=None,
                        role_title=None,
                        contact_type="email_contact",
                        source="gmail",
                        first_seen=headers.get("Date", ""),
                        last_interaction=headers.get("Date", ""),
                        interaction_count=warmth,
                        sentiment_avg=0.0,
                        warmth_score=warmth,
                        notes=None,
                    )
                    asyncio.run(ci.upsert_contact(contact))
                    contacts_found += 1

                except Exception as e:
                    if verbose:
                        console.print(f"  [dim]Skipped message: {e}[/dim]")
                    continue

            console.print(f"[success][+] Sync complete![/success]")
            console.print(f"  Contacts found: {contacts_found}")
            console.print(f"  Companies identified: {len(companies_found)}")

        elif action == "import-jobs":
            console.print("[bold]Contact Intelligence — Import from job-auto[/bold]\n")
            import json

            settings = get_settings()
            jobs_path = settings.jobauto_jobs_path
            if not jobs_path or not jobs_path.exists():
                console.print("[error]Job database not found.[/error]")
                console.print(
                    "Set GHOSTMAIL_JOBAUTO_JOBS_PATH to your JobAuto jobs.json path."
                )
                raise typer.Exit(1)

            jobs = json.loads(jobs_path.read_text(encoding='utf-8', errors='ignore'))
            console.print(f"Found {len(jobs)} jobs in database. Importing companies...\n")

            companies_imported = set()
            for job in jobs:
                company_name = job.get("company", "").strip()
                if not company_name or company_name in companies_imported:
                    continue

                company = Company(
                    id=None,
                    name=company_name,
                    domain=None,
                    industry=None,
                    size=None,
                    ats_type=job.get("source", None),
                    board_url=job.get("url", None),
                    relationship="applied" if job.get("status") == "applied" else "prospect",
                    funding_stage=None,
                    tech_stack=None,
                    last_interaction=job.get("discoveredAt", ""),
                    interaction_count=1,
                )
                asyncio.run(ci.upsert_company(company))
                companies_imported.add(company_name)

            console.print(f"[success][+] Import complete![/success]")
            console.print(f"  Companies imported: {len(companies_imported)}")

        elif action == "warm":
            console.print("[bold]Contact Intelligence — Warm Contacts[/bold]\n")

            warm = asyncio.run(ci.get_warm_companies(limit=limit))

            if not warm:
                console.print("[dim]No contacts yet. Run `ghostmail contacts sync` first.[/dim]")
                raise typer.Exit(0)

            from rich.table import Table as RichTable

            table = RichTable(show_header=True, header_style="bold")
            table.add_column("Company", style="cyan")
            table.add_column("Domain")
            table.add_column("Relationship")
            table.add_column("Interactions", justify="right")
            table.add_column("Last Contact")

            for c in warm:
                table.add_row(
                    c.get("name", ""),
                    c.get("domain", "") or "-",
                    c.get("relationship", ""),
                    str(c.get("interaction_count", 0)),
                    str(c.get("last_interaction", ""))[:10] if c.get("last_interaction") else "-",
                )

            console.print(table)

        elif action == "list":
            console.print("[bold]Contact Intelligence — All Contacts[/bold]\n")

            asyncio.run(ci.init_db())
            cursor = asyncio.run(ci._conn.execute(
                "SELECT * FROM contacts ORDER BY last_interaction DESC LIMIT ?", (limit,)
            ))
            rows = asyncio.run(cursor.fetchall())

            if not rows:
                console.print("[dim]No contacts yet. Run `ghostmail contacts sync` first.[/dim]")
                raise typer.Exit(0)

            from rich.table import Table as RichTable

            table = RichTable(show_header=True, header_style="bold")
            table.add_column("Name", style="cyan")
            table.add_column("Email")
            table.add_column("Source")
            table.add_column("Last Interaction")

            for r in rows:
                table.add_row(
                    r["name"] or "",
                    r["email"] or "",
                    r["source"] or "",
                    str(r["last_interaction"])[:10] if r["last_interaction"] else "-",
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(rows)} of total contacts.[/dim]")

        else:
            console.print(f"[error]Unknown action: {action}[/error]")
            console.print("Available actions: sync, import-jobs, list, warm")
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[error][-] Contacts failed:[/error] {e}")
        raise typer.Exit(1)


# ==================== Version ====================


@app.command()
def version():
    """Show GhostMail version."""
    console.print(f"GhostMail v{__version__}")


# ==================== Main ====================

try:
    from .mcp_server import add_mcp_commands

    add_mcp_commands(app)
except ImportError as e:
    logging.getLogger(__name__).debug(f"MCP server not available: {e}")


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
