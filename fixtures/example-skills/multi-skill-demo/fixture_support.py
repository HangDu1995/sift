import json
import logging


def output(message, stream):
    handler = logging.Handler()

    def emit(record):
        stream.write(record.getMessage() + "\n")

    handler.emit = emit
    handler.handle(logging.LogRecord(__name__, logging.INFO, __file__, 0, str(message), (), None))


def review_issue(severity, category, description):
    return {"line": 2, "severity": severity, "category": category, "description": description}


def review_report(path, issue, summary=None):
    payload = {"file": path}
    if summary is not None:
        payload["summary"] = summary
    payload["issues"] = [issue]
    return json.dumps(payload, ensure_ascii=False, indent=2)
