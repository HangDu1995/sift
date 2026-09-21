"""Plain user-visible output through a logging handler, independent of log filtering."""

import logging
import sys


class TextOutputHandler(logging.Handler):
    """Write plain text; callers own flushing and stream errors propagate normally."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def emit(self, record):
        self.stream.write(record.getMessage())


def write_text(text: str, *, stream) -> None:
    handler = TextOutputHandler(stream)
    record = logging.LogRecord("sift.output", logging.INFO, __file__, 0, text, (), None)
    handler.handle(record)


def print_text(text: str, *, file=None) -> None:
    stream = sys.stdout if file is None else file
    write_text(str(text), stream=stream)
    write_text("\n", stream=stream)
