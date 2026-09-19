from typing import List, Dict, Any
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from email_workflow.models.email import DecisionOption

console = Console()

def print_banner(title: str):
    console.print(Panel(f"[bold cyan]{title}[/bold cyan]", border_style="cyan", expand=True))

def render_stage_result(step_num: int, result: Dict[str, Any]):
    analysis = result.get("analysis")
    decision: DecisionOption = result.get("decision")
    msg_id = result.get("message_id")
    summary = result.get("summary")

    # An email that was already handled comes back with no decision at all -
    # nothing was decided this time round. It still deserves a line, so it is
    # obvious why it was passed over.
    if result.get("status") == "skipped" or decision is None:
        stage = result.get("stage")
        stage_name = getattr(stage, "value", stage) or "unknown"
        table = Table(
            title=f"Stage Result #{step_num} - Message ID: {msg_id}",
            border_style="blue",
            show_header=True,
        )
        table.add_column("Property", style="bold")
        table.add_column("Value")
        table.add_row("Decision", "[blue]ALREADY HANDLED[/blue]")
        table.add_row("Reasoning", f"Handled in an earlier run (stage: {stage_name}).")
        table.add_row(
            "Summary Action",
            summary or "Skipped - no AI calls were made, so this cost nothing.",
        )
        console.print(table)
        return

    color = "yellow"
    if decision == DecisionOption.AUTOMATICALLY_REPLY:
        color = "green"
    elif decision == DecisionOption.ESCALATE:
        color = "red"
    elif decision == DecisionOption.ARCHIVE or decision == DecisionOption.IGNORE:
        color = "blue"

    table = Table(title=f"Stage Result #{step_num} - Message ID: {msg_id}", border_style=color, show_header=True)
    table.add_column("Property", style="bold")
    table.add_column("Value")

    if analysis:
        table.add_row("Subject", analysis.subject)
        table.add_row("Sender", f"{analysis.sender.name} <{analysis.sender.email}>")
        table.add_row("Category", f"[{color}]{analysis.category.value}[/{color}]")
        table.add_row("Importance / Urgency", f"{analysis.importance.value} / {analysis.urgency.value}")
        table.add_row("Confidence", f"{analysis.confidence:.2f}")
        if analysis.missing_information:
            table.add_row("Missing Info", f"[bold red]{', '.join(analysis.missing_information)}[/bold red]")
        if analysis.commitments_implied:
            table.add_row("Commitments Implied", ", ".join(analysis.commitments_implied))

    table.add_row("Decision", f"[{color}]{decision.value.upper()}[/{color}]")
    table.add_row("Summary Action", summary)

    console.print(table)

def render_providers_status(providers_status: Dict[str, Dict[str, str]]):
    table = Table(title="AI Provider Environment Status", border_style="cyan", show_header=True)
    table.add_column("Provider Key", style="bold yellow")
    table.add_column("Provider Name", style="bold white")
    table.add_column("Config Provider Value")
    table.add_column("Env Var Name")
    table.add_column("Status", style="bold")

    for key, info in providers_status.items():
        status_str = f"[bold green]set[/bold green]" if info["status"] == "set" else f"[bold red]missing[/bold red]"
        table.add_row(
            key,
            info["name"],
            key,
            info["env_var"],
            status_str,
        )

    console.print(table)

def render_audit_log(events: List[Dict[str, Any]], thread_id_filter: str = None):
    title = f"Audit Log (Thread: {thread_id_filter})" if thread_id_filter else "Audit Log"
    table = Table(title=title, border_style="magenta", show_header=True)
    table.add_column("Timestamp", style="dim")
    table.add_column("Event Type", style="bold cyan")
    table.add_column("Message ID")
    table.add_column("Thread ID")
    table.add_column("Detail")

    for e in events:
        table.add_row(
            e.get("timestamp", "")[:19].replace("T", " "),
            e.get("event_type", ""),
            e.get("message_id", ""),
            e.get("thread_id", ""),
            e.get("detail", ""),
        )

    console.print(table)

def render_thread_replay(thread_id: str, thread_state: Dict[str, Any], events: List[Dict[str, Any]]):
    tree = Tree(f"[bold cyan]Thread Replay: {thread_id}[/bold cyan] (Subject: {thread_state.get('canonical_subject', 'N/A')})")

    msgs_node = tree.add("[bold yellow]Messages Timeline[/bold yellow]")
    for m in thread_state.get("messages", []):
        status_color = "green" if m["status"] == "replied" else ("red" if m["status"] == "superseded" else "white")
        msgs_node.add(f"Message ID: [bold]{m['message_id']}[/bold] | Received: {m['received_at']} | Status: [{status_color}]{m['status']}[/{status_color}]")

    active_action = thread_state.get("active_action")
    if active_action:
        action_color = "yellow" if active_action["state"] == "in_progress" else ("green" if active_action["state"] == "completed" else "red")
        tree.add(f"[bold]Active Action:[/bold] {active_action['action_type']} on {active_action['target_message_id']} (State: [{action_color}]{active_action['state']}[/{action_color}])")

    events_node = tree.add("[bold magenta]State Transitions & Events[/bold magenta]")
    for e in events:
        events_node.add(f"[{e.get('timestamp', '')[:19]}] [bold]{e.get('event_type')}[/bold]: {e.get('detail')}")

    console.print(Panel(tree, border_style="cyan"))
