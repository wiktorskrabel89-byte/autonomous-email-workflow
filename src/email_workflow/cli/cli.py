from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
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
from rich.table import Column, Table
from rich.prompt import Prompt, Confirm
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from email_workflow.models.config import AppConfig, AIMode
from email_workflow.models.email import EmailMessage, EmailCategory, ImportanceLevel, UrgencyLevel, DecisionOption, SenderInfo
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.providers.ai_factory import get_ai_provider
from email_workflow.providers.key_pool import find_pool, parallel_lanes
from email_workflow.providers.fake_ai import FakeAIProvider
from email_workflow.providers.email_provider import (
    APP_PASSWORD_HELP,
    get_email_provider,
    MockEmailProvider,
)
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
from email_workflow.core.updates import (
    DEFAULT_UPSTREAM,
    PROTECTED,
    apply_update,
    changed_files,
    files_to_copy,
    fetch_upstream,
    keep_your_schedule,
    locally_modified,
    recent_subjects,
    restore_your_schedule,
    update_available,
    write_state,
)
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
    cron_in_workflow,
    local_time_for_utc_cron,
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

# Windows still hands a process a legacy code page when its output is
# redirected, and the tables, the progress bar and the spinner are all
# drawn with characters that code page has no room for. "email-workflow
# run > log.txt" then died on a UnicodeEncodeError rather than on anything
# to do with email. Ask for UTF-8 and never let an unprintable character be
# the reason a run fails.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

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


def nothing_is_set_up(config) -> bool:
    """True when this copy has never been configured, so it could not work.

    Deliberately judged from what is actually there rather than from a "have I
    run before" marker: a marker can be true while the key it refers to has
    been deleted, and then the app sends someone to a menu where every option
    fails. These two are what the app cannot work without.

    A mock inbox or the fake provider is somebody testing on purpose, and is
    left alone.
    """
    if config.ai.api.provider.lower() == "fake" or config.email.provider == "mock":
        return False
    has_key = bool((os.getenv(config.ai.api.api_key_env) or "").strip())
    has_mailbox = bool((os.getenv("GMAIL_ADDRESS") or "").strip())
    return not (has_key and has_mailbox)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Default callback launching interactive main menu if no command argument is passed."""
    _require_login()
    if ctx.invoked_subcommand is not None:
        return

    # First run: go straight into the wizard rather than showing a menu whose
    # every option would fail for want of a key and a mailbox. The menu is
    # still there afterwards, and option 3 reopens this wizard any time.
    if nothing_is_set_up(_load_config_quietly()):
        console.print(Panel(
            "[bold]Nothing is set up yet, so this is the setup wizard.[/bold]\n\n"
            "It needs two things to work: an AI key and your mailbox. This asks "
            "for both, then a couple of questions about you.\n\n"
            "[dim]Everything stays on this machine. You can stop at any point "
            "with Ctrl+C, and reopen this from the menu (option 3).[/dim]",
            title="Welcome",
            border_style="cyan",
        ))
        try:
            setup()
        except KeyboardInterrupt:
            console.print("\n[yellow]Setup stopped. Nothing was saved.[/yellow]")
            return

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

from email_workflow.core.known_facts import KnownFactsManager, append_fact, facts_lost

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
            "[bold yellow]12.[/bold yellow] Update the App "
            "[dim](get the newest version, keeps your settings)[/dim]\n"
            "[bold yellow]13.[/bold yellow] Exit\n"
        )
        console.print(Panel(menu_text, border_style="cyan"))

        choice = Prompt.ask("Select option", choices=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"], default="1")

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
            console.clear()
            update(check=False)
            Prompt.ask("\nPress Enter to return to main menu")
        elif choice == "13":
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
        console.print("2. Google Gemini (e.g. gemini-3.1-flash-lite)")
        console.print("3. Groq (e.g. llama-3.3-70b-versatile)")
        console.print("4. OpenRouter (e.g. anthropic/claude-3.5-sonnet)")

        prov_map = {"1": ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
                    "2": ("gemini", "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
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
        # An app password cannot be created at all until this is on, and the
        # app-password page does not tell you that - it just says the setting
        # is not available for your account.
        two_step_url = "https://myaccount.google.com/signinoptions/twosv"
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
                f"\n[bold white]Step 1: turn on 2-Step Verification "
                f"first.[/bold white]\n"
                f"  [bold cyan][link={two_step_url}]{two_step_url}[/link][/bold cyan]\n\n"
                f"[dim]Google only offers app passwords on accounts that have "
                f"2-Step Verification switched on. Without it the app-password "
                f"page does not say why - it just tells you the setting is not "
                f"available for your account, which looks like the page is "
                f"broken. If yours is already on, skip this step.[/dim]\n"
            )
            if Confirm.ask("Open the 2-Step Verification page?", default=True):
                webbrowser.open(two_step_url)
                console.print(
                    "[dim]Turn it on there (phone or authenticator app), then "
                    "come back here.[/dim]"
                )
                Prompt.ask("Press Enter once 2-Step Verification is on", default="")

            console.print(
                f"\n[bold white]Step 2:[/bold white] Generate a Gmail App Password at:\n"
                f"  [bold cyan][link={app_pwd_url}]{app_pwd_url}[/link][/bold cyan]\n"
            )
            console.print("[dim](Opening the link in your browser now...)[/dim]")
            webbrowser.open(app_pwd_url)
            console.print(
                "\n[bold white]Step 3:[/bold white] On that page:\n"
                "  1. Type an app name (e.g. [bold]Email Workflow[/bold])\n"
                "  2. Click [bold]Create[/bold]\n"
                "  3. Copy the [bold]16-character password[/bold] shown (remove spaces)\n\n"
                "[dim]If that page says the setting is not available for your "
                "account, 2-Step Verification is still off - go back to Step 1. "
                "This is not your normal Gmail password, and your normal "
                "password will not work here.[/dim]\n"
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

    # The wizard has set up WHO it talks to. It has said nothing about WHAT it
    # is allowed to do with your mailbox, and those are the settings that
    # decide whether real email goes out in your name. Ending here would leave
    # someone thinking they had finished when they had not seen the question.
    console.print(Panel(
        "[bold]One thing left: what is it allowed to do?[/bold]\n\n"
        "Right now it knows which AI to use and which mailbox to read. It does "
        "not yet know whether it may [bold]send replies for you[/bold], keep "
        "drafts, take routine mail out of your inbox, or star what needs you.\n\n"
        "[dim]Sending is off until you switch it on, so nothing leaves your "
        "mailbox in the meantime. You can change any of this later with "
        "'email-workflow settings'.[/dim]",
        title="Sending & mailbox behaviour",
        border_style="cyan",
    ))

    if Confirm.ask("Set those now?", default=True):
        console.print()
        settings()
    else:
        console.print(
            "[dim]Left as they are. Run [bold]email-workflow settings[/bold] "
            "whenever you want them.[/dim]"
        )

    _ask_about_you(config, config_path)

    console.print(Panel(
        "[bold green]Setup finished.[/bold green]\n\n"
        "  [bold]email-workflow demo[/bold]      see it work, no key needed\n"
        "  [bold]email-workflow run[/bold]       process your inbox now\n"
        "  [bold]email-workflow schedule[/bold]  run it daily, here or on GitHub\n"
        "  [bold]email-workflow facts[/bold]     what it may say about you",
        title="What now",
        border_style="green",
    ))

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
        sender=SenderInfo(name="Test Sender", email="test@example.com"),
        subject="[TEST] This is a test report",
        body=(
            "This is a test message from email-workflow. Nothing here is real: "
            "no such email arrived and nothing was done to your mailbox.\n\n"
            "It is here so you can check that reports reach you."
        ),
        received_at="2026-09-19T16:00:00Z",
    )

    # Deliberately dull. It used to invent a disputed wire transfer from a
    # "Billing Fraud Dept", which reads as a real fraud alert at a glance -
    # a test that frightens the person testing is a bad test.
    dummy_analysis = EmailAnalysis(
        message_id="msg_test_report",
        thread_id="thread_test_report",
        sender=dummy_msg.sender,
        subject=dummy_msg.subject,
        received_at=dummy_msg.received_at,
        category=EmailCategory.OTHER,
        importance=ImportanceLevel.LOW,
        urgency=ImportanceLevel.LOW,
        action_required=False,
        response_required=True,
        safe_to_automate=False,
        confidence=0.95,
        missing_information=[],
        commitments_implied=[],
        recommended_decision=DecisionOption.CREATE_DRAFT,
        reasoning="A test report. No real email was read and nothing was sent.",
    )

    results = dispatcher.notify(
        message=dummy_msg,
        analysis=dummy_analysis,
        decision=DecisionOption.CREATE_DRAFT,
        draft_id="draft_test_report",
        reply_text=(
            "Hi,\n\nThanks for the message - this is what a reply written by "
            "the AI looks like in your report.\n\nBest regards,\nYour name"
        ),
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
    console.print(
        f"[yellow]Hit the free limit of {requests_per_minute} requests per minute. "
        f"Waiting {_how_long(seconds)}, then carrying on by itself - nothing is "
        f"broken, you do not have to do anything.[/yellow]"
    )


def _how_long(seconds: float) -> str:
    wait = int(seconds) + 1 if seconds % 1 else int(seconds)
    if wait >= 60:
        return "about a minute" if wait < 90 else f"{round(wait / 60)} minutes"
    if wait >= 10:
        return f"{wait} seconds"
    return f"{wait} second" + ("s" if wait != 1 else "")


def _announce_pause(seconds: float, who: str) -> None:
    """A wait the server asked for, explained while it happens.

    Different from the throttle message above: that one is us spacing requests
    out, this one is the provider saying "not right now". It is a pause, not a
    failure - saying so is what stops it reading as the app giving up.
    """
    console.print(
        f"[yellow]Waiting {_how_long(seconds)} - {who} asked for a short break. "
        f"It carries on by itself; nothing is broken and nothing is "
        f"lost.[/yellow]"
    )


def _announce_switch(from_link, to_link, error) -> None:
    """Tell the user, mid-run, that the chain moved to another provider."""
    why = {
        "rate_limit": "busy right now",
        "unavailable": "overloaded right now",
        "quota": "out of free quota",
        "auth": "key rejected",
        "model_gone": "model no longer served",
        "no_access": "key not allowed to use it",
        "network": "could not be reached",
    }.get(error.kind, error.kind)
    console.print(
        f"[yellow]{from_link} stopped working ({why}). "
        f"Switching to {to_link} and carrying on.[/yellow]"
    )


MAX_PARALLEL_WORKERS = 8


# Characters that make a line's width unpredictable. An emoji-presentation
# sequence - a plain symbol followed by U+FE0F - is measured as ONE cell by
# rich and drawn as TWO by the terminal, and one cell of disagreement is all it
# takes: the progress line then runs one column past the edge, wraps, and a
# wrapped line cannot be overwritten in place. The bar reprints itself instead,
# twelve times a second, for as long as that email takes.
#
# That is the "ferie spam": the subject "Otwieramy rezerwacje na ferie! [skier]"
# ends in U+26F7 U+FE0F, and it printed a few hundred copies of the bar.
# Emoji that terminals and rich agree on (rocket, eyes) were never a problem.
_UNMEASURABLE = (
    "️"        # emoji presentation selector - the actual culprit
    "︎"        # text presentation selector
    "‍"        # zero-width joiner, for multi-part emoji
    "⃣"        # combining enclosing keycap
)
# Symbols that gain emoji width from the selector above. Dropping the selector
# alone would leave these behind at the width rich expects, but a terminal may
# still draw some of them wide, so they go too.
_AMBIGUOUS_WIDTH = [
    (0x2190, 0x2BFF),   # arrows, misc symbols, dingbats - where U+26F7 lives
    (0xFE00, 0xFE0F),
    (0x1F3FB, 0x1F3FF), # skin tone modifiers
]


def _one_line(text: str) -> str:
    """Text safe to put in a bar that redraws itself in place.

    Anything whose printed width we cannot predict is removed rather than
    guessed at, and newlines are flattened: a subject line is arbitrary text
    from a stranger, and this one goes into a live-redrawing bar.
    """
    out = []
    for ch in text:
        if ch in _UNMEASURABLE:
            continue
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _AMBIGUOUS_WIDTH):
            continue
        out.append(" " if ch in "\r\n\t" else ch)
    return " ".join("".join(out).split())


def _short(subject: str, width: int = 55) -> str:
    subject = _one_line(subject or "").strip() or "(no subject)"
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


def _progress(unit_note=""):
    """The one progress bar this app uses, so every job looks the same.

    A spinner says "still alive". A bar says how far through and how much
    longer - which is what you actually want to know when a full inbox is
    being worked through one AI call at a time.
    """
    return Progress(
        SpinnerColumn(),
        # no_wrap + ellipsis as a second line of defence: if anything still
        # measures wider than expected, it is cut off rather than wrapped onto
        # a second line the bar can never overwrite.
        TextColumn(
            "[bold cyan]{task.description}",
            table_column=Column(no_wrap=True, overflow="ellipsis"),
        ),
        BarColumn(bar_width=None, complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TextColumn(unit_note) if unit_note else TextColumn(""),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _process_one_at_a_time(pipeline, emails, provider_name, model_name):
    """Returns (results, what stopped it early or None).

    The error is handed back rather than thrown: the emails already worked on
    are real work, and losing the digest for them because the twentieth one
    could not be classified is the bug this run kept hitting.
    """
    results = []
    total = len(emails)
    stopped = None
    with _progress(f"[dim]{provider_name} / {model_name}[/dim]") as bar:
        job = bar.add_task("starting...", total=total)
        for idx, email in enumerate(emails, 1):
            # One email can be several AI calls and a rate-limit wait. Without
            # something moving, the app looks frozen - and it is not obvious
            # when it has moved on to the next email either.
            bar.update(job, description=f'"{_short(email.subject, 40)}"')
            try:
                res = pipeline.process_email(email)
            except WorkflowError as e:
                stopped = e
                break
            results.append(res)
            bar.advance(job)
            render_stage_result(idx, res)
        bar.update(job, description="stopped" if stopped else "done")
    return results, stopped


def _process_together(pipeline, emails, workers: int):
    """Several emails at once, one API key each.

    Each result is shown the moment it lands rather than in inbox order. With
    several emails in flight the order they finish in is what actually
    happened, and holding everything back until the slowest one returns would
    throw away the point of running them together.
    """
    total = len(emails)
    results = [None] * total
    stopped = None

    with _progress(f"[dim]{workers} keys[/dim]") as bar:
        job = bar.add_task(f"{min(workers, total)} at a time", total=total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {
                pool.submit(pipeline.process_email, email): i
                for i, email in enumerate(emails)
            }
            for job_done in as_completed(jobs):
                index = jobs[job_done]
                try:
                    results[index] = job_done.result()
                except WorkflowError as e:
                    # One email that could not be worked on must not throw
                    # away the ones that were. The others in flight are left
                    # to finish - they may well succeed on another key.
                    stopped = stopped or e
                    bar.advance(job)
                    continue
                bar.advance(job)
                left = total - int(bar.tasks[0].completed)
                bar.update(job, description=f"{min(workers, left)} at a time"
                           if left else "done")
                render_stage_result(index + 1, results[index])

    return [res for res in results if res is not None], stopped


def _process_inbox(pipeline, email_provider, config):
    """One full pass over the inbox: fetch, process each email, send the digest.

    Returns (results, what stopped it early or None).
    """
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
        return [], None

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
        results, stopped = _process_together(pipeline, emails, workers)
    else:
        results, stopped = _process_one_at_a_time(
            pipeline, emails, provider_name, model_name
        )

    # A run that cannot go on still says what it did get done, and still sends
    # the report. It used to die here with the whole pass thrown away, which is
    # what "it randomly stops working" looked like from the outside.
    if stopped is not None:
        done, left = len(results), len(emails) - len(results)
        _print_workflow_error(stopped)
        console.print(
            Panel(
                f"[bold yellow]Stopped after {done} of {len(emails)}.[/bold yellow]\n\n"
                f"{left} email" + ("s were" if left != 1 else " was") + " not looked at. "
                f"They are still unread, so the next run picks up exactly where "
                f"this one stopped - nothing is lost and nothing is done twice.\n\n"
                f"[dim]The report below covers the {done} that were "
                f"finished.[/dim]",
                border_style="yellow",
            )
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
    return results, stopped


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
            config,
            on_switch=_announce_switch,
            on_throttle=_announce_throttle,
            on_pause=_announce_pause,
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
            _, stopped = _process_inbox(pipeline, email_provider, config)
            if stopped is not None:
                # The report has already gone out with what was finished. The
                # non-zero exit is for whatever ran this - a scheduled run that
                # only got halfway is not a green run.
                raise typer.Exit(code=1)
            console.print("\n[bold green][OK] Processing complete.[/bold green]")
            _notice_if_out_of_date(config, resolve_project_file("config.yaml").parent)

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
    # Google lists its models as "models/gemini-3.1-flash-lite" while config.yaml
    # names them without the prefix, which is also how the API wants them sent.
    # Comparing the two strings as they come made the app announce that a model
    # it was happily using was not available - a false alarm that sends you off
    # changing a setting that was never wrong.
    def _same_model(listed: str) -> bool:
        return current and listed.split("/")[-1] == current.split("/")[-1]

    for model_id in available:
        table.add_row(model_id, "<- in your config" if _same_model(model_id) else "")
    console.print(table)

    if current and not any(_same_model(model_id) for model_id in available):
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
                + APP_PASSWORD_HELP,
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
                f"[bold yellow]What to do:[/bold yellow]\n" + APP_PASSWORD_HELP,
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
    table.add_row("Learn facts from email", show(config.automation.learn_facts_from_email),
                  "suggest things about you from your own mail")
    console.print(table)

    # Asked one at a time, not behind a single yes. Each of these does
    # something different to your mailbox, and a single gate meant people
    # answered no and never saw the four questions behind it.
    console.print("\n[dim]Each one, in turn. Press Enter to keep what it says in brackets.[/dim]\n")

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

    # --- learning about you ------------------------------------------------
    if not config.automation.learn_facts_from_email:
        console.print(
            "\n[dim]With this on, 'email-workflow facts' can read your recent "
            "mail and suggest things to record about you. It only ever "
            "suggests: you pick each one, and nothing is saved until you do. "
            "A wrong fact would be stated to real people as true.[/dim]"
        )
    config.automation.learn_facts_from_email = Confirm.ask(
        "Let the AI suggest facts about you from your email?",
        default=config.automation.learn_facts_from_email,
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

def _facts_ai(config):
    """A model to tidy the facts with, or the one that just appends.

    Never fatal: not being able to reach a model is a reason to add the line
    as written, not a reason to refuse to record it.
    """
    try:
        return get_ai_provider(config)
    except Exception:
        return FakeAIProvider()


def _save_facts(mgr, new_text, old_text):
    """Write the file, keeping the previous version next to it.

    This file is the only thing the assistant may state as fact about you, and
    it took real effort to write. A copy costs nothing and has already been
    needed once.
    """
    backup = mgr.file_path.with_suffix(mgr.file_path.suffix + ".bak")
    try:
        backup.write_text(old_text, encoding="utf-8")
    except OSError:
        backup = None
    mgr.save_facts(new_text)
    if backup:
        console.print(f"[dim]previous version kept as {backup.name}[/dim]")


def _confirm_and_save(mgr, current, merged, verb="Save this"):
    """Show what would change, refuse to lose anything quietly, then save."""
    new_text = "\n".join(line.strip() for line in merged.facts if line.strip()) + "\n"
    if not new_text.strip():
        console.print("[bold red]The model returned nothing. Your facts are "
                      "untouched.[/bold red]")
        return False

    lost = facts_lost(current, new_text, merged.replaced)

    console.print(Panel(new_text.rstrip(), title="How it would look",
                        border_style="cyan"))
    if merged.what_changed:
        console.print(f"[dim]{merged.what_changed}[/dim]")
    if merged.replaced:
        console.print("[yellow]Updated, replacing:[/yellow]")
        for line in merged.replaced:
            console.print(f"  [dim]{line}[/dim]")

    if lost:
        console.print(Panel(
            "[bold red]These would disappear, and nothing said they were "
            "being replaced:[/bold red]\n\n  " + "\n  ".join(lost) + "\n\n"
            "That is the model losing something, not tidying it.",
            title="Careful",
            border_style="red",
        ))
        if not Confirm.ask("Save anyway and lose those?", default=False):
            console.print("[dim]Nothing changed.[/dim]")
            return False
    elif not Confirm.ask(f"{verb}?", default=True):
        console.print("[dim]Nothing changed.[/dim]")
        return False

    _save_facts(mgr, new_text, current)
    console.print("[bold green][OK] Saved.[/bold green]")
    return True


def _learn_from_email(mgr, config, current):
    """Let the AI propose facts from your own mail. It only ever proposes."""
    try:
        provider = get_email_provider(config.email)
        emails = provider.fetch_unprocessed_emails()
    except Exception as e:
        console.print(f"[bold red]Could not read the mailbox:[/bold red] {e}")
        return

    if not emails:
        console.print("[dim]No unread mail to learn from.[/dim]")
        return

    # Enough to be useful, bounded so one enormous newsletter cannot swallow
    # the whole prompt - and the cost of the call with it.
    looked_at = emails[:15]
    blob = "\n\n".join(
        f"From: {m.sender.name} <{m.sender.email}>\nSubject: {m.subject}\n\n"
        f"{(m.body or '')[:800]}"
        for m in looked_at
    )

    with console.status(f"[cyan]reading {len(looked_at)} email(s)...", spinner="dots"):
        suggested = _facts_ai(config).suggest_facts(blob, current)

    candidates = [line.strip() for line in suggested.facts if line.strip()]
    if not candidates:
        console.print(Panel(
            "Nothing in your recent mail states a fact about you plainly "
            "enough to be worth recording.\n\n"
            "[dim]That is the right answer far more often than not - a guessed "
            "fact would be repeated to real people as if it were true.[/dim]",
            title="Nothing to add",
            border_style="cyan",
        ))
        return

    console.print(Panel(
        "\n".join(f"[bold yellow]{n}.[/bold yellow] {line}"
                  for n, line in enumerate(candidates, 1)),
        title=f"Suggested from {len(looked_at)} email(s) - nothing is saved yet",
        border_style="yellow",
    ))
    console.print("[dim]Check each one. The assistant will state these to real "
                  "people as fact.[/dim]")

    picked_text = Prompt.ask(
        "Which do you want to keep? (numbers like 1,3 - or 'all', or 'none')",
        default="none",
    ).strip().lower()

    if picked_text in ("none", ""):
        console.print("[dim]Nothing kept.[/dim]")
        return
    if picked_text == "all":
        chosen = candidates
    else:
        chosen = []
        for piece in picked_text.replace(" ", "").split(","):
            if piece.isdigit() and 1 <= int(piece) <= len(candidates):
                chosen.append(candidates[int(piece) - 1])
        if not chosen:
            console.print("[yellow]Nothing recognised in that answer - nothing "
                          "kept.[/yellow]")
            return

    with console.status("[cyan]filing them in...", spinner="dots"):
        merged = _facts_ai(config).organise_facts(current, "\n".join(chosen))
    _confirm_and_save(mgr, current, merged, verb="Add these")


def _add_to_facts(mgr, config, current):
    console.print(
        "\n[bold]What should it know?[/bold] One thing, in your own words - "
        "the AI files it in the right place.\n"
        "[dim]e.g. \"my phone is 600 100 200\" or \"I don't work Fridays\"[/dim]"
    )
    addition = Prompt.ask("New information").strip()
    if not addition:
        console.print("[dim]Nothing typed.[/dim]")
        return

    with console.status("[cyan]filing it in...", spinner="dots"):
        merged = _facts_ai(config).organise_facts(current, addition)

    _confirm_and_save(mgr, current, merged)


def _rewrite_facts(mgr, current):
    console.print(Panel(
        "[bold yellow]This replaces everything above.[/bold yellow]\n\n"
        "To add one thing without losing the rest, go back and choose "
        "[bold]1[/bold] instead.",
        border_style="yellow",
    ))
    if not Confirm.ask("Replace the whole knowledge base?", default=False):
        return

    console.print("[yellow]Type the new knowledge base. Press Enter twice when "
                  "finished:[/yellow]")
    lines = []
    while True:
        line = input()
        if not line and lines and not lines[-1]:
            break
        lines.append(line)
    new_text = "\n".join(lines).strip()
    if not new_text:
        console.print("[dim]Nothing typed - your facts are untouched.[/dim]")
        return

    _save_facts(mgr, new_text + "\n", current)
    console.print("[bold green][OK] Replaced.[/bold green]")


@app.command()
def facts():
    """View or edit what the AI is allowed to state as fact about you."""
    print_banner("PERSONAL KNOWLEDGE BASE & KNOWN FACTS")
    mgr = KnownFactsManager()
    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()

    while True:
        current = mgr.load_facts()
        console.print(Panel(current.rstrip(), title="What it knows about you",
                            border_style="cyan"))

        # Numbered as they are shown. A hidden option that still answers to its
        # old number is a trap, and a gap in the numbering looks like a fault.
        options = [
            ("add", "Add something new [dim](keeps everything else)[/dim]"),
            ("rewrite", "Rewrite the whole thing"),
        ]
        if config.automation.learn_facts_from_email:
            options.append(("learn", "Learn from my recent email"))
        options.append(("back", "Back"))

        console.print("\n".join(
            f"[bold yellow]{n}.[/bold yellow] {label}"
            for n, (_, label) in enumerate(options, 1)
        ) + "\n")

        picked = Prompt.ask(
            "Choice",
            choices=[str(n) for n in range(1, len(options) + 1)],
            default=str(len(options)),
        )
        action = options[int(picked) - 1][0]

        if action == "back":
            return
        if action == "add":
            _add_to_facts(mgr, config, current)
        elif action == "rewrite":
            _rewrite_facts(mgr, current)
        else:
            _learn_from_email(mgr, config, current)
        console.print()


def _read_text(path) -> str:
    """A file's contents, or "" if it is not there. Never raises."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def _only_the_launcher_was_locked(output: str) -> bool:
    """Whether pip's only complaint was that it could not replace our own .exe.

    Windows locks a running executable, and the program asking for the update
    IS email-workflow.exe - so pip can never replace it from in here. Nothing
    is actually wrong: an editable install runs from the source that was just
    copied in, and the launcher only needs rewriting if the entry points moved.
    """
    text = (output or "").lower()
    locked = (
        "winerror 32" in text
        or "used by another process" in text
        # Windows reports this in the user's own language.
        or "używany przez inny proces" in text
    )
    return locked and "email-workflow.exe" in text


def _ask_a_few(question: str, example: str, limit: int = 3) -> list:
    """Up to `limit` short answers, Enter to stop. Never loops for ever."""
    answers = []
    for n in range(limit):
        if n == 0:
            console.print(f"[dim]for example: {example}[/dim]")
        answer = Prompt.ask(f"  {n + 1}", default="").strip()
        if not answer:
            break
        answers.append(answer)
    return answers


def _ask_about_you(config, config_path) -> None:
    """The last step of the wizard: what to tell you about, and who you are.

    These are two halves of the same thing, which is why they are asked
    together. What it tells you about is what it must never file away, and what
    it knows about you is what it may say on your behalf - and the app is only
    as useful as those two lists. With both empty it archives things that
    mattered (the reason this step exists) and answers every question with
    "[NEEDS INPUT]".
    """
    console.print(Panel(
        "[bold]Last thing: tell it about you.[/bold]\n\n"
        "Two short lists, and they go together.\n\n"
        "[bold]1. What should it always tell you about?[/bold] Anything on that "
        "list is starred and left in your inbox, however routine it looks - it "
        "is never filed away.\n"
        "[bold]2. What should it know about you?[/bold] These are the only "
        "facts it may state as yours when it writes a reply. It never invents "
        "one; without them it just says a detail is missing.\n\n"
        "[dim]Press Enter on an empty line to move on. Both can be changed "
        "later - 'email-workflow settings' and 'email-workflow facts'.[/dim]",
        title="About you",
        border_style="cyan",
    ))

    console.print("\n[bold cyan]Things to always tell you about[/bold cyan]")
    topics = _ask_a_few(
        "topic",
        "job offers and anything about my applications  /  "
        "anything from my landlord  /  my daughter's school",
    )
    if topics:
        existing = list(config.automation.never_archive_about or [])
        config.automation.never_archive_about = existing + [
            t for t in topics if t not in existing
        ]
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                import yaml
                yaml.safe_dump(
                    config.model_dump(mode="json"), f,
                    default_flow_style=False, allow_unicode=True,
                )
            console.print(f"[green]Saved {len(topics)}. Those are never archived.[/green]")
        except OSError as e:
            console.print(f"[yellow]Could not save those to config.yaml: {e}[/yellow]")
    else:
        console.print("[dim]None given. Routine mail is filed away as usual.[/dim]")

    console.print("\n[bold cyan]Things it should know about you[/bold cyan]")
    facts = _ask_a_few(
        "fact",
        "I work 9 to 5 CET  /  I am free for calls on Wednesday afternoons  /  "
        "I am a freelance designer",
    )
    if not facts:
        console.print(
            "[dim]None given. It will not invent any - it will say a detail is "
            "missing instead. Add them later with 'email-workflow facts'.[/dim]"
        )
        return

    manager = KnownFactsManager()
    try:
        # Appended one at a time, never written over the top: this file is a
        # knowledge base someone has built up, and replacing it wholesale has
        # eaten one before.
        text = manager.load_facts()
        for fact in facts:
            text = append_fact(text, fact)
        manager.save_facts(text)
        console.print(f"[green]Saved {len(facts)}. Nothing already there was touched.[/green]")
    except Exception as e:
        console.print(f"[yellow]Could not save those: {e}[/yellow]")


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


def _github_account(project_root):
    ok, who = _shell(["gh", "api", "user", "-q", ".login"], cwd=project_root)
    return who.strip().splitlines()[0].strip() if ok and who.strip() else ""


def _existing_github_repo(project_root):
    """The repo this folder already pushes to, or "" if there is none.

    Without this, a second run tries to create a repository that exists, and
    the only sign is a line saying so while everything else is done twice.
    Someone coming back to change the hour should not be made to think about
    repositories at all.
    """
    ok, url = _shell(["git", "remote", "get-url", "origin"], cwd=project_root)
    if not ok or "github.com" not in url:
        return ""
    ok, name = _shell(["gh", "repo", "view", "--json", "nameWithOwner",
                       "-q", ".nameWithOwner"], cwd=project_root)
    return name.strip().splitlines()[0].strip() if ok and name.strip() else ""


def _write_cron_and_push(project_root, cron, hour, minute, full_name):
    """Put the time in the workflow and push it. Returns True if it landed."""
    wf_path = project_root / ".github" / "workflows" / "email-workflow.yml"
    if not wf_path.exists():
        console.print("[bold red]The workflow file is missing, so there is "
                      "nothing to schedule.[/bold red]")
        return False

    wf_path.write_text(
        workflow_with_cron(wf_path.read_text(encoding="utf-8"), cron),
        encoding="utf-8",
    )
    _shell(["git", "add", "-A"], cwd=project_root)
    ok, out = _shell(
        ["git", "commit", "-m", f"Run the email workflow daily at {hour:02d}:{minute:02d}"],
        cwd=project_root,
    )
    if not ok and "nothing to commit" not in out.lower():
        console.print(f"[yellow]git commit said:[/yellow] {out.strip()[:200]}")

    ok, out = _shell(["git", "push", "origin", "HEAD"], cwd=project_root)
    if not ok:
        console.print(Panel(
            f"[bold red]The new time is saved here but could not be pushed, so "
            f"GitHub is still running the old one.[/bold red]\n\n{out.strip()[:400]}",
            border_style="red",
        ))
        return False
    return True


def _upload_secrets(project_root, secrets, full_name):
    """Each value on stdin, so it never appears in a command line other
    processes on this machine can read. No --repo: inside the repository gh
    reads it from the remote, and a bare folder name is not the OWNER/REPO it
    expects - which is what silently lost every secret the first time."""
    sent, failed = 0, []
    for name, value in sorted(secrets.items()):
        ok, out = _shell(["gh", "secret", "set", name],
                         cwd=project_root, stdin_text=value)
        if ok:
            sent += 1
        else:
            failed.append(name)
            console.print(f"[yellow]could not set {name}: {out.strip()[:120]}[/yellow]")

    if failed:
        listed = "\n  ".join(failed)
        console.print(Panel(
            f"[bold red]{len(failed)} of {len(secrets)} keys did not upload, so "
            f"this is NOT working yet.[/bold red]\n\n  {listed}\n\n"
            f"A run without its keys fails on the first email.\n\n"
            f"Add them here:\n"
            f"  https://github.com/{full_name}/settings/secrets/actions",
            title="Not finished",
            border_style="red",
        ))
        return False
    console.print(f"[dim]uploaded all {sent} keys[/dim]")
    return True


def _ask_time(default="07:30"):
    """Ask until it is a time, or give up if the user is clearly done."""
    while True:
        try:
            return parse_time(Prompt.ask("What time each day?", default=default))
        except ValueError as e:
            console.print(f"[red]{e}[/red]")


def _current_schedule(wf_path):
    """(hour, minute) the workflow currently fires at, in local time."""
    if not wf_path.exists():
        return None
    cron = cron_in_workflow(wf_path.read_text(encoding="utf-8"))
    return local_time_for_utc_cron(cron) if cron else None


def _github_menu(project_root, full_name, wf_path, preset=None):
    """Everything you can do once it is already on GitHub.

    Returning "create" means the user asked for a different repository, and the
    caller falls through to the first-time path.
    """
    while True:
        at = _current_schedule(wf_path)
        running = f"{at[0]:02d}:{at[1]:02d}" if at else "an unknown time"

        console.print(Panel(
            f"  repository: [bold]{full_name}[/bold]\n"
            f"  runs daily at: [bold]{running}[/bold] your time\n\n"
            f"[bold yellow]1.[/bold yellow] Change the time\n"
            f"[bold yellow]2.[/bold yellow] Run it now\n"
            f"[bold yellow]3.[/bold yellow] See the last runs on GitHub\n"
            f"[bold yellow]4.[/bold yellow] Upload your keys again\n"
            f"[bold yellow]5.[/bold yellow] Use a different repository\n"
            f"[bold yellow]6.[/bold yellow] Back",
            title="Already on GitHub",
            border_style="cyan",
        ))
        choice = Prompt.ask("Choice", choices=["1", "2", "3", "4", "5", "6"], default="6")

        if choice == "6":
            return ""

        if choice == "1":
            hour, minute = preset or _ask_time(running if at else "07:30")
            preset = None
            if at == (hour, minute):
                console.print("[dim]That is already the time it runs at.[/dim]\n")
                continue
            cron = utc_cron_for_local_time(hour, minute)
            drift = describe_drift(hour, minute)
            if drift:
                console.print(f"[yellow]{drift}[/yellow]")
            if _write_cron_and_push(project_root, cron, hour, minute, full_name):
                console.print(Panel(
                    f"[bold green]Done.[/bold green] It now runs daily at "
                    f"[bold]{hour:02d}:{minute:02d}[/bold] your time.",
                    border_style="green",
                ))

        elif choice == "2":
            console.print("[dim]asking GitHub to run it now...[/dim]")
            ok, out = _shell(["gh", "workflow", "run", "Email workflow"],
                             cwd=project_root)
            if ok:
                console.print(Panel(
                    "[bold green]Started.[/bold green] It takes about a minute.\n\n"
                    "Choose [bold]3[/bold] in a moment to see how it went.",
                    border_style="green",
                ))
            else:
                console.print(Panel(
                    f"[bold red]GitHub would not start it.[/bold red]\n\n"
                    f"{out.strip()[:400]}\n\n"
                    f"[dim]A workflow that has never run once may need enabling "
                    f"in the Actions tab first.[/dim]",
                    border_style="red",
                ))

        elif choice == "3":
            # JSON rather than gh's --template: a Go template renders the run
            # id as 3.5449167057e+10, which is not an id anyone can paste.
            ok, out = _shell(
                ["gh", "run", "list", "--limit", "10",
                 "--json", "status,conclusion,createdAt,databaseId"],
                cwd=project_root,
            )
            try:
                runs = json.loads(out) if ok and out.strip() else []
            except ValueError:
                runs = []
            if not runs:
                console.print("[dim]No runs yet - choose 2 to start one.[/dim]\n")
                continue

            table = Table(title="Runs on GitHub", border_style="cyan")
            table.add_column("When (UTC)")
            table.add_column("State")
            table.add_column("Result")
            table.add_column("Run id", style="dim")
            for entry in runs:
                result = entry.get("conclusion") or "-"
                colour = {"success": "green", "failure": "red"}.get(result, "yellow")
                table.add_row(
                    str(entry.get("createdAt", ""))[:16].replace("T", " "),
                    str(entry.get("status", "")),
                    f"[{colour}]{result}[/{colour}]",
                    str(int(entry.get("databaseId", 0))),
                )
            console.print(table)
            console.print(
                f"[dim]Why one of them failed:  "
                f"gh run view <run id> --log-failed --repo {full_name}[/dim]\n"
            )

        elif choice == "4":
            secrets = secrets_from_env_file(find_env_file())
            if not secrets:
                console.print("[bold red]There are no keys in your .env to "
                              "upload.[/bold red]\n")
                continue
            console.print(
                f"[yellow]About to upload {len(secrets)} key(s) to {full_name}: "
                f"{', '.join(sorted(secrets))}[/yellow]"
            )
            if Confirm.ask("Upload them?", default=False):
                _upload_secrets(project_root, secrets, full_name)

        elif choice == "5":
            console.print(Panel(
                f"This folder currently pushes to [bold]{full_name}[/bold].\n\n"
                f"Using a different repository points it somewhere else. The "
                f"old repository is not deleted and its schedule keeps running "
                f"until you turn it off in its Actions tab - otherwise you get "
                f"the same reports twice.",
                title="A different repository",
                border_style="yellow",
            ))
            if not Confirm.ask("Point this folder at a new repository?", default=False):
                continue
            _shell(["git", "remote", "remove", "origin"], cwd=project_root)
            return "create"


def _schedule_on_github(project_root, preset=None):
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

    account = _github_account(project_root)
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
            account = _github_account(project_root)
            console.print(f"[dim]now signed in as {account or 'unknown'}[/dim]")

    wf_path = project_root / ".github" / "workflows" / "email-workflow.yml"

    # 2. Already set up? Then this is almost always "change the hour", and
    #    nobody should have to think about repositories to do that.
    existing = _existing_github_repo(project_root) if (project_root / ".git").exists() else ""
    if existing:
        if _github_menu(project_root, existing, wf_path, preset=preset) != "create":
            return
        existing = ""

    # 3. A first time: a repo is a copy of this folder on someone else's
    #    computer, so everything below is checked before anything is created.
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

    # Only now is a time actually needed.
    hour, minute = preset or _ask_time()
    cron = utc_cron_for_local_time(hour, minute)
    drift = describe_drift(hour, minute)

    repo = Prompt.ask("Repository name", default=repo_name_suggestion(project_root))

    console.print(Panel(
        f"[bold]This will:[/bold]\n"
        f"  1. create a [bold]private[/bold] GitHub repository '{repo}'\n"
        f"  2. push this folder to it\n"
        f"  3. upload {len(secrets)} key(s) to that repository:\n"
        f"     [dim]{', '.join(sorted(secrets))}[/dim]\n"
        f"  4. run it daily at [bold]{hour:02d}:{minute:02d}[/bold] your time "
        f"(cron [bold]{cron}[/bold] UTC)\n\n"
        + (f"[yellow]{drift}[/yellow]\n\n" if drift else "")
        + "[bold yellow]Your API keys and your Gmail app password will be "
          "stored on GitHub.[/bold yellow] GitHub encrypts them and hides them "
          "in logs, but they do leave this computer.",
        title="Before anything is uploaded",
        border_style="yellow",
    ))
    if not Confirm.ask("Go ahead?", default=False):
        console.print("[dim]Stopped. Nothing left this computer.[/dim]")
        return

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
        console.print(Panel(f"[bold red]Could not create the repository.[/bold red]\n\n"
                            f"{out.strip()[:600]}", border_style="red"))
        return

    full_name = _existing_github_repo(project_root) or repo
    if not _upload_secrets(project_root, secrets, full_name):
        return

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

    if where == "computer":
        hour, minute = _ask_time() if not at else parse_time(at)
        _schedule_on_this_computer(hour, minute, project_root)
    else:
        # The time is asked further in: with a repository already set up,
        # the answer is usually "run it now" or "show me the runs", and
        # neither needs one.
        _schedule_on_github(project_root, preset=parse_time(at) if at else None)


def _update_source(config) -> str:
    return (getattr(getattr(config, "update", None), "source", "") or DEFAULT_UPSTREAM)


def _notice_if_out_of_date(config, project_root):
    """One quiet line after a run. Never applies anything, never fails a run.

    Checking automatically is fine; updating automatically is not. Code that
    replaces itself behind your back, on a machine that sends email as you, is
    not a convenience.
    """
    try:
        if not getattr(getattr(config, "update", None), "check_automatically", True):
            return
        newer, _, _ = update_available(project_root, _update_source(config))
        if newer:
            console.print(
                "\n[dim]There is a newer version of this app. "
                "Run [bold]email-workflow update[/bold] to get it - your "
                "settings and your data are left alone.[/dim]"
            )
    except Exception:
        pass


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Only look; change nothing"),
):
    """Get the newest version, keeping your settings, keys and history."""
    print_banner("UPDATE")

    project_root = resolve_project_file("config.yaml").parent
    config_path = resolve_project_file("config.yaml")
    config = AppConfig.load_from_file(config_path) if config_path.exists() else AppConfig()
    source = _update_source(config)

    with console.status("[cyan]checking...", spinner="dots"):
        newer, head, why = update_available(project_root, source)

    if not newer:
        console.print(f"[bold green]{why}[/bold green]")
        console.print(f"[dim]{source}[/dim]")
        return
    if check:
        console.print(f"[bold yellow]{why}[/bold yellow]")
        console.print("[dim]Run [bold]email-workflow update[/bold] to get it.[/dim]")
        return

    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix="email-workflow-update-"))
    new_tree = workspace / "new"
    try:
        with console.status("[cyan]downloading the new version...", spinner="dots"):
            ok, out = fetch_upstream(source, new_tree)
        if not ok:
            console.print(Panel(
                f"[bold red]Could not download the update.[/bold red]\n\n"
                f"{out.strip()[:400]}\n\n"
                f"[dim]Nothing on this computer was changed.[/dim]",
                border_style="red",
            ))
            return

        changes = changed_files(new_tree, project_root)
        if not changes:
            console.print("[bold green]The code here is already identical to "
                          "the newest version.[/bold green]")
            write_state(project_root, head, source)
            return

        subjects = recent_subjects(new_tree)
        if subjects:
            console.print(Panel(
                "\n".join(f"  - {line}" for line in subjects[:10]),
                title="What changed in the project",
                border_style="cyan",
            ))

        shown = changes[:20]
        more = len(changes) - len(shown)
        console.print(Panel(
            f"[bold]{len(changes)} file(s) would be replaced:[/bold]\n\n  "
            + "\n  ".join(shown)
            + (f"\n  [dim]...and {more} more[/dim]" if more else "")
            + "\n\n[bold green]These are NOT touched, whatever the update "
              "contains:[/bold green]\n  " + "\n  ".join(PROTECTED)
            + "\n\n[dim]Your settings, your keys, your knowledge base, your "
              "login and the record of what has already been handled all stay "
              "exactly as they are.[/dim]",
            title="Before anything is replaced",
            border_style="yellow",
        ))

        # An update replaces files. Work of your own that is not committed
        # would simply be gone, so it is named, and the default flips to no.
        yours = [f for f in locally_modified(project_root)
                 if any(f.replace(chr(92), "/") in line for line in changes)]
        if yours:
            console.print(Panel(
                "[bold red]You have changes here that are not committed, "
                "and the update would overwrite them:[/bold red]\n\n  "
                + "\n  ".join(yours) + "\n\n"
                "[dim]Commit or copy them somewhere first. This is your own "
                "work, not something the project can give back.[/dim]",
                title="Your edits would be lost",
                border_style="red",
            ))

        if not Confirm.ask("Update now?", default=not yours):
            console.print("[dim]Nothing changed.[/dim]")
            return

        # The workflow file is code and gets replaced, but the hour inside
        # it is a choice somebody made. Put it back afterwards.
        your_cron = keep_your_schedule(project_root)
        # Read before anything is replaced, so "did the dependencies change?"
        # can be answered afterwards without guessing.
        deps_before = _read_text(project_root / "pyproject.toml")
        with _progress() as bar:
            todo = files_to_copy(new_tree)
            job = bar.add_task("replacing files", total=len(todo))
            copied, problems = apply_update(
                new_tree, project_root,
                on_file=lambda rel: bar.update(
                    job, advance=1, description=rel.name[:28]
                ),
            )
            bar.update(job, description="done")
        if restore_your_schedule(project_root, your_cron):
            console.print("[dim]kept your daily run at the hour you chose[/dim]")
        for line in problems:
            console.print(f"[yellow]could not replace {line}[/yellow]")
        write_state(project_root, head, source)
        console.print(f"[dim]replaced {copied} file(s)[/dim]")

        # Only when the dependency list itself changed. This is an editable
        # install: the code that was just copied in IS the code that runs, so
        # a reinstall buys nothing unless pyproject.toml moved - and on Windows
        # it actively fails, because pip tries to rewrite email-workflow.exe
        # while that exe is the program asking for the update. That failure was
        # reported as "the new code is in place but reinstalling failed",
        # which reads like a broken update when nothing was wrong at all.
        if deps_before != _read_text(project_root / "pyproject.toml"):
            with console.status("[cyan]new dependencies, installing...",
                                spinner="dots"):
                ok, out = _shell([sys.executable, "-m", "pip", "install", "-e",
                                  ".", "--quiet"], cwd=project_root)
            if not ok and _only_the_launcher_was_locked(out):
                console.print(
                    "[dim]The launcher could not be rewritten because it is "
                    "the program you are running. It did not need "
                    "rewriting.[/dim]"
                )
            elif not ok:
                console.print(Panel(
                    f"[bold yellow]The new code is in place but its "
                    f"dependencies could not be installed.[/bold yellow]\n\n"
                    f"{out.strip()[:300]}\n\n"
                    f"Close the app and run this yourself:\n\n"
                    f"    pip install -e .",
                    border_style="yellow",
                ))

        # Does it actually start? Better to find out here than at 7am.
        ok, out = _shell([sys.executable, "-c",
                          "import email_workflow.cli.cli as c; print('ok')"],
                         cwd=project_root)
        if not ok or "ok" not in out:
            console.print(Panel(
                f"[bold red]The updated app does not start.[/bold red]\n\n"
                f"{out.strip()[:400]}\n\n"
                f"[dim]Your settings and data are untouched. Report this, or "
                f"reinstall with: pip install -e .[/dim]",
                border_style="red",
            ))
            return

        console.print(Panel(
            "[bold green]Updated, and it starts.[/bold green]\n\n"
            "[dim]Your settings, keys and history were not touched.[/dim]",
            border_style="green",
        ))

        # A scheduled run uses the copy on GitHub, not this one.
        if (project_root / ".git").exists():
            remote_ok, _ = _shell(["git", "remote", "get-url", "origin"],
                                  cwd=project_root)
            if remote_ok and Confirm.ask(
                "\nYour daily run uses the copy on GitHub. Push the update "
                "there too?", default=True,
            ):
                _shell(["git", "add", "-A"], cwd=project_root)
                _shell(["git", "commit", "-m", "Update the app"], cwd=project_root)
                ok, out = _shell(["git", "push", "origin", "HEAD"], cwd=project_root)
                console.print(
                    "[bold green]Pushed - the scheduled run uses the new "
                    "version from now on.[/bold green]" if ok else
                    f"[yellow]Could not push: {out.strip()[:200]}[/yellow]"
                )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
