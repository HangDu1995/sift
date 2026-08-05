# tests/test_http_throttle.py
"""裸 HTTP 通道限流异常分派:429 → _HttpRateLimited(透传做 AIMD)、529 → _HttpThrottled(回退 cc)。
"""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

from skill_eval import utils


def _http_err(code: int, body: bytes = b'{"error":{"message":"x"}}') -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    return urllib.error.HTTPError("http://x/v1/messages", code, "err", hdrs, io.BytesIO(body))


def _raise(err):
    def _f(*a, **k):
        raise err
    return _f


def test_rate_limited_not_runtime_error_subclass():
    """_HttpRateLimited 非 RuntimeError 子类(防被通用 except RuntimeError 误吞,要触发 AIMD)。"""
    assert not issubclass(utils._HttpRateLimited, RuntimeError)


def test_llm_http_call_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(429)))
    with pytest.raises(utils._HttpRateLimited):
        utils._llm_http_call("prompt", "m", 10, {"type": "object"}, None)


def test_llm_http_call_529_still_throttled(monkeypatch):
    """529 行为不变(回归):仍 _HttpThrottled 交 dispatcher 回退 cc。"""
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(529)))
    with pytest.raises(utils._HttpThrottled):
        utils._llm_http_call("prompt", "m", 10, {"type": "object"}, None)


def test_llm_http_call_other_code_still_runtime_error(monkeypatch):
    """其他 HTTP 错(如 400)仍 RuntimeError(暴露、走既有重试/兜底)。"""
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(400)))
    with pytest.raises(RuntimeError):
        utils._llm_http_call("prompt", "m", 10, {"type": "object"}, None)


def test_call_claude_http_rate_limited_not_falling_back(monkeypatch):
    """429 透传,不回退 cc(同账号同限,cc 也撞)。"""
    monkeypatch.setattr(utils, "_llm_http_call", _raise(utils._HttpRateLimited("429")))
    cc = []
    monkeypatch.setattr(utils, "_call_claude_cc", lambda *a, **k: cc.append(1) or "cc")
    with pytest.raises(utils._HttpRateLimited):
        utils.call_claude("prompt", model="m", transport="http")
    assert cc == []  # 没回退


def test_call_claude_http_throttled_falls_back_to_cc(monkeypatch):
    """529 回退 cc(dispatcher 既有行为,回归)。"""
    monkeypatch.setattr(utils, "_llm_http_call", _raise(utils._HttpThrottled("529")))
    monkeypatch.setattr(utils, "_call_claude_cc", lambda *a, **k: "cc-result")
    assert utils.call_claude("prompt", model="m", transport="http") == "cc-result"
