from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import sys
import time
import json
import typer
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from datetime import datetime, time as dtime, timedelta
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

from email_workflow.models.config import AppConfig, AIMode
from email_workflow.models.email import EmailMessage, EmailCategory, ImportanceLevel, UrgencyLevel, DecisionOption, SenderInfo
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.providers.ai_factory import get_ai_provider
from email_workflow.providers.key_pool import find_pool, parallel_lanes
from email_workflow.providers.fake_ai import FakeAIProvider
from email_workflow.providers.email_provider import get_email_provider, MockEmailProvider
from email_workflow.providers.api_providers import PROVIDER_METADATA, OpenAICompatibleProvider
from email_workflow.providers.fallback_ai import FallbackAIProvider
from email_workflow.providers.ai_factory import build_provider_chain
from email_workflow.core.pipeline import WorkflowPipeline
from email_workflow.core.audit import AuditLogger
from email_workflow.core.thread_manager import ThreadManager
from email_workflow.core.notifications import NotificationDispatcher
from email_workflow.core.paths import resolve_project_file, find_env_file
from email_workflow.core.errors import WorkflowError
from email_workflow.core.auth import AuthManager, MIN_PASSWORD_LENGTH
from email_workflow.core.usage import UsageTracker
from email_workflow.core.scheduling import (
    current_system,
    describe_drift,
    existing_crontab,
    git_is_clean_of_secrets,
    install_hint,
    local_schedule_plan,
    merge_crontab,
    parse_time,
    repo_name_suggestion,
    secrets_from_env_file,
    tool_available,
    utc_cron_for_local_time,
    workflow_with_cron,
)
from email_workflow.cli.formatter import (
    print_banner,
    render_stage_result,
    render_providers_status,
    render_audit_log,
    render_thread_replay,
)

app = typer.Typer(
    name="email-workflow",
    help="Autonomous Email Workflow CLI Test Harness",
    add_completion=False,
    invoke_without_command=True,
)

console = Console()

def _unwrap(opt, default):
    """Safely unwrap Typer OptionInfo objects when functions are called programmatically."""
    if hasattr(opt, "default"):
        return opt.default
    return opt if opt is not None else default

def _prompt_api_key(env_var: str, prov_name: str) -> str:
    """Prompt user for API key with option for visible or masked input."""
    console.print(f"\n[bold cyan]Input API Key for {prov_name} ({env_var}):[/bold cyan]")
    hide = Confirm.ask("Mask key input on screen for privacy?", default=False)
    if hide:
        console.print("[dim](Keystrokes will be hidden on screen. Paste or type key and press Enter)[/dim]")
    key = Prompt.ask(f"API Key ({env_var})", password=hide)
    return key.strip()

def _write_env_var(key: str, value: str):
    """Save environment variable to .env file and current OS environment."""
    os.environ[key] = value
    env_file = find_env_file()
    lines = []
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

    key_found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            key_found = True
        else:
            new_lines.append(line)

    if not key_found:
        new_lines.append(f"{key}={value}\n")

    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def get_seconds_until_scheduled_time(time_str: str) -> tuple[int, str]:
    """Calculate seconds remaining until HH:MM time today or tomorrow."""
    try:
        parts = [int(p) for p in time_str.strip().split(":")]
        target_time = dtime(hour=parts[0], minute=parts[1])
    except Exception:
        raise ValueError(f"Invalid time format '{time_str}'. Expected HH:MM (e.g. 18:00).")

    now = datetime.now()
    target_dt = datetime.combine(now.date(), target_time)
    if target_dt <= now:
        target_dt += timedelta(days=1)

    secs = int((target_dt - now).total_seconds())
    target_str = target_dt.strftime("%Y-%m-%d %H:%M")
    return secs, target_str

def _load_config_quietly() -> AppConfig:
    """Config for startup checks, falling back to defaults if it is unreadable."""
    try:
        config_path = resolve_project_file("config.yaml")
        if config_path.exists():
            return AppConfig.load_from_file(config_path)
    except Exception:
        pass
    return AppConfig()


def _ask_password(label: str, config: Optional[AppConfig] = None) -> str:
    """Ask for the login password, on screen or hidden.

    A hidden prompt shows nothing at all while you type - not even dots - so a
    typo only surfaces when the two entries disagree. Showing it is the more
    helpful default for a login you set on your own machine.
    """
    if config is None:
        config = _load_config_quietly()
    return Prompt.ask(label, password=not config.security.show_password_while_typing)


def _create_login(auth: AuthManager, config: Optional[AppConfig] = None) -> None:
    console.print(
        Panel(
            "[bold]First run - create your login.[/bold]\n\n"
            "This app can read your mailbox and send email on your behalf, so it is\n"
            "locked behind a username and password.\n\n"
            "[dim]Your password is never saved. Only a salted hash of it is written\n"
            "to auth.json, which cannot be turned back into the password.\n"
            "What you type is shown on screen so you can check it. To hide it, set\n"
            "security.show_password_while_typing: false in config.yaml.[/dim]",
            title="Set up your login",
            border_style="cyan",
        )
    )
    while True:
        username = Prompt.ask("Choose a username", default=os.getenv("USERNAME") or "user")
        password = _ask_password(
            f"Choose a password (at least {MIN_PASSWORD_LENGTH} characters)", config
        )
        repeated = _ask_password("Type the password again", config)

        if password != repeated:
            console.print("[red]Those two passwords are different. Let's try again.[/red]\n")
            continue
        try:
            auth.set_credentials(username, password)
        except ValueError as e:
            console.print(f"[red]{e}[/red]\n")
            continue

        console.print("[bold green][OK] Login created. Keep this password safe.[/bold green]\n")
        return


def _require_login() -> None:
    """Ask who this is before the app opens. Exits if they cannot prove it."""
    config = _load_config_quietly()
    if not config.security.require_login:
        return

    # Unattended runs (a scheduled job, a server, GitHub Actions) have nobody
    # to type a password. This is the same deliberate choice as setting
    # security.require_login: false - anyone able to set an environment
    # variable on the machine could edit config.yaml just as easily - but it
    # keeps the local install locked while a scheduled copy runs headless.
    if os.getenv("EMAIL_WORKFLOW_DISABLE_LOGIN", "").strip() in ("1", "true", "yes"):
        return

    auth = AuthManager(store_path=config.security.store)

    if not auth.is_configured():
        _create_login(auth, config)
        return

    attempts = max(1, config.security.max_login_attempts)
    for remaining in range(attempts - 1, -1, -1):
        username = Prompt.ask("Username", default=auth.username)
        password = _ask_password("Password", config)

        if auth.verify(username, password):
            console.print(f"\n[bold green]Welcome back, {username}.[/bold green]\n")
            return

        if remaining:
            console.print(
                f"[red]Wrong username or password. "
                f"{remaining} attempt(s) left.[/red]\n"
            )

    console.print(
        Panel(
            "[bold red]Too many failed attempts.[/bold red]\n\n"
            "If you have forgotten your password, delete [cyan]auth.json[/cyan] in the\n"
            "project folder and the app will ask you to set a new login.",
            border_style="red",
        )
    )
    raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Default callback launching interactive main menu if no command argument is passed."""
    _require_login()
    if ctx.invoked_subcommand is None:
        interactive_main_menu()


@app.command()
def passwd():
    """Change the login password, or turn the login off."""
    print_banner("CHANGE LOGIN")
    config = _load_config_quietly()
    auth = AuthManager(store_path=config.security.store)

    if not auth.is_configured():
        console.print("[yellow]No login is set yet.[/yellow]")
        _create_login(auth, config)
        return

    if Confirm.ask("Remove the login entirely (anyone can open the app)?", default=False):
        auth.disable()
        console.print("[bold yellow]Login removed.[/bold yellow]")
        return

    while True:
        username = Prompt.ask("New username", default=auth.username)
        password = _ask_password(
            f"New password (at least {MIN_PASSWORD_LENGTH} characters)", config
        )
        repeated = _ask_password("Type the new password again", config)
        if password != repeated:
            console.print("[red]Those two passwords are different. Try again.[/red]\n")
            continue
        try:
            auth.set_credentials(username, password)
        except ValueError as e:
            console.print(f"[red]{e}[/red]\n")
            continue
        console.print("[bold green][OK] Password changed.[/bold green]")
        return

from email_workflow.core.known_facts import KnownFactsManager

def interactive_main_menu():
    """Interactive main menu with numbered options."""
    while True:
        console.clear()
        print_banner("AUTONOMOUS EMAIL WORKFLOW - MAIN MENU")

        menu_text = (
            "[bold cyan]Please select an action:[/bold cyan]\n\n"
            "[bold yellow]1.[/bold yellow] Run Email Pipeline "
            "[dim](only UNREAD mail from the last 7 days)[/dim]\n"
            "[bold yellow]2.[/bold yellow] Set Up a Daily Run "
            "[dim](this computer, or GitHub so it runs with the PC off)[/dim]\n"
            "[bold yellow]3.[/bold yellow] Interactive Setup Wizard (Provider, Key, Model, Gmail, Notifications)\n"
            "[bold yellow]4.[/bold yellow] View Active Settings Dashboard\n"
            "[bold yellow]5.[/bold yellow] Edit Personal Knowledge Base & Known Facts (Set schedules, project info, rules)\n"
            "[bold yellow]6.[/bold yellow] Run Offline Demo Mode (Zero API keys needed)\n"
            "[bold yellow]7.[/bold yellow] Check Provider API Key Status\n"
            "[bold yellow]8.[/bold yellow] View Audit Logs\n"
            "[bold yellow]9.[/bold yellow] Replay Thread Supersession History\n"
            "[bold yellow]10.[/bold yellow] Test Notification & Escalation Report Delivery\n"
            "[bold yellow]11.[/bold yellow] Sending & Mailbox Settings "
            "[dim](send replies? keep drafts?)[/dim]\n"
            "[bold yellow]12.[/bold yellow] Exit\n"
        )
        console.print(Panel(menu_text, border_style="cyan"))

        choice = Prompt.ask("Select option", choices=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"], default="1")

        if choice == "1":
            console.clear()
            run(config_file="config.yaml", mock_inbox=None, loop=False, interval=60, schedule=None)
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "2":
            console.clear()
            # The real thing: it registers a daily task with the operating
            # system, or sets it up on GitHub. What used to be here only
            # blocked this window until the chosen time and ran once -
            # closing the terminal cancelled it, which is not a schedule.
            # That is still there as "run --schedule HH:MM" for a one-off.
            schedule(at=None, where=None)
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "3":
            console.clear()
            setup()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "4":
            console.clear()
            dashboard()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "5":
            console.clear()
            facts()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "6":
            console.clear()
            demo()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "7":
            console.clear()
            providers()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "8":
            console.clear()
            tid = Prompt.ask("Filter by Thread ID (leave empty for all)", default="")
            log(thread_id=tid if tid.strip() else None, audit_file="audit.jsonl")
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "9":
            console.clear()
            tid = Prompt.ask("Enter Thread ID to replay", default="thread_meet_02")
            try:
                replay(thread_id=tid, state_file="state.json", audit_file="audit.jsonl")
            except Exception as e:
                console.print(f"[red]Replay error: {e}[/red]")
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "10":
            console.clear()
            test_report(channel=None)
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "11":
            console.clear()
            settings()
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "12":
            console.print("[bold green]Goodbye![/bold green]")
            sys.exit(0)

@app.command()
def dashboard():
    """Display current active configuration and system status dashboard."""
    print_banner("AUTONOMOUS EMAIL WORKFLOW - SYSTEM DASHBOARD")
    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()

    table = Table(title="Active Configuration Summary", border_style="cyan", show_header=True)
    table.add_column("Category", style="bold yellow")
    table.add_column("Setting", style="bold white")
    table.add_column("Value", style="green")

    # AI Settings
    table.add_row("AI Engine", "Mode", config.ai.mode.value)
    if config.ai.mode == AIMode.API:
        key_status = "[bold green]Set[/bold green]" if os.getenv(config.ai.api.api_key_env) else "[bold red]Missing[/bold red]"
        table.add_row("AI Engine", "Provider", config.ai.api.provider)
        table.add_row("AI Engine", "Model", config.ai.api.model)
        table.add_row("AI Engine", "API Key Status", key_status)
    else:
        table.add_row("AI Engine", "Ollama Endpoint", config.ai.local.endpoint)
        table.add_row("AI Engine", "Model", config.ai.local.model)

    # Email Settings
    table.add_row("Email Account", "Provider", config.email.provider)
    table.add_row("Email Account", "Account Ref", config.email.account_ref)
    table.add_row("Email Account", "Mailbox", config.email.mailbox)

    # Notifications Settings
    table.add_row("Notifications", "Channel", config.notifications.channel)
    table.add_row("Notifications", "Discord Webhook", "[bold green]Configured[/bold green]" if os.getenv("DISCORD_WEBHOOK_URL") else "[dim]Not set[/dim]")
    table.add_row("Notifications", "Email SMTP", "[bold green]Configured[/bold green]" if os.getenv("NOTIFICATION_SENDER_EMAIL") else "[dim]Not set[/dim]")
    table.add_row("Notifications", "WhatsApp", "[bold green]Configured[/bold green]" if os.getenv("TWILIO_ACCOUNT_SID") else "[dim]Not set[/dim]")

    # Automation Level
    table.add_row("Automation", "Level", config.automation.level.value)
    table.add_row("Automation", "Auto-Reply Threshold", f"{config.automation.confidence_threshold_auto_reply:.2f}")

    console.print(table)

@app.command()
def setup():
    """
    Interactive setup wizard to select AI Provider, Model, API Key, Gmail Account, and Notifications.
    No manually creating .env files needed!
    """
    print_banner("AUTONOMOUS EMAIL WORKFLOW - INTERACTIVE SETUP")

    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()

    # AI Configuration Section
    console.print("[bold yellow]Select AI Mode:[/bold yellow]")
    console.print("1. API Provider (OpenAI, Gemini, Groq, OpenRouter)")
    console.print("2. Offline Fake AI (Demo mode, no keys required)")
    console.print("3. Local Ollama HTTP Server")

    mode_choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="1")

    if mode_choice == "2":
        config.ai.mode = AIMode.API
        config.ai.api.provider = "fake"
        console.print("[bold green]Configured for Offline Fake AI![/bold green]")
    elif mode_choice == "3":
        config.ai.mode = AIMode.LOCAL
        endpoint = Prompt.ask("Ollama Endpoint", default="http://localhost:11434")
        model = Prompt.ask("Ollama Model Name", default="llama3.2")
        config.ai.local.endpoint = endpoint
        config.ai.local.model = model
        console.print(f"[bold green]Configured for Local Ollama ({model} at {endpoint})![/bold green]")
    else:
        config.ai.mode = AIMode.API
        console.print("\n[bold yellow]Select API Provider:[/bold yellow]")
        console.print("1. OpenAI (ChatGPT models, e.g. gpt-4o-mini)")
        console.print("2. Google Gemini (e.g. gemini-3-flash-preview)")
        console.print("3. Groq (e.g. llama-3.3-70b-versatile)")
        console.print("4. OpenRouter (e.g. anthropic/claude-3.5-sonnet)")

        prov_map = {"1": ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
                    "2": ("gemini", "gemini-3-flash-preview", "GEMINI_API_KEY"),
                    "3": ("groq", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
                    "4": ("openrouter", "anthropic/claude-3.5-sonnet", "OPENROUTER_API_KEY")}

        p_choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="1")
        prov_key, default_model, env_var = prov_map[p_choice]

        config.ai.api.provider = prov_key
        config.ai.api.api_key_env = env_var

        current_key = os.getenv(env_var, "")
        if current_key:
            console.print(f"[dim]Found existing key for {env_var}[/dim]")
            if Confirm.ask("Use existing key?", default=True):
                api_key = current_key
            else:
                api_key = _prompt_api_key(env_var, prov_key.upper())
        else:
            api_key = _prompt_api_key(env_var, prov_key.upper())

        if api_key:
            _write_env_var(env_var, api_key)
            console.print(f"[bold green]Saved {env_var} to .env![/bold green]")

        model_name = Prompt.ask(f"Enter Model Name for {prov_key.upper()}", default=default_model)
        config.ai.api.model = model_name

    # Email Account Section — single unified flow
    console.print("\n[bold yellow]Email Account Setup:[/bold yellow]")
    console.print("1. Real Gmail IMAP & SMTP (reads your actual inbox & sends real replies)")
    console.print("2. Mock Inbox Reader (test fixtures, no credentials needed)")
    ep_choice = Prompt.ask("Choice", choices=["1", "2"], default="1")

    if ep_choice == "1":
        import webbrowser
        config.email.provider = "gmail"
        app_pwd_url = "https://myaccount.google.com/apppasswords"
        console.print("\n[bold cyan]--- Real Gmail Setup ---[/bold cyan]")

        g_email = Prompt.ask("Gmail Address", default=os.getenv("GMAIL_ADDRESS", config.email.account_ref))

        # Check if App Password already saved
        existing_pwd = os.getenv("GMAIL_APP_PASSWORD", "")
        if existing_pwd:
            console.print(f"\n[bold green]You already have a Gmail App Password saved.[/bold green]")
            use_existing = Confirm.ask("Use the saved App Password?", default=True)
            if use_existing:
                g_pwd = existing_pwd
                console.print("[dim]Using saved App Password.[/dim]")
            else:
                g_pwd = None
        else:
            use_existing = False
            g_pwd = None

        if not g_pwd:
            console.print(
                f"\n[bold white]Step 1:[/bold white] Generate a Gmail App Password at:\n"
                f"  [bold cyan][link={app_pwd_url}]{app_pwd_url}[/link][/bold cyan]\n"
            )
            console.print("[dim](Opening the link in your browser now...)[/dim]")
            webbrowser.open(app_pwd_url)
            console.print(
                "\n[bold white]Step 2:[/bold white] On that page:\n"
                "  1. Type an app name (e.g. [bold]Email Workflow[/bold])\n"
                "  2. Click [bold]Create[/bold]\n"
                "  3. Copy the [bold]16-character password[/bold] shown (remove spaces)\n"
            )
            g_pwd = Prompt.ask("Gmail App Password (16 chars, visible so you can type/paste it)")

        config.email.account_ref = g_email
        config.email.mailbox = "INBOX"
        if g_email:
            _write_env_var("GMAIL_ADDRESS", g_email)
            _write_env_var("NOTIFICATION_SENDER_EMAIL", g_email)
        if g_pwd and not use_existing:
            _write_env_var("GMAIL_APP_PASSWORD", g_pwd)
            _write_env_var("NOTIFICATION_SENDER_PASSWORD", g_pwd)
        account_email = g_email
        console.print("[bold green]Gmail configured![/bold green]")
    else:
        config.email.provider = "mock"
        config.email.mailbox = "INBOX"
        account_email = config.email.account_ref
        console.print("[bold green]Configured Mock Inbox Reader![/bold green]")

    # Notifications Section
    console.print("\n[bold yellow]Select Notification Channel for Escalation & Batch Reports:[/bold yellow]")
    console.print("1. Terminal (Rich Console output)")
    console.print("2. Discord Webhook")
    console.print("3. Email (SMTP)")
    console.print("4. WhatsApp (Twilio)")
    console.print("5. All channels")

    notif_choice = Prompt.ask("Choice", choices=["1", "2", "3", "4", "5"], default="1")
    notif_map = {"1": "terminal", "2": "discord", "3": "email", "4": "whatsapp", "5": "all"}
    config.notifications.channel = notif_map[notif_choice]

    if notif_choice in ("2", "5"):
        console.print("\n[bold cyan]--- Discord Webhook Configuration ---[/bold cyan]")
        discord_url = Prompt.ask("Enter Discord Webhook URL", default=os.getenv("DISCORD_WEBHOOK_URL", ""))
        if discord_url:
            _write_env_var("DISCORD_WEBHOOK_URL", discord_url)

    if notif_choice in ("3", "5"):
        console.print("\n[bold cyan]--- Email SMTP Configuration ---[/bold cyan]")
        sender = Prompt.ask("Sender Email", default=os.getenv("NOTIFICATION_SENDER_EMAIL", account_email))
        pwd = Prompt.ask("Sender Email Password / App Password", password=True)
        recipient = Prompt.ask("Recipient Email for Reports", default=os.getenv("NOTIFICATION_RECIPIENT_EMAIL", sender))
        smtp_srv = Prompt.ask("SMTP Server Host", default=os.getenv("SMTP_SERVER", "smtp.gmail.com"))
        smtp_p = Prompt.ask("SMTP Server Port", default=os.getenv("SMTP_PORT", "587"))

        if sender: _write_env_var("NOTIFICATION_SENDER_EMAIL", sender)
        if pwd: _write_env_var("NOTIFICATION_SENDER_PASSWORD", pwd)
        if recipient: _write_env_var("NOTIFICATION_RECIPIENT_EMAIL", recipient)
        if smtp_srv: _write_env_var("SMTP_SERVER", smtp_srv)
        if smtp_p: _write_env_var("SMTP_PORT", smtp_p)

    if notif_choice in ("4", "5"):
        console.print("\n[bold cyan]--- WhatsApp (Twilio) Configuration ---[/bold cyan]")
        sid = Prompt.ask("Twilio Account SID", default=os.getenv("TWILIO_ACCOUNT_SID", ""))
        token = Prompt.ask("Twilio Auth Token", password=True)
        from_num = Prompt.ask("Twilio WhatsApp From (e.g. whatsapp:+14155238886)", default=os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"))
        to_num = Prompt.ask("Your WhatsApp To (e.g. whatsapp:+1234567890)", default=os.getenv("TWILIO_WHATSAPP_TO", ""))

        if sid: _write_env_var("TWILIO_ACCOUNT_SID", sid)
        if token: _write_env_var("TWILIO_AUTH_TOKEN", token)
        if from_num: _write_env_var("TWILIO_WHATSAPP_FROM", from_num)
        if to_num: _write_env_var("TWILIO_WHATSAPP_TO", to_num)

    with open(config_path, "w", encoding="utf-8") as f:
        import yaml
        yaml.safe_dump(config.model_dump(mode="json"), f, default_flow_style=False)

    console.print("\n[bold green][OK] Configuration updated in config.yaml and .env![/bold green]\n")
    dashboard()

@app.command()
def test_report(
    channel: Optional[str] = typer.Option(None, "--channel", "-ch", help="Override notification channel (terminal|discord|email|whatsapp|all)")
):
    """
    Test sending a sample workflow notification & escalation report.
    """
    channel = _unwrap(channel, None)
    print_banner("TEST NOTIFICATION & REPORT SYSTEM")
    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()

    if channel:
        config.notifications.channel = channel

    dispatcher = NotificationDispatcher(config.notifications)

    dummy_msg = EmailMessage(
        message_id="msg_test_report",
        thread_id="thread_test_report",
        sender=SenderInfo(name="Billing Fraud Dept", email="alert@fake-finance.com"),
        subject="[URGENT REPORT TEST] Disputed Wire Transfer #9842",
        body="This is a test notification report generated by email-workflow.",
        received_at="2026-09-18T16:00:00Z",
    )

    dummy_analysis = EmailAnalysis(
        message_id="msg_test_report",
        thread_id="thread_test_report",
        sender=dummy_msg.sender,
        subject=dummy_msg.subject,
        received_at=dummy_msg.received_at,
        category=EmailCategory.FINANCIAL,
        importance=ImportanceLevel.HIGH,
        urgency=ImportanceLevel.HIGH,
        action_required=True,
        response_required=True,
        safe_to_automate=False,
        confidence=0.95,
        missing_information=["Approved wire authorization code"],
        commitments_implied=["Financial transfer confirmation"],
        recommended_decision=DecisionOption.ESCALATE,
        reasoning="Test financial transaction dispute requiring manual review.",
    )

    results = dispatcher.notify(
        message=dummy_msg,
        analysis=dummy_analysis,
        decision=DecisionOption.ESCALATE,
        draft_id=None,
        reply_text="[Sample AI Draft Reply]\nDear Billing Dept,\n\nWe have received your alert regarding transfer #9842. This transaction has been placed on hold pending executive authorization.\n\nBest regards,\nUser",
    )

    console.print("\n[bold cyan]Report Delivery Results:[/bold cyan]")
    for ch, status in results.items():
        st_text = "[bold green]Delivered[/bold green]" if status else "[bold red]Failed / Not configured[/bold red]"
        console.print(f"  - Channel '{ch}': {st_text}")

@app.command()
def demo():
    """
    Run offline demo against 4 built-in fixtures with zero API keys and zero network calls.
    """
    print_banner("AUTONOMOUS EMAIL WORKFLOW - DEMO MODE (OFFLINE / FAKE AI)")

    fixture_path = resolve_project_file("fixtures/demo_fixtures.json")
    if not fixture_path.exists():
        console.print(f"[bold red]Demo fixtures file not found at {fixture_path}[/bold red]")
        raise typer.Exit(code=1)

    config = AppConfig()
    config.ai.mode = AIMode.API
    config.ai.api.provider = "fake"
    config.state.store = "demo_state.json"

    demo_state = resolve_project_file("demo_state.json")
    demo_audit = resolve_project_file("demo_audit.jsonl")
    demo_idemp = resolve_project_file("demo_idempotency.json")

    if demo_state.exists(): demo_state.unlink()
    if demo_audit.exists(): demo_audit.unlink()
    if demo_idemp.exists(): demo_idemp.unlink()

    ai_provider = FakeAIProvider()
    email_provider = MockEmailProvider(inbox_path=str(fixture_path))

    pipeline = WorkflowPipeline(
        config=config,
        ai_provider=ai_provider,
        email_provider=email_provider,
        store_path=str(demo_state),
        audit_path=str(demo_audit),
        idempotency_path=str(demo_idemp),
    )

    emails = email_provider.fetch_unprocessed_emails()
    console.print(f"[bold cyan]Loaded {len(emails)} demo fixture emails from {fixture_path}[/bold cyan]\n")

    results = []
    for idx, email in enumerate(emails, 1):
        res = pipeline.process_email(email)
        results.append(res)
        render_stage_result(idx, res)

    pipeline.notifier.send_run_digest(
        results,
        provider_name="FakeAI (Offline Demo)",
        model_name="demo-deterministic",
    )

    console.print("\n[bold green][OK] Demo execution completed clean![/bold green]\n")

    audit = AuditLogger(log_path=str(demo_audit))
    render_audit_log(audit.get_all_events())

def _active_model_label(config) -> str:
    """How the user should see the engine currently in use."""
    if config.ai.mode == AIMode.API:
        return f"{config.ai.api.provider} / {config.ai.api.model}"
    return f"{config.ai.local.runtime} / {config.ai.local.model}"


def _print_workflow_error(e: WorkflowError) -> None:
    """Show an expected failure as advice, never as a traceback."""
    body = f"[bold red]{e.message}[/bold red]"
    if e.hint:
        body += "\n\n[bold yellow]What to do:[/bold yellow]\n" + e.hint
    console.print(Panel(body, title="Could not finish", border_style="red"))


def _announce_throttle(seconds: float, requests_per_minute: int) -> None:
    """Explain the pause while it happens, so it does not look like a freeze."""
    wait = int(seconds) + 1 if seconds % 1 else int(seconds)
    if wait >= 60:
        how_long = f"about a minute"
    elif wait >= 10:
        how_long = f"{wait} seconds"
    else:
        how_long = f"{wait} second" + ("s" if wait != 1 else "")

    console.print(
        f"[yellow]Hit the free limit of {requests_per_minute} requests per minute. "
        f"Waiting {how_long}, then carrying on by itself - nothing is broken, "
        f"you do not have to do anything.[/yellow]"
    )


def _announce_switch(from_link, to_link, error) -> None:
    """Tell the user, mid-run, that the chain moved to another provider."""
    console.print(
        f"[yellow]{from_link} stopped working ({error.kind}). "
        f"Switching to {to_link} and carrying on.[/yellow]"
    )


MAX_PARALLEL_WORKERS = 8


def _short(subject: str, width: int = 55) -> str:
    subject = (subject or "(no subject)").strip()
    return subject if len(subject) <= width else subject[: width - 3] + "..."


def _worker_count(config, ai_provider, email_count: int) -> int:
    """How many emails to work on at once: one per API key, never more.

    More workers than keys would only queue behind the same per-minute limit,
    and each worker also opens its own mailbox connection, so this is capped
    whatever the config asks for.
    """
    keys_cfg = getattr(config.ai, "keys", None)
    if keys_cfg is None or not keys_cfg.parallel:
        return 1
    lanes = parallel_lanes(ai_provider)
    wanted = keys_cfg.max_workers or lanes
    return max(1, min(wanted, lanes, email_count, MAX_PARALLEL_WORKERS))


def _process_one_at_a_time(pipeline, emails, provider_name, model_name) -> list:
    results = []
    total = len(emails)
    for idx, email in enumerate(emails, 1):
        # One email can be several AI calls and a rate-limit wait. Without a
        # line on screen the whole time the app just looks frozen - and it is
        # not obvious when it has moved on to the next email either.
        with console.status(
            f"[bold cyan]({idx}/{total}) thinking about[/bold cyan] "
            f'"{_short(email.subject)}" [dim]- {provider_name} / {model_name}[/dim]',
            spinner="dots",
        ):
            res = pipeline.process_email(email)
        results.append(res)
        render_stage_result(idx, res)
    return results


def _process_together(pipeline, emails, workers: int) -> list:
    """Several emails at once, one API key each.

    Each result is shown the moment it lands rather than in inbox order. With
    several emails in flight the order they finish in is what actually
    happened, and holding everything back until the slowest one returns would
    throw away the point of running them together.
    """
    total = len(emails)
    results = [None] * total
    finished = 0

    def label() -> str:
        return (
            f"[bold cyan]{finished}/{total} done[/bold cyan] [dim]- "
            f"{min(workers, total - finished)} being worked on, "
            f"{workers} keys[/dim]"
        )

    with console.status(label(), spinner="dots") as status:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {
                pool.submit(pipeline.process_email, email): i
                for i, email in enumerate(emails)
            }
            for job in as_completed(jobs):
                index = jobs[job]
                results[index] = job.result()
                finished += 1
                status.update(label())
                render_stage_result(index + 1, results[index])

    return results


def _process_inbox(pipeline, email_provider, config) -> list:
    """One full pass over the inbox: fetch, process each email, send the digest."""
    # Say out loud what is and is not being looked at. "It only found 2" is
    # confusing until you know it never reads mail you have already opened.
    days = getattr(config.email, "max_age_days", 7)
    scope = f"unread mail from the last {days} day" + ("s" if days != 1 else "")
    if config.email.provider == "mock":
        scope = "the test inbox"

    emails = email_provider.fetch_unprocessed_emails()
    if not emails:
        console.print(
            f"[dim]Nothing to do - no {scope}.\n"
            f"Anything you have already opened is left alone on purpose.[/dim]"
        )
        return []

    provider_name, model_name = pipeline.ai.describe()
    console.print(
        f"[bold cyan]Found {len(emails)} in {scope}.[/bold cyan] "
        f"[dim](already-read mail is never touched)[/dim]\n"
        f"[bold cyan]Processing with {provider_name} / {model_name}...[/bold cyan]\n"
    )

    # Only this run's problems belong in this run's report. The provider
    # object is reused for every email and for every --loop pass, so an
    # error left on it from an earlier pass would otherwise be shown for
    # ever, long after the mailbox started behaving.
    if hasattr(email_provider, "last_archive_error"):
        email_provider.last_archive_error = ""

    workers = _worker_count(config, pipeline.ai, len(emails))
    if workers > 1:
        console.print(
            f"[bold cyan]{workers} API keys - {workers} emails at a time.[/bold cyan]\n"
        )
        results = _process_together(pipeline, emails, workers)
    else:
        results = _process_one_at_a_time(
            pipeline, emails, provider_name, model_name
        )

    # Archiving and starring are deliberately non-fatal, but they were also
    # silent - so a mailbox that refused every change looked like success.
    mailbox_problem = getattr(email_provider, "last_archive_error", "")
    if mailbox_problem:
        console.print(
            Panel(
                f"[bold yellow]The mailbox did not accept every change.[/bold yellow]\n\n"
                f"{mailbox_problem}\n\n"
                f"[dim]Nothing was deleted. Any email that could not be filed "
                f"is left unread for the next run.[/dim]",
                border_style="yellow",
            )
        )

    # Read it again: a failover during the run may have moved us to another
    # provider, and the digest must name the one that did the work.
    provider_name, model_name = pipeline.ai.describe()
    pipeline.notifier.send_run_digest(
        results, provider_name=provider_name, model_name=model_name
    )
    return results


@app.command()
def run(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    mock_inbox: Optional[str] = typer.Option(None, "--mock-inbox", "-m", help="Path to JSON file of mock emails"),
    loop: bool = typer.Option(False, "--loop", "-l", help="Run continuous polling daemon loop"),
    interval: int = typer.Option(60, "--interval", "-i", help="Polling interval in seconds"),
    schedule: Optional[str] = typer.Option(None, "--schedule", "-s", help="Schedule daily execution time in HH:MM format (e.g. 18:00)"),
):
    """
    Run pipeline using configured AI provider (OpenAI, Gemini, Groq, OpenRouter, or Ollama).
    If API key is missing, prompts interactively to enter key!
    """
    config_file = _unwrap(config_file, "config.yaml")
    mock_inbox = _unwrap(mock_inbox, None)
    loop = _unwrap(loop, False)
    interval = _unwrap(interval, 60)
    schedule = _unwrap(schedule, None)

    print_banner("AUTONOMOUS EMAIL WORKFLOW - RUN MODE")

    config_path = resolve_project_file(config_file)
    if not config_path.exists():
        console.print(f"[bold red]Config file not found: {config_path}[/bold red]")
        raise typer.Exit(code=1)

    config = AppConfig.load_from_file(config_path)

    if config.ai.mode == AIMode.API and config.ai.api.provider != "fake":
        env_var = config.ai.api.api_key_env
        key = os.getenv(env_var)
        if not key or not key.strip():
            console.print(f"[bold yellow]Notice:[/bold yellow] API key variable '[bold cyan]{env_var}[/bold cyan]' for provider '[bold cyan]{config.ai.api.provider}[/bold cyan]' is not set.")
            if Confirm.ask("Would you like to enter your API key now?", default=True):
                user_key = _prompt_api_key(env_var, config.ai.api.provider.upper())
                if user_key:
                    _write_env_var(env_var, user_key)
                    console.print(f"[bold green]Key saved to .env![/bold green]")
                else:
                    console.print("[bold red]No key entered. Cannot proceed.[/bold red]")
                    raise typer.Exit(code=1)

    try:
        ai_provider = get_ai_provider(
            config, on_switch=_announce_switch, on_throttle=_announce_throttle
        )
    except ValueError as e:
        console.print(Panel(f"[bold red]{e}[/bold red]", title="Cannot start", border_style="red"))
        raise typer.Exit(code=1)

    if isinstance(ai_provider, FallbackAIProvider):
        console.print(
            f"[dim]Provider chain: {ai_provider.describe_chain()}[/dim]\n"
        )

    # Several keys means several accounts, and that is worth saying out loud:
    # it is the difference between one free tier and three.
    pool = find_pool(ai_provider)
    if pool is not None:
        console.print(f"[dim]Keys in use ({pool.size}):[/dim]")
        for lane in pool.lanes:
            console.print(f"[dim]  - {lane}[/dim]")
        console.print()

    inbox_file = mock_inbox or "fixtures/demo_fixtures.json"
    resolved_inbox = resolve_project_file(inbox_file)

    if config.email.provider == "mock" and not resolved_inbox.exists():
        console.print(f"[bold red]Mock inbox file not found: {resolved_inbox}[/bold red]")
        raise typer.Exit(code=1)

    email_provider = get_email_provider(config.email, mock_inbox_path=str(resolved_inbox))

    pipeline = WorkflowPipeline(
        config=config,
        ai_provider=ai_provider,
        email_provider=email_provider,
        store_path=str(resolve_project_file(config.state.store)),
        audit_path=str(resolve_project_file("audit.jsonl")),
        idempotency_path=str(resolve_project_file("idempotency.json")),
    )

    try:
        if schedule:
            while True:
                try:
                    secs, target_str = get_seconds_until_scheduled_time(schedule)
                except ValueError as ve:
                    console.print(f"[bold red]Schedule error:[/bold red] {ve}")
                    raise typer.Exit(code=1)

                hrs = secs // 3600
                mins = (secs % 3600) // 60
                console.print(
                    f"[bold yellow]Daily schedule active: next run at {target_str} "
                    f"(in {hrs}h {mins}m). Press Ctrl+C to stop.[/bold yellow]"
                )
                try:
                    time.sleep(secs)
                except KeyboardInterrupt:
                    console.print("\n[yellow]Daily schedule stopped.[/yellow]")
                    return

                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                console.print(f"\n[bold cyan]=== Scheduled run at {stamp} ===[/bold cyan]\n")
                _process_inbox(pipeline, email_provider, config)

        elif loop:
            console.print(
                f"[bold yellow]Polling the inbox every {interval}s. "
                f"Press Ctrl+C to stop.[/bold yellow]\n"
            )
            try:
                iteration = 1
                while True:
                    console.print(f"[dim]--- Polling cycle #{iteration} ---[/dim]")
                    _process_inbox(pipeline, email_provider, config)
                    iteration += 1
                    time.sleep(interval)
            except KeyboardInterrupt:
                console.print("\n[yellow]Polling stopped.[/yellow]")
                return

        else:
            _process_inbox(pipeline, email_provider, config)
            console.print("\n[bold green][OK] Processing complete.[/bold green]")

    except WorkflowError as e:
        _print_workflow_error(e)
        raise typer.Exit(code=1)

@app.command()
def providers():
    """
    List supported AI providers and check whether their environment variables are set.
    """
    print_banner("AI PROVIDERS STATUS")

    status = {}
    for prov_key, meta in PROVIDER_METADATA.items():
        env_var = meta["env_var"]
        val = os.getenv(env_var)
        is_set = "set" if (val and val.strip()) else "missing"
        status[prov_key] = {
            "name": meta["name"],
            "env_var": env_var,
            "status": is_set,
        }

    render_providers_status(status)

@app.command()
def models(
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider to query (default: the one in config.yaml)"
    ),
):
    """
    List the models your API key can actually use.

    Run this when a model stops working. Listing models costs no generation
    quota, so it is safe on a free tier.
    """
    provider = _unwrap(provider, None)
    print_banner("AVAILABLE MODELS")

    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()

    prov_id = (provider or config.ai.api.provider).lower()
    if prov_id not in PROVIDER_METADATA:
        console.print(
            f"[bold red]Unknown provider '{prov_id}'.[/bold red] "
            f"Supported: {', '.join(PROVIDER_METADATA)}"
        )
        raise typer.Exit(code=1)

    api_cfg = config.ai.api
    if prov_id != config.ai.api.provider.lower():
        meta = PROVIDER_METADATA[prov_id]
        api_cfg = api_cfg.model_copy(
            update={"provider": prov_id, "api_key_env": meta["env_var"]}
        )

    current = config.ai.api.model if prov_id == config.ai.api.provider.lower() else None

    try:
        available = OpenAICompatibleProvider(prov_id, api_cfg).list_models()
    except WorkflowError as e:
        _print_workflow_error(e)
        raise typer.Exit(code=1)
    except ValueError as e:
        console.print(Panel(f"[bold red]{e}[/bold red]", title="Cannot list models", border_style="red"))
        raise typer.Exit(code=1)

    if not available:
        console.print("[yellow]The provider returned no models for this key.[/yellow]")
        return

    table = Table(
        title=f"{PROVIDER_METADATA[prov_id]['name']} - {len(available)} model(s) available",
        border_style="cyan",
    )
    table.add_column("Model id", style="white")
    table.add_column("", style="bold green")
    for model_id in available:
        table.add_row(model_id, "<- in your config" if model_id == current else "")
    console.print(table)

    if current and current not in available:
        console.print(
            Panel(
                f"[bold red]Your configured model '{current}' is NOT in this list.[/bold red]\n"
                f"Pick one above and set it in config.yaml under [cyan]ai.api.model[/cyan].",
                border_style="red",
            )
        )
    else:
        console.print(
            "\n[dim]To switch, edit config.yaml -> ai.api.model, or run "
            "'email-workflow setup'.[/dim]"
        )


@app.command()
def testmail(
    to: Optional[str] = typer.Option(None, "--to", help="Where to send (default: your own address)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
):
    """
    Send yourself a few test emails, so there is something to process.

    Every one is clearly marked [TEST] in the subject and says in the body that
    it was generated by this app. They go to your own address.
    """
    to = _unwrap(to, None)
    yes = _unwrap(yes, False)

    print_banner("SEND TEST EMAILS")
    config = _load_config_quietly()

    address = os.getenv("GMAIL_ADDRESS", config.email.account_ref)
    password = os.getenv("GMAIL_APP_PASSWORD", os.getenv("NOTIFICATION_SENDER_PASSWORD", ""))
    recipient = to or address

    if not address or not password:
        console.print(
            Panel(
                "[bold red]No mailbox credentials.[/bold red]\n\n"
                "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file, "
                "or run 'email-workflow setup'.",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    # One per decision path, so a single run exercises the whole pipeline.
    samples = [
        (
            "[TEST] Quick question about your working hours",
            "Hi,\n\nWhat are your working hours this week, and could we meet on "
            "Thursday morning?\n\nThanks,\nAnna\n\n"
            "-- This is a test message generated by email-workflow. --",
            "should be answerable from your Known Facts",
        ),
        (
            "[TEST] Our weekly newsletter is here",
            "This week: five tips, two links and a photo of a dog.\n\n"
            "Unsubscribe at any time.\n\n"
            "-- This is a test message generated by email-workflow. --",
            "routine -> should be archived",
        ),
        (
            "[TEST] Receipt for your subscription",
            "Thank you for your payment of 21,99 PLN.\n"
            "Invoice number: TEST-0001. Card ending 0000.\n\n"
            "-- This is a test message generated by email-workflow. --",
            "financial -> should be escalated and starred",
        ),
        (
            "[TEST] Can you confirm the budget for next quarter?",
            "Hi,\n\nCould you confirm the exact budget figure for Q1 and when we "
            "can start? I need the number today.\n\nBest,\nMarek\n\n"
            "-- This is a test message generated by email-workflow. --",
            "needs a fact you never gave it -> should NOT be answered blindly",
        ),
    ]

    table = Table(title=f"Will be sent to {recipient}", border_style="cyan")
    table.add_column("Subject", style="white")
    table.add_column("What it is testing", style="dim")
    for subject, _, purpose in samples:
        table.add_row(subject, purpose)
    console.print(table)

    console.print(
        f"\n[dim]They are sent from {address}, so they arrive as ordinary unread "
        f"mail. Delete them afterwards like any other email.[/dim]"
    )

    if not yes and not Confirm.ask(f"\nSend {len(samples)} test emails now?", default=False):
        console.print("[dim]Nothing was sent.[/dim]")
        return

    sent = 0
    try:
        smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(address, password)
            for subject, body, _ in samples:
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = subject
                msg["From"] = address
                msg["To"] = recipient
                server.send_message(msg)
                sent += 1
                console.print(f"[green]sent:[/green] {subject}")
    except Exception as e:
        console.print(
            Panel(
                f"[bold red]Sending stopped after {sent} of {len(samples)}.[/bold red]\n\n{e}\n\n"
                f"[bold yellow]What to do:[/bold yellow]\nGmail needs an App Password, "
                f"not your normal password. Check GMAIL_APP_PASSWORD in your .env file.",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    console.print(
        f"\n[bold green][OK] Sent {sent} test emails to {recipient}.[/bold green]\n"
        f"[dim]Give Gmail a few seconds, then run the pipeline (menu option 1) "
        f"to watch them being handled.[/dim]"
    )


@app.command()
def settings():
    """
    Turn sending, drafts, archiving and starring on or off.
    """
    print_banner("SENDING & MAILBOX SETTINGS")

    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()
    mail = config.email

    def show(value: bool) -> str:
        return "[bold green]ON[/bold green]" if value else "[dim]off[/dim]"

    table = Table(title="What the app is allowed to do", border_style="cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Now", justify="center")
    table.add_column("What it means", style="dim")
    table.add_row("Send replies", show(mail.allow_send),
                  "actually send email, with nobody reading it first")
    table.add_row("Keep drafts", show(mail.create_drafts),
                  "save a draft when a reply is not sent")
    table.add_row("Archive unimportant", show(mail.archive_unimportant),
                  "routine mail leaves the inbox")
    table.add_row("Star important", show(mail.star_important),
                  f"star and label '{mail.important_label}'")
    console.print(table)

    if not Confirm.ask("\nChange these?", default=False):
        return

    # --- sending -----------------------------------------------------------
    if not mail.allow_send:
        console.print(
            Panel(
                "[bold yellow]Read this before turning sending on.[/bold yellow]\n\n"
                "The app will send replies to real people [bold]without showing them\n"
                "to you first[/bold]. A sent email cannot be taken back.\n\n"
                "It only sends when every check passes: the sender asked something\n"
                "your Known Facts already answer, the reply invents nothing, and the\n"
                "AI was confident. Anything touching passwords, payments or bank\n"
                "details is never sent, whatever this setting says.\n\n"
                "[dim]Your Known Facts are what it is allowed to say. Check them with\n"
                "'email-workflow facts' before switching this on.[/dim]",
                border_style="yellow",
            )
        )
    mail.allow_send = Confirm.ask("Send replies automatically?", default=mail.allow_send)

    # --- drafts ------------------------------------------------------------
    mail.create_drafts = Confirm.ask(
        "Keep drafts for replies that are not sent?", default=mail.create_drafts
    )
    if not mail.create_drafts:
        console.print(
            "[dim]Drafts off: a reply that cannot be sent safely is escalated to you\n"
            "instead, with the text in the report. Nothing is lost, nothing is left\n"
            "sitting in your Drafts folder.[/dim]"
        )

    if mail.allow_send and not mail.create_drafts:
        console.print(
            "\n[bold]So: replies that pass every check are sent, and everything\n"
            "else comes to you. No drafts at all.[/bold]"
        )
    elif not mail.allow_send and not mail.create_drafts:
        console.print(
            "\n[yellow]Note: with both off, the app never writes to your mailbox -\n"
            "it only tells you what it would have done.[/yellow]"
        )

    # --- mailbox tidying ---------------------------------------------------
    mail.archive_unimportant = Confirm.ask(
        "Archive unimportant mail (take it out of the inbox)?",
        default=mail.archive_unimportant,
    )
    mail.star_important = Confirm.ask(
        "Star important mail?", default=mail.star_important
    )
    if mail.star_important:
        mail.important_label = Prompt.ask(
            "Label to put on it", default=mail.important_label
        )

    import yaml

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True)

    console.print(f"\n[bold green][OK] Saved to {config_path.name}.[/bold green]")
    if mail.allow_send:
        console.print(
            "[bold yellow]Sending is ON. The next run can send real email.[/bold yellow]"
        )


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
):
    """
    Erase your personal data from this folder, so it can be shared safely.

    Removes the login, your knowledge base, run history and usage records.
    config.yaml and .env are left alone - check those yourself before sharing,
    as they hold your address and your API keys.
    """
    yes = _unwrap(yes, False)
    print_banner("RESET PERSONAL DATA")

    targets = [
        ("auth.json", "your login"),
        ("known_facts.txt", "your personal knowledge base"),
        ("state.json", "thread state from previous runs"),
        ("audit.jsonl", "the audit log"),
        ("idempotency.json", "which emails were already handled"),
        ("usage.jsonl", "recorded API usage"),
    ]

    present = [(name, what) for name, what in targets if resolve_project_file(name).exists()]
    if not present:
        console.print("[green]Nothing personal to remove - this folder is already clean.[/green]")
        return

    table = Table(title="Will be deleted", border_style="red")
    table.add_column("File", style="white")
    table.add_column("What it is", style="dim")
    for name, what in present:
        table.add_row(name, what)
    console.print(table)

    console.print(
        "\n[yellow]Check these yourself before sharing - they are NOT touched:[/yellow]\n"
        "  [cyan]config.yaml[/cyan]  your email address and settings\n"
        "  [cyan].env[/cyan]         your API keys and app password\n"
    )

    if not yes and not Confirm.ask("Delete the files listed above?", default=False):
        console.print("[dim]Nothing was deleted.[/dim]")
        return

    for name, _ in present:
        try:
            resolve_project_file(name).unlink()
            console.print(f"[green]Removed {name}[/green]")
        except OSError as e:
            console.print(f"[red]Could not remove {name}: {e}[/red]")

    console.print("\n[bold green][OK] This folder no longer holds your personal data.[/bold green]")


@app.command()
def usage(
    days: int = typer.Option(7, "--days", "-d", help="How many days of history to show"),
    reset: bool = typer.Option(False, "--reset", help="Erase the recorded usage history"),
):
    """
    Show how much of your API keys you have used.

    Counted locally, from every call this app makes. Providers do not offer this:
    Gemini sends no rate-limit headers and has no usage endpoint for an API key,
    so nobody can ask it how much of a free tier is left. Where a provider does
    report limits (Groq, OpenAI), those numbers are shown too.
    """
    days = _unwrap(days, 7)
    reset = _unwrap(reset, False)

    print_banner("API KEY USAGE")
    config = _load_config_quietly()
    tracker = UsageTracker(store_path=config.ai.usage.store)

    if reset:
        if Confirm.ask("Erase all recorded usage history?", default=False):
            tracker.reset()
            console.print("[bold yellow]Usage history erased.[/bold yellow]")
        return

    today = tracker.totals_today()
    if not today:
        console.print(
            Panel(
                "Nothing recorded today.\n\n"
                "[dim]Usage is counted from the moment this feature was added. "
                "Run the pipeline and come back.[/dim]",
                border_style="cyan",
            )
        )
    else:
        table = Table(title="Today", border_style="cyan", show_header=True)
        table.add_column("Provider / model", style="white")
        table.add_column("Calls", justify="right", style="bold")
        table.add_column("Failed", justify="right")
        table.add_column("Quota hits", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Of your limit", style="bold")

        warn_at = config.ai.usage.warn_at_percent
        for name, row in sorted(today.items()):
            provider = name.split(" / ")[0]
            limit = config.ai.usage.limits.get(provider)

            share = "[dim]not set[/dim]"
            if limit and limit.requests_per_day > 0:
                pct = row["calls"] * 100 // limit.requests_per_day
                colour = "red" if pct >= 100 else ("yellow" if pct >= warn_at else "green")
                share = f"[{colour}]{pct}% of {limit.requests_per_day} calls[/{colour}]"
            elif limit and limit.tokens_per_day > 0:
                pct = row["total_tokens"] * 100 // limit.tokens_per_day
                colour = "red" if pct >= 100 else ("yellow" if pct >= warn_at else "green")
                share = f"[{colour}]{pct}% of {limit.tokens_per_day} tokens[/{colour}]"

            table.add_row(
                name,
                str(row["calls"]),
                str(row["failed"]) if row["failed"] else "-",
                f"[red]{row['quota_hits']}[/red]" if row["quota_hits"] else "-",
                f"{row['total_tokens']:,}",
                share,
            )
        console.print(table)

        # Anything a provider told us directly beats our own counting.
        for name, row in sorted(today.items()):
            if row.get("reported"):
                reported = "  ".join(f"{k.replace('x-ratelimit-', '')}={v}"
                                     for k, v in row["reported"].items())
                console.print(f"[dim]{name} reported: {reported}[/dim]")

    history = tracker.daily_counts(days=max(1, days))
    if any(calls for _, calls, _ in history):
        past = Table(title=f"Last {len(history)} days", border_style="cyan")
        past.add_column("Date", style="white")
        past.add_column("Calls", justify="right")
        past.add_column("Tokens", justify="right")
        past.add_column("", style="cyan")
        busiest = max(calls for _, calls, _ in history) or 1
        for day, calls, tokens in history:
            bar = "#" * int(calls * 20 / busiest) if calls else ""
            past.add_row(day, str(calls), f"{tokens:,}", bar)
        console.print(past)

    if not config.ai.usage.limits:
        console.print(
            Panel(
                "To see a percentage, tell the app your daily allowance. Providers\n"
                "do not expose it, so copy it from your provider's dashboard into\n"
                "[cyan]config.yaml[/cyan]:\n\n"
                "[dim]ai:\n"
                "  usage:\n"
                "    limits:\n"
                "      gemini:\n"
                "        requests_per_day: 250[/dim]",
                title="Want a percentage?",
                border_style="yellow",
            )
        )


@app.command()
def log(
    thread_id: Optional[str] = typer.Option(None, "--thread-id", "-t", help="Filter logs by thread ID"),
    audit_file: str = typer.Option("audit.jsonl", "--file", "-f", help="Audit log file path"),
):
    """
    Pretty-print audit log entries.
    """
    thread_id = _unwrap(thread_id, None)
    audit_file = _unwrap(audit_file, "audit.jsonl")

    log_path = resolve_project_file(audit_file)
    if not log_path.exists():
        demo_log = resolve_project_file("demo_audit.jsonl")
        if demo_log.exists():
            log_path = demo_log

    if not log_path.exists():
        console.print("[yellow]No audit log file found.[/yellow]")
        return

    audit = AuditLogger(log_path=str(log_path))
    events = audit.get_events_for_thread(thread_id) if thread_id else audit.get_all_events()
    render_audit_log(events, thread_id_filter=thread_id)

@app.command()
def replay(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="Thread ID to replay"),
    state_file: str = typer.Option("state.json", "--state-file", help="Thread state store file"),
    audit_file: str = typer.Option("audit.jsonl", "--audit-file", help="Audit log file path"),
):
    """
    Show full message history and state transitions for a specific thread.
    """
    thread_id = _unwrap(thread_id, "thread_meet_02")
    state_file = _unwrap(state_file, "state.json")
    audit_file = _unwrap(audit_file, "audit.jsonl")

    state_path = resolve_project_file(state_file)
    if not state_path.exists():
        demo_st = resolve_project_file("demo_state.json")
        if demo_st.exists():
            state_path = demo_st

    audit_path = resolve_project_file(audit_file)
    if not audit_path.exists():
        demo_au = resolve_project_file("demo_audit.jsonl")
        if demo_au.exists():
            audit_path = demo_au

    thread_mgr = ThreadManager(store_path=str(state_path))
    thread_state = thread_mgr.get_thread(thread_id)

    if not thread_state:
        console.print(f"[bold red]Thread '{thread_id}' not found in state store.[/bold red]")
        raise typer.Exit(code=1)

    audit = AuditLogger(log_path=str(audit_path))
    events = audit.get_events_for_thread(thread_id)

    render_thread_replay(thread_id, thread_state.model_dump(), events)

@app.command()
def facts():
    """View or edit personal Knowledge Base & Known Facts."""
    print_banner("PERSONAL KNOWLEDGE BASE & KNOWN FACTS")
    mgr = KnownFactsManager()
    current_facts = mgr.load_facts()

    console.print(Panel(current_facts, title="Current Known Authorized Facts", border_style="cyan"))

    if Confirm.ask("Would you like to edit your Known Facts?", default=False):
        console.print("[yellow]Type your new Known Facts below. Press Enter twice when finished:[/yellow]")
        lines = []
        while True:
            line = input()
            if not line and lines and not lines[-1]:
                break
            lines.append(line)
        new_text = "\n".join(lines).strip()
        if new_text:
            mgr.save_facts(new_text)
            console.print("[bold green][OK] Updated Known Facts saved successfully![/bold green]")

if __name__ == "__main__":
    app()


def _shell(argv, cwd=None, stdin_text=None, interactive=False):
    """Run one external command. Returns (ok, output).

    Never shell=True: every argument is passed as a list, so a folder name with
    a space or a quote in it cannot turn into part of the command.
    """
    import subprocess
    try:
        if interactive:
            # gh's login is a conversation with the user - it needs the real
            # terminal, so nothing is captured here.
            done = subprocess.run(argv, cwd=cwd)
            return done.returncode == 0, ""
        done = subprocess.run(
            argv, cwd=cwd, input=stdin_text, capture_output=True,
            text=True, timeout=180,
        )
        return done.returncode == 0, (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError:
        return False, f"'{argv[0]}' is not installed."
    except Exception as e:
        return False, str(e)


def _schedule_on_this_computer(hour, minute, project_root):
    """Register a daily task with whatever this operating system uses."""
    plan = local_schedule_plan(hour, minute, "email-workflow run", project_root)

    console.print(Panel(
        f"[bold]{plan['explain']}[/bold]\n\n"
        f"[dim]Command: {' '.join(plan['argv'])}[/dim]",
        title=f"Scheduling on this computer ({current_system()})",
        border_style="cyan",
    ))
    console.print(
        "[yellow]Remember: this only fires while the computer is awake. "
        "A laptop that is shut at that hour simply misses the run.[/yellow]\n"
    )
    if not Confirm.ask("Set it up now?", default=True):
        console.print("[dim]Nothing changed.[/dim]")
        return

    for path, contents in plan.get("files", []):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        console.print(f"[dim]wrote {path}[/dim]")

    stdin_text = None
    if plan["kind"] == "cron":
        # Never overwrite the crontab wholesale - other jobs live in it too.
        stdin_text = merge_crontab(existing_crontab(), plan["stdin_line"])

    ok, out = _shell(plan["argv"], stdin_text=stdin_text)
    if ok:
        console.print(Panel(
            f"[bold green]Done.[/bold green] It will run every day at "
            f"{hour:02d}:{minute:02d}.",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[bold red]That did not work.[/bold red]\n\n{out.strip()[:600]}",
            border_style="red",
        ))


def _schedule_on_github(hour, minute, project_root):
    """Put it on GitHub Actions: free, and it runs with your computer off."""
    for tool in ("git", "gh"):
        if not tool_available(tool):
            console.print(Panel(
                f"[bold red]'{tool}' is not installed.[/bold red]\n\n"
                f"Install it with:\n\n    {install_hint(tool)}\n\n"
                f"then run [bold]email-workflow schedule[/bold] again.",
                title="One thing missing",
                border_style="red",
            ))
            raise typer.Exit(code=1)

    # 1. Who are you on GitHub?
    #
    # This folder can be copied to another person or another machine, and gh
    # may already be signed in as whoever set that machine up. Publishing to
    # the wrong account, with somebody else's keys, is not something to
    # discover afterwards - so the account is named and confirmed out loud.
    signed_in, _ = _shell(["gh", "auth", "status"])
    if not signed_in:
        console.print(Panel(
            "You are not signed in to GitHub yet.\n\n"
            "The next step opens GitHub's own login. Choose [bold]HTTPS[/bold] "
            "and let it authenticate in the browser - it is GitHub asking, not "
            "this app, and no password is ever seen here.\n\n"
            "Sign in as [bold]yourself[/bold]: the repository, the schedule and "
            "the keys all end up on whichever account you use here.",
            title="Sign in to GitHub",
            border_style="cyan",
        ))
        if not Confirm.ask("Sign in now?", default=True):
            console.print("[dim]Stopped. Nothing was changed.[/dim]")
            return
        ok, _ = _shell(["gh", "auth", "login"], interactive=True)
        if not ok:
            console.print("[bold red]Sign-in did not complete. Nothing was changed.[/bold red]")
            return

    def github_account():
        ok, who = _shell(["gh", "api", "user", "-q", ".login"], cwd=project_root)
        return who.strip().splitlines()[0].strip() if ok and who.strip() else ""

    account = github_account()
    if account:
        console.print(
            f"\nGitHub is signed in as [bold]{account}[/bold]. "
            f"Everything below goes to that account.\n"
        )
        if not Confirm.ask(f"Is '{account}' you?", default=True):
            console.print("[dim]Signing in as someone else...[/dim]")
            ok, _ = _shell(["gh", "auth", "login"], interactive=True)
            if not ok:
                console.print("[bold red]Sign-in did not complete. Nothing was changed.[/bold red]")
                return
            account = github_account()
            console.print(f"[dim]now signed in as {account or 'unknown'}[/dim]")

    # 2. A repo is a copy of this folder on someone else's computer.
    if not (project_root / ".git").exists():
        ok, out = _shell(["git", "init"], cwd=project_root)
        if not ok:
            console.print(f"[bold red]git init failed:[/bold red] {out[:300]}")
            return
        console.print("[dim]started a git repository here[/dim]")

    exposed = git_is_clean_of_secrets(project_root)
    if exposed:
        console.print(Panel(
            "[bold red]Stopping: these would be uploaded, and they are "
            "private.[/bold red]\n\n  " + "\n  ".join(exposed) + "\n\n"
            "They must be listed in [bold].gitignore[/bold] first. A private "
            "repository is still a copy on someone else's computer, and one "
            "click can make a repository public later.",
            title="Your secrets are not protected",
            border_style="red",
        ))
        raise typer.Exit(code=1)

    # 3. What goes where
    secrets = secrets_from_env_file(find_env_file())
    secrets = secrets_from_env_file(find_env_file())
    if not secrets:
        console.print(Panel(
            "[bold red]There are no API keys in your .env.[/bold red]\n\n"
            "The scheduled run needs YOUR OWN keys. This folder may have come "
            "from someone else, and their keys neither travel with it nor "
            "would you want them to.\n\n"
            "Run [bold]email-workflow setup[/bold] first to add your own.",
            title="Nothing to log in with",
            border_style="red",
        ))
        if not Confirm.ask("Set the schedule up anyway, without keys?", default=False):
            return

    repo = Prompt.ask("Repository name", default=repo_name_suggestion(project_root))
    cron = utc_cron_for_local_time(hour, minute)
    drift = describe_drift(hour, minute)

    console.print(Panel(
        f"[bold]This will:[/bold]\n"
        f"  1. create a [bold]private[/bold] GitHub repository '{repo}'\n"
        f"  2. push this folder to it\n"
        f"  3. upload {len(secrets)} secret(s) to that repository:\n"
        f"     [dim]{', '.join(sorted(secrets))}[/dim]\n"
        f"  4. run the workflow daily at [bold]{hour:02d}:{minute:02d}[/bold] "
        f"your time (cron [bold]{cron}[/bold] UTC)\n\n"
        + (f"[yellow]{drift}[/yellow]\n\n" if drift else "")
        + "[bold yellow]Your API keys and your Gmail app password will be "
          "stored on GitHub.[/bold yellow] GitHub encrypts them and they are "
          "hidden in logs, but they do leave this computer.",
        title="Before anything is uploaded",
        border_style="yellow",
    ))
    if not Confirm.ask("Go ahead?", default=False):
        console.print("[dim]Stopped. Nothing left this computer.[/dim]")
        return

    # 4. Write the workflow with the chosen time, then commit everything.
    wf_path = project_root / ".github" / "workflows" / "email-workflow.yml"
    if wf_path.exists():
        wf_path.write_text(
            workflow_with_cron(wf_path.read_text(encoding="utf-8"), cron),
            encoding="utf-8",
        )
        console.print(f"[dim]set the schedule in {wf_path.name}[/dim]")

    _shell(["git", "add", "-A"], cwd=project_root)
    ok, out = _shell(
        ["git", "commit", "-m", f"Run the email workflow daily at {hour:02d}:{minute:02d}"],
        cwd=project_root,
    )
    if not ok and "nothing to commit" not in out.lower():
        console.print(f"[yellow]git commit said:[/yellow] {out.strip()[:300]}")

    ok, out = _shell(
        ["gh", "repo", "create", repo, "--private", "--source=.",
         "--remote=origin", "--push"],
        cwd=project_root,
    )
    if not ok:
        if "already exists" in out.lower():
            console.print("[dim]repository already exists - pushing to it[/dim]")
            _shell(["git", "push", "-u", "origin", "HEAD"], cwd=project_root)
        else:
            console.print(Panel(f"[bold red]Could not create the repository.[/bold red]\n\n"
                                f"{out.strip()[:600]}", border_style="red"))
            return

    # The full owner/name. gh wants OWNER/REPO anywhere a repository is named,
    # and a bare folder name is rejected - which is what silently lost every
    # secret the first time.
    ok, out = _shell(["gh", "repo", "view", "--json", "nameWithOwner",
                      "-q", ".nameWithOwner"], cwd=project_root)
    full_name = out.strip().splitlines()[0].strip() if ok and out.strip() else repo

    # 5. The secrets, one at a time, each value on stdin so it never appears in
    #    a command line that other processes on the machine can read. No
    #    --repo: inside the repository gh reads it from the remote.
    sent, failed = 0, []
    for name, value in sorted(secrets.items()):
        ok, out = _shell(["gh", "secret", "set", name],
                         cwd=project_root, stdin_text=value)
        if ok:
            sent += 1
        else:
            failed.append(name)
            console.print(f"[yellow]could not set {name}: {out.strip()[:120]}[/yellow]")

    # A scheduled run with no key fails on the very first email, so this is not
    # a detail to mention in passing - it decides whether any of this works.
    # Announcing "it is live" here would be the same lie the reports used to
    # tell when they called a sent reply a draft.
    if failed:
        listed = "\n  ".join(failed)
        console.print(Panel(
            f"[bold red]{len(failed)} of {len(secrets)} secrets did not upload, "
            f"so this is NOT working yet.[/bold red]\n\n  {listed}\n\n"
            f"The repository exists and the daily time is set, but a run without "
            f"its keys fails on the first email.\n\n"
            f"Add them here:\n"
            f"  https://github.com/{full_name}/settings/secrets/actions\n\n"
            f"or fix the problem above and run [bold]email-workflow schedule[/bold] "
            f"again - it is safe to repeat.",
            title="Not finished",
            border_style="red",
        ))
        return
    console.print(f"[dim]uploaded all {sent} secrets[/dim]")

    console.print(Panel(
        f"[bold green]It is live.[/bold green]\n\n"
        f"GitHub will run it every day at [bold]{hour:02d}:{minute:02d}[/bold] "
        f"your time, whether this computer is on or not.\n\n"
        f"Try it immediately:\n\n"
        f"    gh workflow run \"Email workflow\" --repo {full_name}\n\n"
        f"Watch it:\n\n"
        f"    gh run list --repo {full_name}\n\n"
        f"[dim]GitHub switches scheduled workflows off in a repository with no "
        f"activity for 60 days. If the reports stop, push anything or press "
        f"\"Run workflow\" in the Actions tab.[/dim]",
        title="Scheduled",
        border_style="green",
    ))


@app.command()
def schedule(
    at: Optional[str] = typer.Option(None, "--at", help="Time of day, e.g. 07:30"),
    where: Optional[str] = typer.Option(
        None, "--where", help="computer | github"
    ),
):
    """Run this every day - on this computer, or on GitHub with the PC off."""
    print_banner("SCHEDULE A DAILY RUN")

    project_root = resolve_project_file("config.yaml").parent

    if where is None:
        console.print("[bold yellow]Where should it run?[/bold yellow]\n")
        console.print(
            "[bold]1. On this computer[/bold]\n"
            f"   [dim]{current_system()} handles it. Private and free - but it "
            f"only runs while the computer is awake, so a shut laptop misses "
            f"it.[/dim]\n"
        )
        console.print(
            "[bold]2. On GitHub (free)[/bold]\n"
            "   [dim]Runs on GitHub's machines, so your computer can be off. "
            "You sign in to GitHub once and this sets up everything else.[/dim]\n"
        )
        where = "computer" if Prompt.ask("Choice", choices=["1", "2"], default="2") == "1" else "github"

    where = where.strip().lower()
    if where not in ("computer", "github"):
        console.print("[bold red]--where must be 'computer' or 'github'.[/bold red]")
        raise typer.Exit(code=1)

    while True:
        text = at or Prompt.ask("What time each day?", default="07:30")
        try:
            hour, minute = parse_time(text)
            break
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            if at:
                raise typer.Exit(code=1)

    if where == "computer":
        _schedule_on_this_computer(hour, minute, project_root)
    else:
        _schedule_on_github(hour, minute, project_root)
