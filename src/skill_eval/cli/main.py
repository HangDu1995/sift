import typer
from rich.console import Console

from skill_eval import __version__

app = typer.Typer(
    name="skill-eval",
    help="Automated evaluation tool for Claude Code Skills.",
    no_args_is_help=True,
)
console = Console()


def _version(value: bool):
    if value:
        console.print(f"skill-eval {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", callback=_version, is_eager=True, help="Show version."
    ),
):
    pass
