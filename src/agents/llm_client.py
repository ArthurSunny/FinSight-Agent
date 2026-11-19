"""DeepSeek LLM 客户端封装（基于 openai SDK 的兼容接口）。

设计要点
--------
1. **不泄露密钥**：``__repr__`` / 日志只输出 ``已配置(len=N)``，任何异常信息都会先经过
   :func:`mask_secret` 清洗，杜绝密钥随 traceback 落到终端或报告里。
2. **严格 JSON**：DeepSeek 的 ``deepseek-chat`` 支持 ``response_format={"type": "json_object"}``；
   同时实现本地容错解析（剥离 ```json 围栏、括号配平提取），避免个别情况下被围栏污染。
3. **用量记账**：每次调用返回 :class:`LLMResult`，含 prompt/completion tokens 与耗时，
   供最终报告输出 Token 概览。
4. **离线模式**：``FINSIGHT_LLM_MODE=offline`` 时使用确定性模板响应，
   让整张图（含条件边重试、质检、落盘）在没有密钥的环境下也能完整跑通。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 密钥处理
# ---------------------------------------------------------------------------
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{6,})")


def mask_secret(text: str) -> str:
    """把任何形如 ``sk-xxxx`` 的字符串替换为 ``sk-****``（用于异常信息清洗）。"""
    return _SECRET_PATTERN.sub("sk-****", str(text))


@dataclass(frozen=True)
class LLMConfig:
    """LLM 连接配置（密钥仅存于内存，不参与任何序列化）。"""

    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.3
    #: 分析节点输出上限。实测：Beneish 8 变量 + 6 条规则 + 9 条 footnote_queries 的 JSON
    #: 会超过 4096 tokens 并被硬截断（导致 JSON 解析失败、Phase 3 接口丢失），故统一放宽。
    max_tokens: int = 8192
    #: 首席撰稿节点的输出上限（六章节 Markdown 通常 5000~8000 tokens）。
    editor_max_tokens: int = 8192
    timeout_s: float = 120.0
    mode: str = "api"

    @property
    def key_status(self) -> str:
        """脱敏后的密钥状态描述，可安全打印。"""
        if not self.api_key:
            return "未配置"
        return f"已配置(len={len(self.api_key)})"

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量 / 项目根目录 .env 读取配置。

        显式先加载 ``<项目根>/.env``（不依赖 CWD，也不依赖 dotenv 的向上递归查找），
        再退回 ``load_dotenv()`` 的默认行为，避免"在子目录启动脚本时读不到密钥"。
        """
        from src.config import PROJECT_ROOT

        root_env = PROJECT_ROOT / ".env"
        if root_env.exists():
            load_dotenv(root_env, override=False)
        load_dotenv(override=False)

        def _get(name: str, default: str = "") -> str:
            """读取并清洗：去掉首尾空白与误加的成对引号。"""
            value = (os.getenv(name) or default).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
            return value

        mode = _get("FINSIGHT_LLM_MODE", "api").lower()
        return cls(
            api_key=_get("DEEPSEEK_API_KEY"),
            base_url=_get("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com",
            model=_get("DEEPSEEK_MODEL", "deepseek-chat") or "deepseek-chat",
            temperature=float(_get("FINSIGHT_LLM_TEMPERATURE", "0.3")),
            max_tokens=int(_get("FINSIGHT_LLM_MAX_TOKENS", "8192")),
            editor_max_tokens=int(_get("FINSIGHT_EDITOR_MAX_TOKENS", "8192")),
            mode=mode,
        )


class LLMError(RuntimeError):
    """LLM 调用或解析失败。"""


class LLMConfigError(LLMError):
    """缺少密钥等配置问题。"""


@dataclass
class LLMResult:
    """单次调用结果（含用量与耗时）。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    latency_s: float = 0.0
    mode: str = "api"
    #: 结束原因：``stop`` 正常结束；``length`` 表示被 max_tokens 截断（必须显式诊断）
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def usage_record(self, tag: str) -> dict[str, Any]:
        return {
            "tag": tag,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "latency_s": round(self.latency_s, 3),
            "mode": self.mode,
            "finish_reason": self.finish_reason,
        }


# ---------------------------------------------------------------------------
# JSON 容错解析
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_loose(text: str) -> dict[str, Any]:
    """从模型输出中稳健提取 JSON 对象。

    依次尝试：直接解析 → 剥离 ``` 围栏 → 括号配平截取第一个完整对象。
    """
    candidate = (text or "").strip()
    if not candidate:
        raise LLMError("模型返回为空，无法解析 JSON")

    for attempt in (candidate, *_FENCE_RE.findall(candidate)):
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    start = candidate.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    snippet = candidate[start : index + 1]
                    try:
                        parsed = json.loads(snippet)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"无法从模型输出中解析出 JSON（前 200 字符：{candidate[:200]!r}）")


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class DeepSeekClient:
    """DeepSeek 对话客户端（OpenAI 兼容协议）。"""

    def __init__(self, config: Optional[LLMConfig] = None, *, force_mode: Optional[str] = None) -> None:
        self.config = config or LLMConfig.from_env()
        self.mode = (force_mode or self.config.mode or "api").lower()
        self._client: Any = None
        self.call_count = 0

    # -- 状态 ---------------------------------------------------------------
    @property
    def available(self) -> bool:
        """是否可走真实 API。"""
        return self.mode != "offline" and bool(self.config.api_key)

    def status_text(self) -> str:
        """可安全打印的状态描述（不含密钥）。"""
        return (
            f"mode={self.mode} model={self.config.model} "
            f"base_url={self.config.base_url} api_key={self.config.key_status}"
        )

    def __repr__(self) -> str:  # 防止误打印密钥
        return f"DeepSeekClient({self.status_text()})"

    # -- 内部 ---------------------------------------------------------------
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.api_key:
            raise LLMConfigError(
                "未检测到 DEEPSEEK_API_KEY。请在项目根目录 .env 中配置：DEEPSEEK_API_KEY=sk-...；"
                "或使用 FINSIGHT_LLM_MODE=offline 运行确定性离线模式。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise LLMError("缺少 openai 依赖，请执行：pip install openai") from exc

        self._client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
            max_retries=2,
        )
        return self._client

    # -- 调用 ---------------------------------------------------------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        tag: str = "chat",
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """单轮对话调用。"""
        if not self.available:
            return self._offline_response(tag, system, user, json_mode)

        client = self._ensure_client()
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一包装并脱敏
            raise LLMError(mask_secret(f"DeepSeek 调用失败（tag={tag}）：{type(exc).__name__}: {exc}")) from exc

        latency = time.perf_counter() - started
        self.call_count += 1

        text = (response.choices[0].message.content or "").strip()
        finish_reason = str(getattr(response.choices[0], "finish_reason", "") or "")
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
        cached = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)

        return LLMResult(
            text=text,
            model=self.config.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached,
            latency_s=latency,
            mode="api",
            finish_reason=finish_reason,
        )

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        tag: str = "json",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        """要求模型返回 JSON 对象并解析。"""
        result = self.chat(
            system,
            user,
            tag=tag,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            payload = parse_json_loose(result.text)
        except LLMError:
            if self.available:
                # 仅真实 API 才值得再花一次调用做修复；离线模式直接抛出
                repair = self.chat(
                    "你是严格的 JSON 修复器。只输出修复后的 JSON 对象本身，不要任何解释、不要代码围栏。",
                    f"以下内容应是合法 JSON 对象但解析失败，请修复并输出：\n<<<\n{result.text}\n>>>",
                    tag=f"{tag}:repair",
                    json_mode=True,
                    temperature=0.0,
                )
                result.prompt_tokens += repair.prompt_tokens
                result.completion_tokens += repair.completion_tokens
                result.total_tokens += repair.total_tokens
                result.latency_s += repair.latency_s
                payload = parse_json_loose(repair.text)
            else:
                raise
        return payload, result

    # -- 离线模式 -----------------------------------------------------------
    def _offline_response(self, tag: str, system: str, user: str, json_mode: bool) -> LLMResult:
        """确定性模板响应：让整张图在没有密钥时也能完整执行。"""
        from src.agents.offline_templates import build_offline_payload

        started = time.perf_counter()
        if json_mode:
            payload = build_offline_payload(tag, user)
            text = json.dumps(payload, ensure_ascii=False)
        else:
            text = build_offline_payload(f"{tag}:markdown", user).get("markdown", "")
        latency = time.perf_counter() - started
        self.call_count += 1
        return LLMResult(
            text=text,
            model="offline-template",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_s=latency,
            mode="offline",
        )
