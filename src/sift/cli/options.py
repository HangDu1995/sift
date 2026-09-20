"""Dataclass option declarations using Typer's normal parameter conversion."""

from dataclasses import fields
from inspect import getdoc
from typing import get_type_hints

from typer.core import TyperCommand
from typer.main import get_click_param, get_command_name
from typer.models import ParamMeta


def _command_type(options_type, handler):
    annotations = get_type_hints(options_type)

    class OptionsCommand(TyperCommand):
        def __init__(self, **kwargs):
            params = []
            converters = {}
            for field in fields(options_type):
                parameter, converter = get_click_param(
                    ParamMeta(name=field.name, default=field.default, annotation=annotations[field.name])
                )
                params.append(parameter)
                if converter:
                    converters[field.name] = converter

            def invoke(**values):
                converted = {
                    key: converters[key](value) if key in converters else value for key, value in values.items()
                }
                return handler(options_type(**converted))

            super().__init__(**{**kwargs, "params": params, "callback": invoke})

    return OptionsCommand


def options_command(app, options_type, name=None, **metadata):
    """Register declared fields as normal Typer options, then pass one options value."""

    def register(handler):
        def entrypoint():
            """Parameter-free registration entry; OptionsCommand supplies the callback."""

        app.command(
            name or get_command_name(handler.__name__),
            cls=_command_type(options_type, handler),
            **{"help": getdoc(handler), **metadata},
        )(entrypoint)
        return handler

    return register
