"""QualityJudge 抽象基类 — 所有质检维度的统一接口。

所有质检器（SafetyChecker、FactualityChecker、RelevanceChecker 等）
都继承自 QualityJudge，共享 LLM 调用、JSON 解析和错误处理逻辑。

设计决策：
- 使用 concurrent.futures.ThreadPoolExecutor 实现 LLM 调用的超时控制
  （因为 LLMRouter.chat() 是同步的，不能直接用 asyncio.wait_for）
- _parse_judge_response 包含多级 JSON 修复逻辑，处理 LLM 输出中的
  Markdown 代码块、尾随逗号、单引号等常见问题
- 错误处理遵循"优先返回结构化错误"原则，避免让异常扩散到上层
- QualityVerdict 兼容新旧接口，同时支持 dimension/details（新）和
  reasoning/metadata（旧）两种字段名

兼容性说明：
  本模块重写了 Task 5 版本，但保留了旧方法（_load_prompt、_render_messages、
  _call_llm、_parse_json_response、_build_verdict）作为向后兼容的包装。
  factuality.py 和 relevance.py 无需修改即可继续使用。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import Template
from typing import Any

import yaml

from src.api.schemas import QualityVerdict as _SchemaVerdict

logger = logging.getLogger(__name__)


# ── QualityVerdict 兼容类 ──────────────────────────────


class QualityVerdict(_SchemaVerdict):
    """质量评估结果，兼容新旧接口。

    同时接受两类字段：
    - 新接口: dimension, passed, score, details
    - 旧接口: passed, score, reasoning (映射到 details), metadata (额外数据)

    用法示例：
        # 新接口（SafetyChecker 使用）
        QualityVerdict(dimension="safety", passed=True, score=1.0, details="安全")

        # 旧接口（FactualityChecker/RelevanceChecker 使用）
        QualityVerdict(passed=True, score=1.0, reasoning="OK", metadata={...})
    """

    reasoning: str = ""
    metadata: dict[str, Any] = {}

    def __init__(self, **data: Any) -> None:  # type: ignore[override]
        """兼容新旧两种构造方式。

        当使用旧接口（传 reasoning 不传 details）时，自动将 reasoning 复制到 details。
        当使用旧接口传 metadata 时，保留该字段。
        """
        if "reasoning" in data and "details" not in data:
            data["details"] = data["reasoning"]
        if "dimension" not in data:
            data["dimension"] = ""
        super().__init__(**data)


# ── QualityJudge 抽象基类 ─────────────────────────────


class QualityJudge(ABC):
    """质检维度的抽象基类。

    每个质检维度（安全、事实性、相关性等）都继承此类并实现 evaluate() 方法。

    Attributes:
        llm_provider: LLM 路由器实例，需提供 chat() 方法
        prompt_template_name: 提示词模板名称（对应 prompts/quality/ 下的 YAML 键名）
        config: 配置字典
    """

    # ── 新接口（Task 8+） ──────────────────────────────

    def __init__(
        self,
        llm_provider: Any,
        prompt_template_name: str = "",
        config: dict | None = None,
    ):
        """初始化 QualityJudge。

        Args:
            llm_provider: 具有 chat() 方法的 LLM 实例（LLMRouter 或 MockLLMJudge）
            prompt_template_name: YAML 模板的键名（如 "safety_judge"）
            config: 配置字典，支持以下键：
                - quality_judge_provider: Judge 模型提供者（空字符串表示跟随默认）
                - quality_judge_model: Judge 模型名称
                - quality_judge_timeout_s: 单次 LLM 调用超时秒数（默认 10）
                - quality_fail_closed_for_safety: 安全维度是否 fail-closed（默认 True）
                - prompts_dir: 提示词模板目录（默认 "prompts"）
        """
        self.llm_provider = llm_provider
        self.prompt_template_name = prompt_template_name
        self.config = config or {}

        # 向后兼容（旧接口使用）
        self._prompt_dir = Path(self.config.get("prompts_dir", "prompts")) / "quality"
        self._judge_model = self.config.get("quality_judge_model")

    @abstractmethod
    def evaluate(
        self, query: str, answer: str, context: str | None = None
    ) -> QualityVerdict:
        """执行单个维度的质量评估。

        新接口使用 context 作为可选关键字参数（str | None），
        旧接口通过 **kwargs 传递 context（list[str] | None）。

        Args:
            query: 用户原始问题
            answer: 模型生成的回答
            context: 检索到的上下文（可选，用于事实性/相关性评估）

        Returns:
            QualityVerdict: 包含维度名、是否通过、得分和详细说明的评估结果
        """
        ...

    # ── LLM 调用（新） ──────────────────────────────────

    def _call_judge(self, prompt: str) -> dict:
        """调用 LLM 评判模型并返回解析后的 JSON 字典。

        使用 ThreadPoolExecutor 实现超时保护：
        1. 在独立线程中执行 LLM 调用
        2. 超过 quality_judge_timeout_s 秒则抛出 TimeoutError
        3. 超时或异常时返回兜底结果（含 _error=True 标识）

        Args:
            prompt: 完整的评判提示词

        Returns:
            dict: 解析后的评判结果（包含 passed/score/violations/reasoning 等字段）
                  异常时返回兜底 dict，包含 _error=True 标识
        """
        timeout = self.config.get("quality_judge_timeout_s", 10)
        executor = ThreadPoolExecutor(max_workers=1)

        try:
            future = executor.submit(self._do_llm_call, prompt)
            raw = future.result(timeout=timeout)
            return self._parse_judge_response(raw)
        except TimeoutError:
            logger.warning("LLM Judge 调用超时（%ds）", timeout)
            return self._build_fallback_result("timeout", f"LLM 评判超时（{timeout}s）")
        except Exception as exc:
            logger.error("LLM Judge 调用异常: %s", exc)
            return self._build_fallback_result("error", f"LLM 调用异常: {exc}")
        finally:
            executor.shutdown(wait=False)

    def _do_llm_call(self, prompt: str) -> str:
        """实际执行 LLM 调用（在子线程中运行）。

        Args:
            prompt: 评判提示词

        Returns:
            str: LLM 返回的原始文本
        """
        provider = self.config.get("quality_judge_provider", "")
        model = self.config.get("quality_judge_model", "")

        messages = [
            {
                "role": "system",
                "content": "你是一个专业的内容安全审查员。请严格按照用户要求的格式输出。",
            },
            {"role": "user", "content": prompt},
        ]

        # 仅当 provider/model 非空时才传入，确保与 MockLLMJudge 等兼容
        call_kwargs: dict[str, Any] = dict(
            messages=messages,
            temperature=0.0,
            max_tokens=2048,
        )
        if provider:
            call_kwargs["provider"] = provider
        if model:
            call_kwargs["model"] = model

        return self.llm_provider.chat(**call_kwargs)

    @staticmethod
    def _parse_judge_response(raw: str) -> dict:
        """解析 LLM 输出的 JSON 响应，包含多级修复逻辑。

        处理以下常见问题：
        1. LLM 在 JSON 前后添加了 Markdown 代码块标记（```json … ```）
        2. JSON 前后有多余的文本说明
        3. JSON 中包含尾随逗号（trailing commas）
        4. JSON 中使用单引号替代双引号
        5. 空响应或纯文本响应

        Args:
            raw: LLM 返回的原始文本

        Returns:
            dict: 解析后的评判结果；解析完全失败时返回兜底结果（含 _error=True）
        """
        if not raw or not raw.strip():
            logger.warning("LLM Judge 返回空响应")
            return {
                "passed": False,
                "score": 0.0,
                "violations": [],
                "reasoning": "[Judge Error] empty: LLM 返回空响应",
                "_error": True,
                "_error_type": "empty",
            }

        text = raw.strip()

        # 第1层：移除 Markdown 代码块标记（```json 和 ```）
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s*```\s*$', '', text, flags=re.MULTILINE)
        text = text.strip()

        # 第2层：提取 JSON 对象（从第一个 { 到最后一个 }）
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if not json_match:
            logger.warning("LLM Judge 响应中未找到 JSON 对象: %s", raw[:200])
            return {
                "passed": False,
                "score": 0.0,
                "violations": [],
                "reasoning": f"[Judge Error] no_json: 响应中未找到 JSON 对象: {raw[:200]}",
                "_error": True,
                "_error_type": "no_json",
            }

        json_str = json_match.group()

        # 第3层：尝试直接解析
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # 第4层：修复常见 JSON 格式问题后重试
        fixed = json_str

        # 移除尾随逗号（在 } 或 ] 之前的逗号）
        fixed = re.sub(r',\s*}', '}', fixed)
        fixed = re.sub(r',\s*]', ']', fixed)

        # 将单引号替换为双引号（简单处理：整体替换）
        if "'" in fixed:
            fixed = fixed.replace("'", '"')

        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.error("JSON 解析失败（修复后仍失败）: %s", json_str[:300])
            return {
                "passed": False,
                "score": 0.0,
                "violations": [],
                "reasoning": f"[Judge Error] parse_error: JSON 解析失败: {json_str[:200]}",
                "_error": True,
                "_error_type": "parse_error",
            }

    @staticmethod
    def _build_fallback_result(error_type: str, message: str) -> dict:
        """构建兜底评判结果（JSON 解析失败/超时时使用）。

        Args:
            error_type: 错误类型标识（如 "timeout"、"parse_error"）
            message: 错误描述

        Returns:
            dict: 兜底结果（passed=False, score=0.0, _error=True）
        """
        return {
            "passed": False,
            "score": 0.0,
            "violations": [],
            "reasoning": f"[Judge Error] {error_type}: {message}",
            "_error": True,
            "_error_type": error_type,
        }

    # ── 提示词模板（新） ────────────────────────────────

    def _render_prompt(self, template_name: str, **kwargs: Any) -> str:
        """从 YAML 文件加载提示词模板并渲染。

        模板文件位于 {prompts_dir}/quality/{template_name}.yaml
        使用 ``{{ variable }}`` 语法进行变量替换。

        Args:
            template_name: 模板键名（如 "safety_judge"），同时也是文件名（不含扩展名）
            **kwargs: 模板变量，如 question=..., answer=..., context=...

        Returns:
            str: 渲染后的提示词

        Raises:
            FileNotFoundError: 模板文件不存在
            ValueError: 模板内容无效
            KeyError: 模板中使用了未提供的变量
        """
        prompts_dir = self.config.get("prompts_dir", "prompts")
        path = f"{prompts_dir}/quality/{template_name}.yaml"

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"模板文件 {path} 为空或无效")

        # 尝试按 template_name 键取值
        template = data.get(template_name)
        if template is None:
            # 回退：取第一个字符串值
            for val in data.values():
                if isinstance(val, str):
                    template = val
                    break
        if not isinstance(template, str):
            raise ValueError(
                f"模板文件 {path} 中未找到有效的字符串模板（键 '{template_name}'）"
            )

        # 使用正则替换 ``{{ variable }}`` 为实际值
        def _replace_var(match: re.Match) -> str:
            var_name = match.group(1).strip()
            if var_name in kwargs:
                return str(kwargs[var_name])
            raise KeyError(f"模板变量 '{var_name}' 未提供")

        return re.sub(r'\{\{\s*(\w+)\s*\}\}', _replace_var, template)

    # ── 向后兼容方法（旧接口） ─────────────────────────

    def _load_prompt(self, template_name: str) -> str:
        """【兼容】从 YAML 文件加载 Prompt 模板字符串。

        旧接口方法，新代码请使用 _render_prompt()。
        仅返回模板内容（不渲染变量），与旧版 factuality.py / relevance.py 兼容。

        Args:
            template_name: YAML 文件名（如 "factuality_judge.yaml"）

        Returns:
            str: 模板内容

        Raises:
            FileNotFoundError: 模板文件不存在
            ValueError: YAML 格式不符合预期
        """
        path = self._prompt_dir / template_name
        if not path.exists():
            raise FileNotFoundError(f"Prompt 模板文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError(f"Prompt 模板格式错误（非 dict 或为空）: {path}")

        # 取 YAML 文件中的第一个值作为模板内容
        return str(list(data.values())[0])

    def _render_messages(self, template: str, **kwargs: Any) -> list[dict[str, str]]:
        """【兼容】渲染 Prompt 模板并构造 OpenAI 格式消息列表。

        旧接口方法，新代码不再需要（_render_prompt 直接返回渲染后的字符串）。

        Args:
            template: 含占位符的模板字符串（如 "问题：{{ question }}"）
            **kwargs: 模板变量键值对

        Returns:
            list[dict]: OpenAI 格式的 messages 列表
                [{"role": "system", "content": "..."},
                 {"role": "user", "content": "..."}]
        """
        # 将 {{ var }} 转换为 $var 格式（string.Template 兼容）
        content = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"$\1", template)
        # 安全替换：未提供的变量保持原样，不报错
        content = Template(content).safe_substitute(**kwargs)

        return [
            {
                "role": "system",
                "content": "你是一名专业的质量审查员，请严格按照要求进行评估。",
            },
            {"role": "user", "content": content},
        ]

    def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """【兼容】调用 LLM 获取评估结果。

        旧接口方法，直接调用 llm_provider.chat()，不带超时保护。
        新代码请使用 _call_judge()（含 ThreadPoolExecutor 超时保护）。

        Args:
            messages: OpenAI 格式的消息列表

        Returns:
            str: LLM 的文本响应

        Raises:
            TimeoutError: LLM 调用超时
            Exception: 其他 LLM 调用异常
        """
        return self.llm_provider.chat(
            messages,
            model=self._judge_model,
            temperature=0.0,
            max_tokens=4096,
        )

    def _parse_json_response(self, response_text: str) -> dict:
        """【兼容】从 LLM 响应文本中提取并解析 JSON。

        旧接口方法，新代码请使用 _parse_judge_response()（含更完善的修复逻辑）。
        此方法在解析失败时会抛出异常（与旧版行为一致）。

        Args:
            response_text: LLM 返回的原始文本

        Returns:
            dict: 解析后的 JSON 字典

        Raises:
            ValueError: 无法从响应中提取有效 JSON
            json.JSONDecodeError: JSON 格式不合法
        """
        # 委托给新方法，但将错误转为异常（保持旧接口行为）
        result = self._parse_judge_response(response_text)
        if result.get("_error"):
            raise ValueError(result.get("reasoning", "JSON 解析失败"))
        return result

    def _build_verdict(self, response: dict) -> QualityVerdict:
        """【兼容】将 LLM 返回的 JSON 字典转换为 QualityVerdict。

        Args:
            response: JSON 字典，应包含 passed、score、reasoning 等字段

        Returns:
            QualityVerdict: 结构化的评估结果
        """
        standard_fields = {"passed", "score", "reasoning", "dimension", "details"}
        metadata = {k: v for k, v in response.items() if k not in standard_fields}

        return QualityVerdict(
            passed=bool(response.get("passed", False)),
            score=float(response.get("score", 0.0)),
            reasoning=str(response.get("reasoning", "")),
            metadata=metadata,
        )
