"""Invoke the real CLI using isolated Click testing contexts."""

from sift.cli.main import app
from typer.testing import CliRunner


def invoke_cli(arguments, **settings):
    return CliRunner().invoke(app, arguments, **settings)
