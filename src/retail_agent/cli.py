"""Command-line interface: `retail-agent chat --user alice`."""

from __future__ import annotations

import json
import logging
import warnings

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(add_completion=False, help="Retail data analysis assistant (LangGraph + Gemini + BigQuery).")
persona_app = typer.Typer(help="View or roll back the assistant persona (admins).")
app.add_typer(persona_app, name="persona")
console = Console()

HELP = """\
**Ask anything about sales, products, customers and orders**, e.g.
- *Who are our top 10 customers by spend?*
- *Compare Calvin Klein and Diesel - why do they perform differently?*
- *Create a Q1 report with insights and action items for Q2* (saved to your reports)
- *Delete all the reports we made in this conversation*

| Command | |
|---|---|
| `/reports [text]` | list your saved reports |
| `/report <id>` | show one report |
| `/undo` | restore the last deleted reports |
| `/prefs [key=value \\| clear]` | view/set preferences (format, depth, charts, language, focus, note) |
| `/persona [history \\| rollback <v>]` | view the persona; admins can roll back |
| `/trace [id]` | step-by-step trace of the last (or given) turn |
| `/stats [days]` | agent metrics |
| `/good`, `/bad [comment]` | rate the last answer |
| `/new` | start a new conversation |
| `/whoami`, `/help`, `/exit` | |
"""


def _quiet_logs() -> None:
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    for name in ("retail_agent", "google", "httpx", "langchain_google_genai"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _service():
    from .service import AgentService

    with console.status("Starting…"):
        return AgentService()


# ---------------------------------------------------------------------- rendering helpers
def render_trace(events: list[dict]) -> None:
    from .observability.render import offset_ms, summarize

    if not events:
        console.print("[yellow]No events for that trace.[/]")
        return
    table = Table(title=f"Trace {events[0]['trace_id']}  (session {events[0]['session_id']}, user {events[0]['user_id']})",
                  show_lines=False, expand=True)
    table.add_column("+ms", justify="right", style="dim", width=7)
    table.add_column("event", style="cyan", width=22)
    table.add_column("details", overflow="fold")
    for e in events:
        style = "red" if e["event"] in {"error", "llm_error", "tool_error", "guardrail_block", "llm_unavailable"} else ""
        table.add_row(str(offset_ms(events, e)), e["event"], summarize(e), style=style)
    console.print(table)


def render_metrics(m: dict) -> None:
    table = Table(title="Agent metrics", show_header=False)
    table.add_column(style="cyan")
    table.add_column()
    for k, v in m.items():
        table.add_row(k, json.dumps(v) if isinstance(v, dict) else str(v))
    console.print(table)


def render_reports(reports) -> None:
    if not reports:
        console.print("[dim]No saved reports.[/]")
        return
    table = Table(title="Your saved reports")
    for col in ("id", "title", "created (UTC)", "conversation"):
        table.add_column(col)
    for r in reports:
        table.add_row(str(r.id), r.title, r.created_at.replace("T", " ").rstrip("Z"), r.conversation_id)
    console.print(table)


def ask_confirmation(payload: dict) -> dict:
    """Human-in-the-loop UI for destructive actions."""
    table = Table(title=f"[bold red]Confirm deletion of {payload['count']} report(s)[/]", border_style="red")
    for col in ("id", "title", "created (UTC)", "preview"):
        table.add_column(col, overflow="fold")
    for r in payload["reports"]:
        table.add_row(str(r["id"]), r["title"], r["created_at"].replace("T", " ").rstrip("Z"), r["snippet"][:70])
    console.print(table)
    if payload.get("requires_typed_confirmation"):
        phrase = payload["expected_phrase"]
        typed = console.input(f"Type [bold]{phrase}[/] to confirm, or press Enter to cancel: ").strip()
        return {"confirmed": typed.lower() == phrase, "typed": typed, "token": payload["token"]}
    answer = console.input("Delete this report? [y/N]: ").strip().lower()
    return {"confirmed": answer in {"y", "yes"}, "token": payload["token"]}


def print_result(result) -> None:
    if result.text:
        border = {"ok": "green", "blocked": "yellow", "degraded": "yellow", "error": "red"}.get(result.status, "blue")
        console.print(Panel(Markdown(result.text), border_style=border, title="Retail Insights", title_align="left"))
    meta = f"trace {result.trace_id} · {result.latency_ms / 1000:.1f}s · {result.tokens:,} tokens · ~${result.cost_usd:.4f}"
    if result.status != "ok":
        meta += f" · {result.status}"
    if result.flags:
        meta += f" · {', '.join(result.flags)}"
    console.print(f"[dim]{meta}[/]")


# ---------------------------------------------------------------------- chat REPL
@app.command()
def chat(
    user: str = typer.Option(..., "--user", "-u", help="Who you are (see `retail-agent users`)."),
    session: str | None = typer.Option(None, "--session", "-s", help="Resume a conversation id."),
):
    """Interactive chat with the assistant."""
    _quiet_logs()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    from .safety.entitlements import UnknownUserError
    from .service import AccessDenied

    svc = _service()
    try:
        principal = svc.principal(user)
    except UnknownUserError:
        console.print(f"[red]Unknown user '{user}'.[/] Known users: {', '.join(p.user_id for p in svc.users())}")
        raise typer.Exit(1) from None
    session_id = session or svc.new_session()
    if session:
        for role, text in svc.history(session_id)[-6:]:
            console.print(f"[bold]{'You' if role == 'user' else 'Assistant'}:[/] {text[:300]}")
    console.print(Panel(
        f"Hi {principal.display_name}! You can analyse [bold]{principal.scope.describe()}[/].\n"
        f"Conversation [cyan]{session_id}[/] · type [bold]/help[/] for commands, [bold]/exit[/] to quit.",
        title="Retail Insights", border_style="cyan",
    ))
    prompt = PromptSession(history=FileHistory(str(svc.settings.data_dir / ".cli_history")))
    last_trace: str | None = None

    def run_with_spinner(fn, *args):
        with console.status("Thinking…", spinner="dots") as status:
            return fn(*args, progress=lambda msg: status.update(msg))

    while True:
        try:
            text = prompt.prompt(f"{user}> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("Bye!")
            return
        if not text:
            continue
        try:
            if text.startswith("/"):
                cmd, _, arg = text[1:].partition(" ")
                arg = arg.strip()
                if cmd in {"exit", "quit"}:
                    console.print("Bye!")
                    return
                if cmd == "help":
                    console.print(Markdown(HELP))
                elif cmd == "whoami":
                    console.print(f"{principal.display_name} ({principal.role}) · scope: {principal.scope.describe()} · "
                                  f"conversation {session_id}")
                elif cmd == "new":
                    session_id = svc.new_session()
                    console.print(f"New conversation [cyan]{session_id}[/]")
                elif cmd == "reports":
                    render_reports(svc.list_reports(user, arg or None))
                elif cmd == "report":
                    r = svc.get_report(user, int(arg)) if arg.isdigit() else None
                    console.print(Panel(Markdown(r.body_md), title=f"#{r.id} {r.title}") if r else "[yellow]Not found.[/]")
                elif cmd == "undo":
                    ids = svc.undo_delete(user, session_id)
                    console.print(f"[green]Restored reports {ids}.[/]" if ids else "[yellow]Nothing to undo.[/]")
                elif cmd == "prefs":
                    if arg == "clear":
                        svc.clear_preferences(user)
                    elif "=" in arg:
                        k, _, v = arg.partition("=")
                        svc.set_preference(user, k.strip(), v.strip())
                    console.print(svc.preferences(user) or "[dim]No preferences yet.[/]")
                elif cmd == "persona":
                    _persona_command(svc, user, arg)
                elif cmd == "trace":
                    tid = arg or last_trace or svc.last_trace_id(user, session_id)
                    render_trace(svc.trace(user, tid) if tid else [])
                elif cmd == "stats":
                    render_metrics(svc.metrics(user, int(arg) if arg.isdigit() else 7))
                elif cmd in {"good", "bad"}:
                    svc.feedback(user, session_id, last_trace, 1 if cmd == "good" else -1, arg)
                    console.print("[green]Thanks for the feedback![/]")
                else:
                    console.print(f"[yellow]Unknown command /{cmd}. Type /help.[/]")
                continue

            result = run_with_spinner(svc.chat, user, session_id, text)
            while result.status == "awaiting_confirmation":
                last_trace = result.trace_id
                decision = ask_confirmation(result.interrupt)
                result = run_with_spinner(svc.resume, user, session_id, decision)
            last_trace = result.trace_id
            print_result(result)
        except AccessDenied as e:
            console.print(f"[red]{e}[/]")
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted.[/]")
        except Exception as e:  # noqa: BLE001 - the REPL never dies
            console.print(f"[red]Unexpected error: {e}[/]")


def _persona_command(svc, user: str, arg: str) -> None:
    if arg.startswith("rollback"):
        version = arg.split()[-1]
        v = svc.rollback_persona(user, int(version))
        console.print(f"[green]Persona rolled back; active version is now v{v.id}.[/]")
        return
    if arg == "history":
        table = Table(title="Persona versions")
        for col in ("version", "active", "author", "created (UTC)", "note", "tone"):
            table.add_column(col, overflow="fold")
        for v in svc.persona_history():
            table.add_row(f"v{v.id}", "●" if v.is_active else "", v.author, v.created_at, v.note, v.persona.tone[:60])
        console.print(table)
        return
    v = svc.persona()
    console.print(Panel(v.persona.render(), title=f"Active persona v{v.id} (by {v.author})"))


# ---------------------------------------------------------------------- other commands
@app.command()
def users():
    """List prototype users and their data access."""
    _quiet_logs()
    from .config import get_settings
    from .safety.entitlements import Directory

    table = Table(title="Users")
    for col in ("user", "name", "role", "can analyse"):
        table.add_column(col)
    for p in Directory(get_settings().users_file).all():
        table.add_row(p.user_id, p.display_name, p.role, p.scope.describe())
    console.print(table)


@app.command()
def trace(trace_id: str, user: str = typer.Option("ceo", "--user", "-u")):
    """Show the step-by-step trace of one turn."""
    _quiet_logs()
    render_trace(_service().trace(user, trace_id))


@app.command()
def stats(days: int = 7, user: str = typer.Option("ceo", "--user", "-u")):
    """Show agent metrics computed from the event log."""
    _quiet_logs()
    render_metrics(_service().metrics(user, days))


@persona_app.command("show")
def persona_show():
    _quiet_logs()
    _persona_command(_service(), "ceo", "")


@persona_app.command("history")
def persona_history():
    _quiet_logs()
    _persona_command(_service(), "ceo", "history")


@app.command()
def doctor():
    """Check credentials and connectivity (BigQuery, Gemini, embeddings)."""
    _quiet_logs()
    from .config import get_settings

    s = get_settings()
    console.print(f"Project: [cyan]{s.gcp_project}[/] · Gemini via [cyan]{'Vertex AI (ADC)' if s.use_vertex else 'AI Studio key'}[/]"
                  f" · agent model [cyan]{s.agent_model}[/] (fallback {s.fallback_model})")
    ok = True
    try:
        from .data.bq_client import BigQueryRunner

        bq = BigQueryRunner(s.gcp_project, s.bq_dataset, s.bq_location)
        n = bq.dry_run(f"SELECT COUNT(*) FROM `{s.bq_dataset}.orders`")
        console.print(f"[green]✓ BigQuery[/] dry-run ok ({n} bytes)")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗ BigQuery:[/] {str(e)[:300]}")
    try:
        from .agent.llm import LLMGateway

        r = LLMGateway(s).primary.invoke("Reply with OK")
        console.print(f"[green]✓ Gemini[/] {s.agent_model}: {r.text[:20]!r}")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗ Gemini:[/] {str(e)[:300]}")
    try:
        from .golden import GoldenStore

        hits, mode = GoldenStore(s).search("monthly revenue trend")
        console.print(f"[green]✓ Golden playbook[/] {len(hits)} hit(s), retrieval={mode}")
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]! Golden playbook:[/] {str(e)[:200]}")
    raise typer.Exit(0 if ok else 1)


def main():  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
