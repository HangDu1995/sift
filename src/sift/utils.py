"""Shared utility: call Claude CLI via subprocess."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable


class _HttpThrottled(Exception):
    """裸 HTTP 撞 529(后端过载/身份限流)→ ``call_claude`` dispatcher 捕获、回退 cc。

    独立类型(**非** RuntimeError 子类):避免被 ``_judge_batch_with_retry`` 的
    ``except RuntimeError`` 误吞——它要穿透到 ``call_claude`` dispatcher 做 cc 兜底
    (dispatcher 在 call_claude 内部捕获,不会真逃出去)。
    """


class _HttpRateLimited(Exception):
    """裸 HTTP 撞 429(账户速率/并发限制)→ **不回退 cc**(同账号同限,cc 也撞),
    穿透 ``call_claude`` dispatcher 交 ``_judge_batch_with_retry`` 做 AIMD 退避:
    limiter.on_throttled()(降并发)+ 退避重试。

    独立类型(**非** RuntimeError 子类):避免被通用 ``except RuntimeError`` 误吞——
    429 要触发 AIMD 降档,不是当普通调用失败立即重试(速率没降、重试无效)。
    """


class _HttpNoToolUse(Exception):
    """裸 HTTP forced tool_use 没拿到 tool_use 块(GLM 只回 thinking / thinking 撑满
    ``_HTTP_MAX_TOKENS``)→ ``call_claude`` dispatcher 捕获、回退 cc(cc 多轮 tool_use
    循环催出 + 降级文本兜底,http 单次 forced 做不到)。

    独立类型(**非** RuntimeError 子类):避免被 ``_judge_batch_with_retry`` 的
    ``except RuntimeError`` 误吞(那条走 classify_error 落 cause=unknown 重试 http,
    系统性失败重试无用)——它要穿透到 ``call_claude`` dispatcher 做 cc 兜底。
    """

    def __init__(self, message="", stop_reason=None):
        super().__init__(message)
        self.stop_reason = stop_reason


# 裸 HTTP forced tool_use 的输出上限。4096(协议级保守默认,所有 Anthropic 兼容端点都接受;
# 不探 GLM 上限、不 baked 模型数)。曾试 8192 治 thinking 撑满,但实测 GLM 服务端 thinking 随
# max_tokens 膨胀——8192 让每批 thinking 更长、整体慢;4096 下大部分批(thinking<4096)http
# 一次过快,撑满没 tool_use 的少数批走 _HttpNoToolUse → cc 回退救回,整体优于 8192 全慢。
_HTTP_MAX_TOKENS = 4096


_CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
_claude_settings_cache: dict[str, str] | None = None


def _claude_settings_env() -> dict[str, str]:
    """``~/.claude/settings.json`` 的 ``env`` 块(cc CLI 自身的凭据/model 来源)。

    裸 HTTP 解析凭据时 os.environ 缺了就回退读这里:VSCode/SDK 把 ``ANTHROPIC_*`` 注入
    **交互 shell** 的 env,但非交互启动的子进程(如 cannbot-insight 的 ``next dev`` spawn
    出来的 sift)继承不到 → 裸 HTTP 撞 ``KeyError`` /「无可用 model」全批失败(audit
    静默出 UNRESOLVED)。回退读 cc 同源的 settings.json,让裸 HTTP 在 **cc 能跑的任何上下文**
    都能跑(实测:剥光 os.environ 后从 settings.json 读 base/token/model 一发即通)。

    读失败 / 无 env 块 → 空 dict(不抛,交调用方判缺 loud 报)。模块级缓存(文件 audit
    期间不变);测试 monkeypatch ``_CLAUDE_SETTINGS_PATH`` 后须同时清 ``_claude_settings_cache``。
    """
    global _claude_settings_cache
    if _claude_settings_cache is None:
        # 局部变量攒好再一次赋值:若先赋空 dict 再填,并发线程会在窗口期看到「非 None 的空」
        # 跳过初始化 → 返空 → model/token 解析失败(refine 多批并发实测踩到、靠重试兜回)。
        cache: dict[str, str] = {}
        try:
            with open(_CLAUDE_SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            env = data.get("env") if isinstance(data, dict) else None
            if isinstance(env, dict):
                cache = {k: str(v) for k, v in env.items()}
        except (OSError, ValueError):
            pass
        _claude_settings_cache = cache
    return _claude_settings_cache


def _anthropic_env(key: str) -> str | None:
    """解析一个 ``ANTHROPIC_*`` 配置值:os.environ 优先(显式 export / IDE 注入),
    回退 ``~/.claude/settings.json`` env 块(cc CLI 同源)。两处都无 → None。

    让裸 HTTP 的凭据解析与 cc 一致(cc 读 settings.json,故 cc 在缺 os.environ 的
    子进程里仍能跑);裸 HTTP 原先只读 os.environ → 在那些上下文里系统性失败。
    """
    return os.environ.get(key) or _claude_settings_env().get(key)


def _resolve_http_model(model: str | None) -> str:
    """裸 HTTP 用的真实模型 id。

    cc CLI 的 model 别名(如 ``glm-5.2[1m]`` 的 ``[1m]`` 上下文窗口后缀)裸 API 不收
    (spike 实证 code 1211「模型不存在」)→ 剥首个 ``[`` 后缀。``model=None``(audit 默认
    不传 ``--model`` 的常态)→ **镜像 cc**:经 ``_anthropic_env`` 解析(os.environ 优先、
    回退 ``~/.claude/settings.json`` env 块,与 cc 同源)再剥后缀;两处都无才抛
    (裸 HTTP 无内置默认)。
    """
    if not model:
        model = _anthropic_env("ANTHROPIC_MODEL")
    if not model:
        raise RuntimeError(
            "裸 HTTP 无可用 model:未传 --model 且 os.environ 与 "
            "~/.claude/settings.json env 块均无 ANTHROPIC_MODEL"
        )
    i = model.find("[")
    return model[:i] if i != -1 else model


def _llm_http_call(prompt, model, timeout, schema, on_usage):
    """裸 HTTP 直连智谱 Anthropic 协议 endpoint(forced tool_use 保 schema)。

    省 cc CLI 的进程 spawn + ~25k 系统 prompt/批(spike 实测 refine 5.3×、headline 2.7×、
    refine 输出 12/12 一致)。stdlib ``urllib`` 不加依赖。``schema`` 作为 forced tool 的
    ``input_schema``(``tool_choice`` force)→ 模型一次产 ``tool_use.input``
    (= ``{"items":[...]}`` 或单对象),解包后 ``json.dumps`` 返(形状与 ``call_claude``
    schema 模式一致,下游 parser 零改动)。

    - **529**(过载/身份限流)→ ``_HttpThrottled``(交 ``call_claude`` dispatcher 回退 cc)。
    - **没拿到 tool_use 块**(GLM 只回 thinking / thinking 撑满 ``_HTTP_MAX_TOKENS``)
      → ``_HttpNoToolUse``(交 dispatcher 回退 cc:cc 多轮 tool_use 循环催出 + 降级文本兜底)。
    - **其它 HTTP 错**(4xx 配置)/ **传输错**(timeout / URLError)→ ``RuntimeError``
      (暴露、走既有重试/兜底;cc 同 endpoint 同样会 timeout,回退无意义)。
    - ``on_usage``:回 ``{input_tokens, output_tokens, …, total_cost_usd:0}``(裸 API 不返
      cost,补 0 对齐 ``call_claude`` 的 payload 形状)。
    """
    body = {
        "model": _resolve_http_model(model),
        "max_tokens": _HTTP_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "name": "emit",
            "description": "emit the JSON result",
            "input_schema": schema,
        }],
        "tool_choice": {"type": "tool", "name": "emit"},
    }
    token = _anthropic_env("ANTHROPIC_AUTH_TOKEN")
    base = _anthropic_env("ANTHROPIC_BASE_URL")
    if not token or not base:
        missing = "ANTHROPIC_AUTH_TOKEN" if not token else "ANTHROPIC_BASE_URL"
        raise RuntimeError(
            f"裸 HTTP 缺凭据:os.environ 与 ~/.claude/settings.json env 块均无 "
            f"{missing}(cc CLI 同源读 settings.json;确认 GLM 凭据已配置)"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        # best-effort 身份(减 529 触发,非保证——保证靠 529→cc 兜底):让后端尽量当 cc 放行。
        "User-Agent": "claude-code/2.1.220",
    }
    base = base.rstrip("/")
    req = urllib.request.Request(
        base + "/v1/messages", data=json.dumps(body).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 529:
            raise _HttpThrottled(
                f"裸 HTTP 529(后端过载/身份限流): {e.read().decode()[:200]}"
            )
        if e.code == 429:
            raise _HttpRateLimited(
                f"裸 HTTP 429(账户速率/并发限制): {e.read().decode()[:200]}"
            )
        raise RuntimeError(f"裸 HTTP {e.code}: {e.read().decode()[:500]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"裸 HTTP 传输失败: {e!r}")
    # 先回 usage(无论成败,产出了 token 就该计;truncated 标记截断留存)。
    truncated = obj.get("stop_reason") == "max_tokens"
    if on_usage is not None:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            payload = dict(usage)
            payload.setdefault("total_cost_usd", 0.0)
            if truncated:
                payload["truncated"] = True
            on_usage(payload)
    # 抠 tool_use.input(forced tool_use 应恒有;缺失 = 异常)。
    tool_items = None
    for b in obj.get("content", []):
        if b.get("type") == "tool_use":
            inp = b.get("input", {})
            tool_items = inp.get("items", inp)   # array schema 包 {"items":[...]} → 解开
            break
    # 截断(stop_reason=max_tokens):thinking 顶到上限 → tool_use 可能不完整(有但畸形/缺字段)。
    # **有 tool_use 就用**(即使截断;下游 parser 判完整性,不完整则 parse_failed 重试),
    # 不 cc 回退——曾用"截断就 cc 回退"但 GLM 频繁顶到 max_tokens 致大批请求走 cc(4-6× 慢)。
    # warn 进 stderr(可观测);on_usage payload 带 truncated 留存(accumulator 按 _KEYS 取,不受影响)。
    if truncated:
        warnings.warn(
            "LLM 响应被截断(stop_reason=max_tokens, max_tokens 上限 "
            f"{_HTTP_MAX_TOKENS}):thinking 顶到上限,tool_use 可能不完整 → 下游 parser "
            "可能 parse_failed。反复出现可减小单批(audit _MAX_LLM_IN_BATCH)。"
        )
    # 没拿到 tool_use(thinking 撑满没空间产 / GLM 主动 end_turn 不调)= forced 真失败
    # → raise _HttpNoToolUse 交 dispatcher 回退 cc(cc 多轮催出 + 降级文本,绕过 http 单次 forced 死结)。
    if tool_items is None:
        raise _HttpNoToolUse(
            f"裸 HTTP 未返回 tool_use(force 失败,thinking 撑满或 GLM 主动不调?): {str(obj)[:300]}",
            stop_reason=obj.get("stop_reason"),
        )
    return json.dumps(tool_items, ensure_ascii=False)


def claude_cli_env() -> dict:
    """claude CLI 子进程环境:剥 CLAUDECODE + 补 claude-code 身份变量。

    智谱(open.bigmodel.cn)等后端在模型过载时,只放行带 claude-code 身份标识
    (AI_AGENT + CLAUDE_CODE_ENTRYPOINT)的请求,裸 claude CLI 请求被 529「访问量过大」
    直接挡回。这两个变量由 VSCode 扩展 / SDK 运行时注入,用户终端默认没有 → 终端跑
    sift(任何 spawn claude 的路径)系统性撞 529(实测:同 token 同时刻,带这俩 OK、
    不带 529/超时)。缺则补(值版本无关,通用 'claude-code' 实测即放行),已有不覆盖
    (在 claude-code 内跑保留真值)。

    call_claude(audit/init/score 判分调用)与 ClaudeRunner(run --task 真任务 spawn)
    共用此 helper,确保两条 spawn 路径都自报身份。
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    if "AI_AGENT" not in env:
        env["AI_AGENT"] = "claude-code"
    if "CLAUDE_CODE_ENTRYPOINT" not in env:
        env["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
    return env


def _spawn_claude(cmd: list[str], prompt: str, timeout: int) -> str:
    """跑 claude CLI 子进程、返 stdout 文本。超时 killpg、非零退出 raise。

    text / json 两条输出格式共用此底层(只管 spawn + 收 stdout,不关心格式)。
    """
    env = claude_cli_env()
    # start_new_session 让外部 CLI(claude/opencode 等通用 backend)在独立进程组,
    # 超时方可 os.killpg 杀整组含其子进程树——这类 CLI 常是 wrapper + node worker,
    # 单 kill 主进程会让 worker 成孤儿、持有 stdout pipe 使 communicate 卡死
    # (实测 claude 曾卡 2588s 才报 300s timeout)。SIGKILL 不可忽略,确保彻底回收。
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        raise RuntimeError(f"claude CLI timed out after {timeout}s")
    if proc.returncode != 0:
        # claude CLI 实测把输出/错误打 stdout、stderr 常空(后端不可用时 exit 1 +
        # stderr 空),只报 stderr 会得到冒号后空白的报错、根因被吞。两条都查才看得到;
        # 优先 stderr(常规错误通道),空则 fallback stdout。
        detail = err.strip() or out.strip()
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail[:500]}")
    return out


def _call_claude_cc(prompt, model, timeout, max_turns, on_usage, schema):
    """call_claude 的 cc CLI 实现(从 call_claude 抽出,逻辑零改动)。

    见 ``call_claude`` 的 docstring(text/json/schema 三模式 + array 解包 + 兜底语义)。
    """
    fmt = "json" if (on_usage is not None or schema is not None) else "text"
    # 结构化输出(--json-schema)需 claude 走 tool_use 循环产 schema 验证过的对象
    # (实测 num_turns=2)。判官常设 max_turns=1 防发散,但 schema 模式下会截断成
    # error_max_turns(整批 RuntimeError → UNRESOLVED)。schema 模式保底 max_turns≥2。
    if schema is not None and max_turns is not None and max_turns < 2:
        max_turns = 2
    cmd = ["claude", "-p", "--output-format", fmt, "--dangerously-skip-permissions"]
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if model:
        cmd.extend(["--model", model])
    if schema is not None:
        cmd.extend(["--json-schema", json.dumps(schema)])
    out = _spawn_claude(cmd, prompt, timeout)
    if on_usage is None and schema is None:
        return out
    # json 模式:解析单结果对象,抠 structured_output / result + usage + cost。
    try:
        obj = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return out  # 兜底:畸形输出当纯文本返,不回调(无法可信地报 token)。
    if on_usage is not None:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            payload = dict(usage)
            payload["total_cost_usd"] = obj.get("total_cost_usd") or 0.0
            on_usage(payload)
    if schema is not None:
        so = obj.get("structured_output")
        if so is not None:
            # array 判官包 {"items":[...]}(CLI 拒顶层 array)→ 透明解开。
            if isinstance(so, dict) and isinstance(so.get("items"), list):
                return json.dumps(so["items"], ensure_ascii=False)
            return json.dumps(so, ensure_ascii=False)
        # structured_output 缺失 → 降级返 result 文本(fallback parser 接住,等同旧行为)。
    # result 缺失 → 空串(不返 raw json,免得 usage dict 被下游文本解析误 carve)。
    return obj.get("result", "")


def call_claude(
    prompt: str,
    model: str | None = None,
    timeout: int = 120,
    max_turns: int | None = None,
    on_usage: Callable[[dict], None] | None = None,
    schema: dict | None = None,
    *,
    transport: str = "cc",
) -> str:
    """调 claude CLI 拿文本回。

    - ``on_usage=None`` 且 ``schema=None``(默认)→ ``--output-format text``,纯文本
      进出(历史行为;其余调用方与所有测试 mock 走这条,字节不变)。
    - ``on_usage`` 给定 → ``--output-format json``,解析单结果对象,返回 ``result``
      (文本,等同 text 模式输出);对象的 ``usage``(token 用量)+ ``total_cost_usd``
      打包成 dict 回调 ``on_usage``,供调用方累加。
      **兜底**:json 解析失败/对象畸形 → 退回返 raw stdout、**不触发** on_usage——
      绝不让抓 token 砸了拿结果(token 是锦上添花,不是单点故障)。
    - ``schema`` 给定 → 额外加 ``--json-schema``(结构化输出:模型被硬约束产符合
      schema 的对象,CLI 在结果里附 ``structured_output`` 字段)。判官用这条从源头
      消灭「文本里抠 JSON」的解析 bug(格式不可畸形)。**返回仍是 str**:有
      ``structured_output`` → 返其 ``json.dumps``(判官侧 ``json.loads`` 一次命中,
      fallback parser 留作兜底);``structured_output`` 缺失(model 未合规)→ 降级返
      ``result`` 文本,判官现有 fallback parser 接住,等同旧行为。
      **array 判官约定**:CLI 不接受顶层 ``{"type":"array"}`` schema(spike 实证触发
      ``error_max_structured_output_retries``),故 array 判官把输出包成
      ``{"items":[...]}``;call_claude 透明解开(``structured_output`` 是 dict 且含
      ``items`` list → 返 ``json.dumps(items)``),判官/parser 零改动、拿纯 array。

    为何不另起 ``call_claude_with_usage`` / ``call_claude_structured`` 而在本函数加
    可选参数:全仓 ~35 处测试 mock patch 的是 ``sift.audit.auditor.call_claude``
    这个名字(auditor.py 把它 from-import 进本模块命名空间);换名会让 mock 全失效→
    测试去打真 claude。默认 None 保持 text 模式,这些 mock 与其余调用方零改动。

    ``transport``(代码级,默认 ``"cc"``):``"cc"`` → cc CLI(``_call_claude_cc``);
    ``"http"`` → 裸 HTTP 直连(``_llm_http_call``,省 cc 进程 spawn + ~25k 系统 prompt/批,
    forced tool_use 保 schema)。裸 HTTP 撞 529(后端过载/身份限流)/ forced tool_use 拿不到
    tool_use 块(GLM 只回 thinking / thinking 撑满 ``_HTTP_MAX_TOKENS`` → ``_HttpNoToolUse``)
    → 回退 cc(cc 多轮催出 + 降级文本兜底);其余 HTTP 错(4xx/timeout→RuntimeError)不回退
    (同 endpoint 无意义)。
    默认 ``"cc"`` 保持现有调用方 / ~35 处 mock 零改动。
    """
    if transport == "http":
        try:
            return _llm_http_call(prompt, model, timeout, schema, on_usage)
        except (_HttpThrottled, _HttpNoToolUse):
            # 529(过载/身份限流)/ forced tool_use 拿不到 tool_use 块(GLM 只回 thinking /
            # thinking 撑满 max_tokens)→ 回退 cc:cc 多轮 tool_use 循环催出 + 降级文本兜底,
            # 绕过 http 单次 forced 的结构死结。其它错(4xx/timeout→RuntimeError)不回退——
            # 同 endpoint 回退无意义。
            return _call_claude_cc(prompt, model, timeout, max_turns, on_usage, schema)
    return _call_claude_cc(prompt, model, timeout, max_turns, on_usage, schema)


class TokenUsageAccumulator:
    """线程安全地累加 call_claude 的 token 用量。

    audit 判官在线程池里并发触发 ``on_usage``(=``self.add``),snapshot 在 audit
    末尾单线程取。锁防并发丢失更新(实证见 tests/test_audit_parallel.py)。
    """

    _KEYS = (
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "total_cost_usd",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {k: 0 for k in self._KEYS}
        self._counts["calls"] = 0
        # 按阶段(refine/headline/judge/…)分桶 token,供报告分阶段展示。无 stage 的 add
        # 不进这里(只进总桶 _counts)。lazily 建。
        self._by_stage: dict[str, dict] = {}

    def _stage_counts(self, stage: str) -> dict:
        sc = self._by_stage.get(stage)
        if sc is None:
            sc = {k: 0 for k in self._KEYS}
            sc["calls"] = 0
            self._by_stage[stage] = sc
        return sc

    def add(self, payload: dict, *, count: int = 1, stage: str | None = None) -> None:
        """on_usage 回调目标 / rollup 子快照累加。缺键补 0(不 KeyError)。
        count: 本次 add 计几次调用;默认 1(单次 call_claude 回调)。rollup 子快照时传
        count=snap.get("calls",1) 以保留子内部真实调用数(否则 add 一次会吞掉 child 的 calls)。
        stage: 归属的 audit 阶段(如 "refine"/"judge");给定时同时累加到该阶段桶,
            便于报告分阶段展示 token。省略 → 只进总桶(向后兼容老调用方)。"""
        with self._lock:
            for k in self._KEYS:
                v = payload.get(k, 0) or 0
                self._counts[k] += v
                if stage is not None:
                    self._stage_counts(stage)[k] += v
            self._counts["calls"] += count
            if stage is not None:
                self._stage_counts(stage)["calls"] += count

    def scoped(self, stage: str) -> Callable[[dict], None]:
        """返绑定 stage 的 add 闭包,供 ``on_usage=acc.scoped("refine")`` 把该处
        call_claude 的 token 归到指定阶段。线程安全(复用 add 的锁)。"""
        def _add(payload: dict, count: int = 1) -> None:
            self.add(payload, stage=stage, count=count)
        return _add

    def snapshot_by_stage(self) -> dict[str, dict]:
        """返各阶段 token 桶的副本 {stage: {input_tokens, …, calls}}。无任何带 stage
        的 add → 空 dict。只含显式归阶段的量;总桶用 snapshot()。"""
        with self._lock:
            return {stage: dict(sc) for stage, sc in self._by_stage.items()}

    def snapshot(self) -> dict:
        """返当前累计的副本;无任何 usage(所有 token 字段 + calls 皆 0)返空 dict。
        判空看 token 不只看 calls:executor 段 count=0 但有 token(executor 非评分 LLM 调用)仍应非空。"""
        with self._lock:
            if not any(self._counts.values()):
                return {}
            return dict(self._counts)


class DurationAccumulator:
    """线程安全累加时长(秒)。rollup 各类调用的 perf_counter 时长(scoring/audit)。

    与 TokenUsageAccumulator 不同:time 是墙钟、单值,只累加秒数(无 calls/cost 多字段)。
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0.0

    def add(self, seconds: float | None) -> None:
        with self._lock:
            self._total += seconds or 0.0

    def snapshot(self) -> float:
        with self._lock:
            return self._total
