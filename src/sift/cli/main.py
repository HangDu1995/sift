import typer
from rich.console import Console

from sift import __version__

app = typer.Typer(
    name="sift",
    help="评测 skill / agent 的能力 + 审计 skill / agent / 编排者的执行遵循性。",
    no_args_is_help=True,
)
console = Console()


def _version(value: bool):
    if value:
        console.print(f"sift {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", callback=_version, is_eager=True, help="Show version."),
):
    pass
