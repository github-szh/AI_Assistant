#!/usr/bin/env python3
"""离线评测脚本 — 批量跑分 + Markdown 报告生成。

对 eval_dataset.json 中的每条 QA 执行全流程质量评估（QualityGuard），
汇总所有维度的得分，生成 Markdown 格式的评测报告。

默认以 Mock 模式运行（不依赖真实 LLM API），
使用 MockLLMJudge 模拟各维度检察器的 LLM 评判响应。

用法:
    # 基本用法（Mock 模式，使用默认数据集）
    python scripts/run_eval.py

    # 指定数据集路径
    python scripts/run_eval.py --dataset tests/test_data/eval_dataset.json

    # 指定输出目录
    python scripts/run_eval.py --output docs

    # 对比上一次评测报告
    python scripts/run_eval.py --compare docs/eval_report_20260601.md

    # 仅评估指定维度
    python scripts/run_eval.py --dimensions safety,factuality

    # 详细输出
    python scripts/run_eval.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

# ── 将项目根目录加入 sys.path（确保 import 能找到 src） ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ──────────── 第三方依赖 ────────────
try:
    from tqdm import tqdm
except ImportError:
    # tqdm 为可选依赖，缺失时使用简单回退
    tqdm = None

# ──────────── 项目内部依赖 ────────────
from src.api.schemas import InterventionInfo, QualityVerdict, SourceInfo
from src.quality.guard import QualityGuard
from src.quality.intervention import InterventionEngine

# ──────────── 日志配置 ────────────
logger = logging.getLogger("run_eval")


# ════════════════════════════════════════════════════════════════
# MockLLMJudge — 模拟 LLM Judge（内联版本，不依赖 tests/conftest）
# ════════════════════════════════════════════════════════════════


class MockLLMJudge:
    """模拟 LLM Judge，根据 Prompt 中 [EVALUATION_TASK] 标记返回预设 JSON。

    支持三种模式：
    - normal（默认）: 按任务类型返回结构化 JSON
    - timeout: 模拟 LLM 调用超时
    - malformed: 模拟 LLM 返回非 JSON 内容
    """

    def __init__(self, mode: str = "normal"):
        self.mode = mode
        self.last_prompt = ""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = False,
        **kwargs: Any,
    ) -> str:
        self.last_prompt = messages[-1]["content"] if messages else ""

        if self.mode == "timeout":
            raise TimeoutError("MockLLMJudge 模拟 LLM 调用超时")
        if self.mode == "malformed":
            return "抱歉，我无法理解您的请求，请重新输入。"

        task = self._parse_task(self.last_prompt)
        return json.dumps(self._build_response(task), ensure_ascii=False)

    def _parse_task(self, prompt: str) -> str:
        for task in ["safety", "factuality", "relevance"]:
            marker = f"[EVALUATION_TASK] {task}"
            marker2 = f"[EVALUATION_TASK]{task}"
            if marker in prompt or marker2 in prompt:
                return task
        return "unknown"

    def _build_response(self, task: str) -> dict:
        responses = {
            "safety": {
                "passed": True,
                "score": 0.95,
                "violations": [],
                "reasoning": "内容安全，无违规内容",
            },
            "factuality": {
                "passed": True,
                "score": 0.90,
                "hallucinations": [],
                "reasoning": "事实一致，回答基于检索内容",
            },
            "relevance": {
                "passed": True,
                "score": 0.85,
                "reasoning": "回答与查询相关，覆盖了核心问题",
            },
            "unknown": {
                "passed": True,
                "score": 0.50,
                "reasoning": "未知评估任务类型，默认通过",
            },
        }
        return responses.get(task, responses["unknown"])


# ════════════════════════════════════════════════════════════════
# 配置对象 — 模拟 Settings（仅含 QualityGuard 所需字段）
# ════════════════════════════════════════════════════════════════


class EvalSettings:
    """模拟 Settings，仅包含 QualityGuard 所需的最小配置字段。"""

    quality_guard_enabled: bool = True
    quality_parallel_eval: bool = False  # 串行模式确保 verdict 顺序可预测（旧接口检察器未设 dimension）
    quality_judge_timeout_s: int = 10
    quality_fail_closed_for_safety: bool = True
    quality_fail_open_for_others: bool = True
    retrieval_stage1_threshold: float = 0.65


# ════════════════════════════════════════════════════════════════
# 评测数据集加载
# ════════════════════════════════════════════════════════════════


def load_dataset(dataset_path: str) -> list[dict]:
    """加载评测数据集 JSON 文件。

    Args:
        dataset_path: JSON 文件路径。

    Returns:
        list[dict]: 数据集中的 QA 条目列表。文件不存在或 dataset 为空时返回空列表。
    """
    path = Path(dataset_path)
    if not path.exists():
        logger.warning("数据集文件不存在: %s", dataset_path)
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("dataset", [])
    logger.info("加载数据集: %s (%d 条)", path.name, len(items))
    return items


def validate_item(item: dict) -> str | None:
    """验证单条 QA 数据的完整性，返回错误信息或 None。

    必填字段: id, question（或 query）
    可选字段: answer, reference_context, context, sources

    数据集格式兼容两种规范：
    - 新格式: question + reference_context（无预生成 answer）
    - 旧格式: query + answer + context
    """
    if not isinstance(item.get("id"), str):
        return "缺少 id 字段"
    # 支持 question 和 query 两种字段名
    has_query = isinstance(item.get("query"), str) and item["query"].strip()
    has_question = isinstance(item.get("question"), str) and item["question"].strip()
    if not has_query and not has_question:
        return "缺少 question/query 字段"
    return None


# ════════════════════════════════════════════════════════════════
# 构建 QualityGuard（Mock 模式）
# ════════════════════════════════════════════════════════════════


def build_mock_guard(
    config: EvalSettings | None = None,
) -> QualityGuard:
    """构建 Mock 模式的 QualityGuard。

    所有 LLM 评判维度使用 MockLLMJudge 模拟，不调用真实 API。
    RetrievalQualityChecker 为纯数值计算，正常执行。

    Args:
        config: 评测配置对象。为 None 时使用默认配置。

    Returns:
        QualityGuard: 配置好的质检编排器实例。
    """
    if config is None:
        config = EvalSettings()

    # ── Mock LLM Judge ──
    mock_llm = MockLLMJudge(mode="normal")

    # ── 各维度检察器配置 ──
    checker_config: dict[str, Any] = {
        "quality_judge_provider": "",
        "quality_judge_timeout_s": config.quality_judge_timeout_s,
        "quality_fail_closed_for_safety": config.quality_fail_closed_for_safety,
        "quality_fail_open_for_others": config.quality_fail_open_for_others,
        "prompts_dir": "prompts",
    }

    # ── 使用真实检察器类，接入 Mock LLM ──
    from src.quality.safety import SafetyChecker
    from src.quality.factuality import FactualityChecker
    from src.quality.relevance import RelevanceChecker

    checkers: dict[str, Any] = {
        "safety": SafetyChecker(
            llm_provider=mock_llm,
            config=checker_config,
        ),
        "factuality": FactualityChecker(
            llm_provider=mock_llm,
            config=checker_config,
        ),
        "relevance": RelevanceChecker(
            llm_provider=mock_llm,
            prompt_dir="prompts/quality",
        ),
    }

    # ── 干预引擎（使用默认规则） ──
    intervention = InterventionEngine()

    return QualityGuard(
        checkers=checkers,
        intervention=intervention,
        config=config,
    )


# ════════════════════════════════════════════════════════════════
# 单条 QA 评测执行
# ════════════════════════════════════════════════════════════════


def _extract_field(item: dict, *keys: str) -> str:
    """从字典中按多个候选键依次取值，返回第一个非空字符串值。

    Args:
        item: 数据字典。
        keys: 候选键名列表，优先级从高到低。

    Returns:
        str: 提取的字符串值，均未找到时返回空字符串。
    """
    for k in keys:
        val = item.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def evaluate_single(
    guard: QualityGuard,
    item: dict,
    dimensions: set[str] | None,
) -> dict[str, Any]:
    """对单条 QA 执行全流程质量评估。

    执行流程：
    1. 从 item 中提取 query / answer / context / sources
       - 支持 question/query 两种字段名
       - 支持 reference_context/context 两种字段名
       - 无预生成 answer 时自动从 context 截取生成
    2. 调用 QualityGuard.run() 执行全维度评估与干预
    3. 收集各维度的 QualityVerdict（修正旧接口检察器未设置 dimension 的问题）
    4. 返回结构化的评测结果

    Args:
        guard: QualityGuard 实例。
        item: 数据条目字典，包含 question/query/answer/context 等字段。
        dimensions: 需要评估的维度集合。为 None 时评估全部 4 个维度。

    Returns:
        dict: 包含 id、各维度 verdict、干预信息、耗时等字段的评测结果。
              异常时返回带 error 字段的结果。
    """
    item_id = item.get("id", "unknown")
    query = _extract_field(item, "question", "query")
    context = _extract_field(item, "reference_context", "context")
    # 无预生成 answer 时，从 context 截取前 300 字作为 mock answer
    answer = _extract_field(item, "answer")
    if not answer and context:
        answer = context[:300]
    elif not answer:
        answer = f"关于「{query[:50]}」的问题，根据相关资料..."
    raw_sources = item.get("sources", [])

    # ── 构造 SourceInfo 列表 ──
    sources: list[SourceInfo] = []
    for s in raw_sources:
        if isinstance(s, dict):
            sources.append(SourceInfo(
                doc_id=s.get("doc_id", ""),
                filename=s.get("filename", ""),
                score=s.get("score"),
                snippet=s.get("snippet", ""),
            ))
        elif isinstance(s, SourceInfo):
            sources.append(s)

    t_start = time.time()

    # ── 全维度评估 ──
    try:
        modified_response, intervention = guard.run(
            query=query,
            answer=answer,
            context=context,
            sources=sources,
        )
        elapsed = time.time() - t_start
    except Exception as exc:
        elapsed = time.time() - t_start
        logger.error("条目 %s 评测异常: %s", item_id, exc)
        return {
            "id": item_id,
            "error": str(exc),
            "elapsed": round(elapsed, 3),
            "query": query,
            "answer": answer,
        }

    # ── 提取各维度 verdict ──
    verdicts_raw: list[QualityVerdict] = []
    if isinstance(intervention, InterventionInfo):
        verdicts_raw = intervention.violations
    elif isinstance(intervention, dict):
        verdicts_raw = intervention.get("violations", [])

    # QualityGuard 中 LLM 评判维度的顺序（safety/factuality/relevance）
    # 旧接口的 FactualityChecker/RelevanceChecker 的 QualityVerdict 不设 dimension 字段，
    # 因此需要按 verdict 列表中的出现顺序将空 dimension 映射到正确的维度名。
    _LLM_DIM_ORDER = list(guard.checkers.keys())  # ["safety", "factuality", "relevance"]
    llm_idx = 0

    # 按维度名称索引，同时处理 Pydantic 模型和 dict 两种格式
    verdict_map: dict[str, dict] = {}
    for v in verdicts_raw:
        if isinstance(v, QualityVerdict):
            dim = v.dimension or ""
            if not dim and llm_idx < len(_LLM_DIM_ORDER):
                # 旧接口：dimension 缺失，按顺序分配
                dim = _LLM_DIM_ORDER[llm_idx]
                llm_idx += 1
            elif dim:
                # 已设置 dimension（如 safety, retrieval_quality），跳过已占用
                if dim in _LLM_DIM_ORDER:
                    llm_idx = max(llm_idx, _LLM_DIM_ORDER.index(dim) + 1)
            if not dim:
                dim = "unknown"
            verdict_map[dim] = {
                "dimension": dim,
                "passed": v.passed,
                "score": v.score,
                "details": v.details or "",
            }
        elif isinstance(v, dict):
            dim = v.get("dimension", "") or ""
            if not dim and llm_idx < len(_LLM_DIM_ORDER):
                dim = _LLM_DIM_ORDER[llm_idx]
                llm_idx += 1
            elif dim:
                if dim in _LLM_DIM_ORDER:
                    llm_idx = max(llm_idx, _LLM_DIM_ORDER.index(dim) + 1)
            if not dim:
                dim = "unknown"
            verdict_map[dim] = {
                "dimension": dim,
                "passed": v.get("passed", True),
                "score": v.get("score", 0.0),
                "details": v.get("details", "") or v.get("reasoning", ""),
            }

    # ── 提取干预信息 ──
    if isinstance(intervention, InterventionInfo):
        intervention_info = {
            "intervened": intervention.intervened,
            "action": intervention.action,
            "reason": intervention.reason,
        }
    elif isinstance(intervention, dict):
        intervention_info = {
            "intervened": intervention.get("intervened", False),
            "action": intervention.get("action", "none"),
            "reason": intervention.get("reason", ""),
        }
    else:
        intervention_info = {"intervened": False, "action": "none", "reason": ""}

    # ── 按 dimensions 参数过滤 ──
    filtered_verdicts = verdict_map
    if dimensions is not None:
        filtered_verdicts = {
            k: v for k, v in verdict_map.items() if k in dimensions
        }

    return {
        "id": item_id,
        "elapsed": round(elapsed, 3),
        "query": query,
        "answer": answer,
        "verdicts": filtered_verdicts,
        "intervention": intervention_info,
    }


# ════════════════════════════════════════════════════════════════
# 批量评测
# ════════════════════════════════════════════════════════════════


def run_evaluation(
    dataset: list[dict],
    guard: QualityGuard,
    dimensions: set[str] | None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """对数据集中所有 QA 条目执行批量评测。

    Args:
        dataset: QA 条目列表。
        guard: QualityGuard 实例。
        dimensions: 需评估的维度集合。None 表示全部维度。
        verbose: 是否输出详细进度信息。

    Returns:
        list[dict]: 每条 QA 的评测结果。异常条目也包含在内（含 error 字段）。
    """
    results: list[dict] = []
    total = len(dataset)

    if total == 0:
        logger.warning("数据集为空，跳过评测")
        return results

    # ── 进度显示 ──
    iterator: Any
    if tqdm is not None and not verbose:
        iterator = tqdm(dataset, desc="评测进度", unit="条")
    else:
        iterator = dataset

    for i, item in enumerate(iterator):
        # 验证数据完整性
        validation_error = validate_item(item)
        if validation_error:
            item_id = item.get("id", f"item-{i}")
            logger.warning("条目 %s 数据不完整: %s", item_id, validation_error)
            results.append({
                "id": item_id,
                "error": f"数据不完整: {validation_error}",
                "query": item.get("query", ""),
                "answer": item.get("answer", ""),
            })
            if verbose:
                print(f"  [SKIP] {item_id}: {validation_error}")
            continue

        # 执行评测（错误不阻断整体流程）
        result = evaluate_single(guard, item, dimensions)
        results.append(result)

        if verbose:
            verdict_str = ", ".join(
                f"{d['dimension']}={d['passed']}({d['score']:.2f})"
                for d in result.get("verdicts", {}).values()
            )
            action = result.get("intervention", {}).get("action", "?")
            status = "ERROR" if "error" in result else f"OK(action={action})"
            print(f"  [{i+1}/{total}] {result['id']}: {status} | {verdict_str}")

    # 输出汇总信息
    success_count = sum(1 for r in results if "error" not in r)
    logger.info(
        "评测完成: %d/%d 条成功, %.1fs 总耗时",
        success_count, total,
        sum(r.get("elapsed", 0) for r in results if "elapsed" in r),
    )
    if verbose:
        print(f"\n评测完成: {success_count}/{total} 条成功")

    return results


# ════════════════════════════════════════════════════════════════
# 评分聚合
# ════════════════════════════════════════════════════════════════


def aggregate_scores(results: list[dict]) -> dict[str, Any]:
    """对评测结果进行评分聚合。

    计算方式：
    - macro_avg: 对每条 QA 的维度得分取平均，再对所有 QA 取平均（每条等权）
    - micro_avg: 对每个维度的得分取平均，再对所有维度取平均（每个维度等权）

    通过率统计：
    - overall_pass_rate: 所有维度都通过的 QA 比例
    - dimension_pass_rates: 每个维度的通过率
    - dimension_avg_scores: 每个维度的平均得分

    Args:
        results: 评测结果列表。

    Returns:
        dict: 聚合后的统计数据，包含通过率、平均分、维度统计、失败分析等。
    """
    # 过滤掉异常条目
    valid = [r for r in results if "error" not in r]

    if not valid:
        return {
            "total": len(results),
            "valid": 0,
            "fully_passed": 0,
            "overall_pass_rate": 0.0,
            "dimensions": {},
            "macro_avg": 0.0,
            "micro_avg": 0.0,
            "failure_analysis": {},
        }

    total = len(results)
    valid_count = len(valid)

    # ── 按维度聚合 ──
    dimension_scores: dict[str, list[float]] = defaultdict(list)
    dimension_passed: dict[str, list[bool]] = defaultdict(list)

    for r in valid:
        for dim, verdict in r.get("verdicts", {}).items():
            dimension_scores[dim].append(verdict.get("score", 0.0))
            dimension_passed[dim].append(verdict.get("passed", False))

    # ── 各维度统计 ──
    dimension_stats: dict[str, dict] = {}
    for dim in sorted(dimension_scores.keys()):
        scores = dimension_scores[dim]
        passed_list = dimension_passed[dim]
        dimension_stats[dim] = {
            "avg_score": round(mean(scores), 4) if scores else 0.0,
            "pass_rate": round(sum(passed_list) / len(passed_list), 4) if passed_list else 0.0,
            "count": len(scores),
            "min_score": round(min(scores), 4) if scores else 0.0,
            "max_score": round(max(scores), 4) if scores else 0.0,
            "failed_count": sum(1 for p in passed_list if not p),
        }

    # ── 总体通过率（所有维度都通过 = 通过） ──
    fully_passed = sum(
        1 for r in valid
        if all(v.get("passed", False) for v in r.get("verdicts", {}).values())
    )
    overall_pass_rate = round(fully_passed / len(valid), 4) if valid else 0.0

    # ── macro_avg: 每条 QA 等权 ──
    item_averages: list[float] = []
    for r in valid:
        scores = [v.get("score", 0.0) for v in r.get("verdicts", {}).values()]
        if scores:
            item_averages.append(mean(scores))
    macro_avg = round(mean(item_averages), 4) if item_averages else 0.0

    # ── micro_avg: 每个维度等权 ──
    dim_avgs = [stat["avg_score"] for stat in dimension_stats.values()]
    micro_avg = round(mean(dim_avgs), 4) if dim_avgs else 0.0

    # ── 失败案例分析 ──
    failure_analysis: dict[str, Any] = {
        "total_failed_items": total - fully_passed,
        "by_dimension": {},
    }
    for dim, stat in dimension_stats.items():
        if stat["failed_count"] > 0:
            failure_analysis["by_dimension"][dim] = {
                "failed_count": stat["failed_count"],
                "failure_rate": round(stat["failed_count"] / stat["count"], 4) if stat["count"] else 0.0,
            }

    # ── 干预动作分布 ──
    action_counts: dict[str, int] = Counter()
    for r in valid:
        action = r.get("intervention", {}).get("action", "none")
        action_counts[action] += 1
    failure_analysis["action_distribution"] = dict(action_counts)

    return {
        "total": total,
        "valid": valid_count,
        "fully_passed": fully_passed,
        "overall_pass_rate": overall_pass_rate,
        "dimensions": dimension_stats,
        "macro_avg": macro_avg,
        "micro_avg": micro_avg,
        "failure_analysis": failure_analysis,
    }


# ════════════════════════════════════════════════════════════════
# Markdown 报告生成
# ════════════════════════════════════════════════════════════════


def _escape_md(text: str) -> str:
    """转义 Markdown 特殊字符（防止表格格式错乱）。"""
    # 主要转义管道符和尖括号
    text = text.replace("|", "\\|")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text


def _stat_row(label: str, value: Any, unit: str = "") -> str:
    """生成 Markdown 表格中的单行统计（键值对）。"""
    return f"| {label} | {value}{unit} |"


def generate_report(
    results: list[dict],
    stats: dict[str, Any],
    dataset_path: str,
    compare_path: str | None,
    dimensions: set[str] | None,
    elapsed_total: float,
) -> str:
    """生成完整的 Markdown 格式评测报告。

    Args:
        results: 逐条评测结果列表。
        stats: 聚合统计数据。
        dataset_path: 数据集文件路径。
        compare_path: 对比报告路径（可选）。
        dimensions: 评估维度集合（可选）。
        elapsed_total: 总耗时（秒）。

    Returns:
        str: 格式化的 Markdown 报告全文。
    """
    lines: list[str] = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ══════════ 标题与元信息 ══════════
    lines.append("# RAG 质量保证 — 离线评测报告")
    lines.append("")
    lines.append(f"- **生成时间**: {timestamp}")
    lines.append(f"- **数据集**: `{dataset_path}`")
    lines.append(f"- **运行模式**: Mock（离线模拟，不调用真实 LLM API）")
    if dimensions:
        lines.append(f"- **评估维度**: {', '.join(sorted(dimensions))}")
    lines.append(f"- **总耗时**: {elapsed_total:.1f}s")
    if compare_path:
        lines.append(f"- **对比基线**: `{compare_path}`")
    lines.append("")

    # ══════════ 1. 总览 ══════════
    lines.append("## 一、评测总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(_stat_row("数据集条目数", stats["total"]))
    lines.append(_stat_row("有效评测数", stats["valid"]))
    lines.append(_stat_row("全部通过数", stats["fully_passed"]))
    lines.append(_stat_row("总体通过率", f"{stats['overall_pass_rate']:.2%}"))
    lines.append(_stat_row("Macro Avg（每条等权）", f"{stats['macro_avg']:.4f}"))
    lines.append(_stat_row("Micro Avg（每维度等权）", f"{stats['micro_avg']:.4f}"))
    lines.append("")

    # ══════════ 2. 各维度统计 ══════════
    lines.append("## 二、各维度评分概览")
    lines.append("")
    lines.append("| 维度 | 平均分 | 通过率 | 评测数 | 失败数 |")
    lines.append("|------|--------|--------|--------|--------|")
    dim_label_map = {
        "safety": "安全",
        "factuality": "事实性",
        "relevance": "相关性",
        "retrieval_quality": "检索质量",
    }
    for dim, stat in sorted(stats.get("dimensions", {}).items()):
        label = dim_label_map.get(dim, dim)
        lines.append(
            f"| {label} | {stat['avg_score']:.4f} | "
            f"{stat['pass_rate']:.2%} | {stat['count']} | "
            f"{stat['failed_count']} |"
        )
    lines.append("")

    # ══════════ 3. 详细结果 ══════════
    lines.append("## 三、逐条详细结果")
    lines.append("")
    valid_results = [r for r in results if "error" not in r]

    if not valid_results:
        lines.append("_无有效评测结果。_")
        lines.append("")
    else:
        for r in valid_results:
            item_id = r["id"]
            action = r.get("intervention", {}).get("action", "none")
            action_badge_map = {
                "block": "BLOCK",
                "degrade": "DEGRADE",
                "warn": "WARN",
                "none": "PASS",
            }
            badge = action_badge_map.get(action, action)

            lines.append(f"### {item_id} — {badge}")
            lines.append("")
            lines.append(f"- **问题**: {_escape_md(r['query'][:100])}")
            lines.append(f"- **耗时**: {r.get('elapsed', 0):.3f}s")
            if r.get("intervention", {}).get("reason"):
                lines.append(f"- **干预原因**: {_escape_md(r['intervention']['reason'])}")
            lines.append("")

            # 维度判定表格
            verdicts = r.get("verdicts", {})
            if verdicts:
                lines.append("| 维度 | 通过 | 得分 | 详情 |")
                lines.append("|------|------|------|------|")
                for dim, v in sorted(verdicts.items()):
                    dim_label = dim_label_map.get(dim, dim)
                    passed_str = "PASS" if v.get("passed", False) else "FAIL"
                    details = _escape_md(v.get("details", "")[:80])
                    lines.append(
                        f"| {dim_label} | {passed_str} | {v.get('score', 0):.4f} | {details} |"
                    )
                lines.append("")

    # 展示异常条目
    error_results = [r for r in results if "error" in r]
    if error_results:
        lines.append("### ⚠️ 异常条目")
        lines.append("")
        lines.append("| ID | 错误信息 |")
        lines.append("|----|----------|")
        for r in error_results:
            lines.append(f"| {r['id']} | {_escape_md(r.get('error', '未知错误'))} |")
        lines.append("")

    # ══════════ 4. 失败案例分析 ══════════
    lines.append("## 四、失败案例分析")
    lines.append("")
    fa = stats.get("failure_analysis", {})
    total_failed = fa.get("total_failed_items", 0)

    if total_failed == 0:
        lines.append("所有条目全部通过，无失败案例。")
        lines.append("")
    else:
        lines.append(f"共有 **{total_failed}** 条未完全通过。")
        lines.append("")

        # 各维度失败分布
        by_dim = fa.get("by_dimension", {})
        if by_dim:
            lines.append("### 维度失败分布")
            lines.append("")
            lines.append("| 维度 | 失败数 | 失败率 |")
            lines.append("|------|--------|--------|")
            for dim, info in sorted(by_dim.items()):
                label = dim_label_map.get(dim, dim)
                lines.append(
                    f"| {label} | {info['failed_count']} | {info['failure_rate']:.2%} |"
                )
            lines.append("")

        # 干预动作分布
        action_dist = fa.get("action_distribution", {})
        if action_dist:
            lines.append("### 干预动作分布")
            lines.append("")
            lines.append("| 动作 | 次数 |")
            lines.append("|------|------|")
            for action, count in sorted(action_dist.items()):
                lines.append(f"| {action.upper()} | {count} |")
            lines.append("")

        # 列出失败的具体条目
        lines.append("### 失败条目列表")
        lines.append("")
        for r in valid_results:
            failed_dims = [
                dim_label_map.get(dim, dim)
                for dim, v in r.get("verdicts", {}).items()
                if not v.get("passed", False)
            ]
            if failed_dims:
                action_label = r.get("intervention", {}).get("action", "none")
                lines.append(
                    f"- **{r['id']}**: {', '.join(failed_dims)} 未通过 → {action_label.upper()}"
                )
        lines.append("")

    # ══════════ 5. 对比基线（可选） ══════════
    if compare_path:
        lines.append("## 五、对比基线分析")
        lines.append("")
        lines.append(f"对比报告: `{compare_path}`")
        lines.append("")
        # 尝试从对比报告中提取关键指标
        baseline_stats = _parse_previous_report(compare_path)
        if baseline_stats:
            lines.append("| 指标 | 当前 | 基线 | 变化 |")
            lines.append("|------|------|------|------|")
            changes = _compute_changes(stats, baseline_stats)
            for row in changes:
                lines.append(row)
        else:
            lines.append("_无法解析基线报告，跳过对比。_")
        lines.append("")

    # ══════════ 6. 附录 ══════════
    lines.append("## 六、附录")
    lines.append("")
    lines.append("### 评分方法说明")
    lines.append("")
    lines.append("- **Macro Avg**: 每条 QA 的维度得分取平均后，再对所有 QA 取平均（每条等权）")
    lines.append("- **Micro Avg**: 每个维度的平均得分再取平均（每个维度等权）")
    lines.append("- **总体通过率**: 所有维度都通过（passed=True）的 QA 占总 QA 的比例")
    lines.append("")
    lines.append("### 维度说明")
    lines.append("")
    lines.append("| 维度 | 评估内容 | 评分范围 |")
    lines.append("|------|----------|----------|")
    lines.append("| 安全（safety） | 检测回答是否包含有害/违规/敏感内容 | 0.0~1.0 |")
    lines.append("| 事实性（factuality） | 检测回答是否有参考资料支撑，是否出现幻觉 | 0.0~1.0 |")
    lines.append("| 相关性（relevance） | 检测回答是否直接回应了用户问题 | 0.0~1.0 |")
    lines.append("| 检索质量（retrieval_quality） | 评估检索结果的相关度分数分布 | 0.0~1.0 |")
    lines.append("")

    return "\n".join(lines)


def _parse_previous_report(report_path: str) -> dict[str, float] | None:
    """解析前次 Markdown 报告，提取关键数值指标。

    Args:
        report_path: 前次报告文件路径。

    Returns:
        dict 或 None: 解析成功返回指标字典，否则返回 None。
    """
    path = Path(report_path)
    if not path.exists():
        logger.warning("基线报告不存在: %s", report_path)
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    stats: dict[str, float] = {}

    # 尝试通过正则提取常见指标
    patterns = [
        ("overall_pass_rate", r"总体通过率\s*\|\s*([\d.]+)%"),
        ("macro_avg", r"Macro Avg.*?\|\s*([\d.]+)"),
        ("micro_avg", r"Micro Avg.*?\|\s*([\d.]+)"),
    ]

    for key, pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                stats[key] = float(match.group(1))
            except ValueError:
                pass

    return stats if stats else None


def _compute_changes(
    current: dict[str, Any],
    baseline: dict[str, float],
) -> list[str]:
    """计算当前结果与基线的变化量，生成对比表格行。

    Args:
        current: 当前评测统计数据。
        baseline: 基线指标字典。

    Returns:
        list[str]: Markdown 表格行列表。
    """
    rows: list[str] = []
    label_map = {
        "overall_pass_rate": ("总体通过率", "%"),
        "macro_avg": ("Macro Avg", ""),
        "micro_avg": ("Micro Avg", ""),
    }

    for key, (label, unit) in label_map.items():
        curr_val = current.get(key, 0.0)
        base_val = baseline.get(key, 0.0)
        if unit == "%":
            curr_str = f"{curr_val:.2%}"
            base_str = f"{base_val:.2f}%"
            diff = curr_val - base_val / 100.0
            diff_str = f"{diff:+.2%}"
        else:
            curr_str = f"{curr_val:.4f}"
            base_str = f"{base_val:.4f}"
            diff = curr_val - base_val
            diff_str = f"{diff:+.4f}"

        indicator = "+" if diff >= 0 else "-"
        rows.append(
            f"| {label} | {curr_str} | {base_str} | {indicator} {diff_str} |"
        )

    return rows


# ════════════════════════════════════════════════════════════════
# 报告写入
# ════════════════════════════════════════════════════════════════


def write_report(report: str, output_dir: str) -> str:
    """将报告写入文件，返回文件路径。

    文件名格式: eval_report_YYYYMMDD_HHMMSS.md

    Args:
        report: Markdown 报告内容。
        output_dir: 输出目录路径。

    Returns:
        str: 报告文件的绝对路径。
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"eval_report_{timestamp}.md"
    filepath = out_path / filename

    filepath.write_text(report, encoding="utf-8")
    logger.info("报告已写入: %s", filepath.resolve())
    return str(filepath.resolve())


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    支持参数:
    --dataset PATH      数据集 JSON 路径（默认: tests/test_data/eval_dataset.json）
    --output DIR        报告输出目录（默认: docs）
    --compare PATH      对比前次报告（用于基线对比）
    --dimensions LIST   评估维度，逗号分隔（默认: 全部维度）
    --verbose           详细输出模式
    """
    parser = argparse.ArgumentParser(
        description="RAG 质量保证离线评测工具 — 批量跑分 + Markdown 报告生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/run_eval.py\n"
            "  python scripts/run_eval.py --dataset tests/test_data/eval_dataset.json\n"
            "  python scripts/run_eval.py --output docs --verbose\n"
            "  python scripts/run_eval.py --compare docs/eval_report_20260601.md\n"
            "  python scripts/run_eval.py --dimensions safety,factuality\n"
        ),
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="tests/test_data/eval_dataset.json",
        help="评测数据集 JSON 路径（默认: tests/test_data/eval_dataset.json）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="docs",
        help="Markdown 报告输出目录（默认: docs）",
    )
    parser.add_argument(
        "--compare",
        type=str,
        default=None,
        help="前次评测报告路径，用于基线对比分析",
    )
    parser.add_argument(
        "--dimensions",
        type=str,
        default=None,
        help="评估维度，逗号分隔，如: safety,factuality,relevance,retrieval_quality（默认: 全部）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="详细输出模式，显示每条 QA 的评测进度",
    )
    return parser.parse_args(argv)


def main() -> None:
    """主入口：加载数据集 → 构建 QualityGuard → 批量评测 → 聚合 → 输出报告。"""
    args = parse_args()

    # ── 日志级别 ──
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── 解析维度过滤 ──
    all_dimensions = {"safety", "factuality", "relevance", "retrieval_quality"}
    selected_dims: set[str] | None = None
    if args.dimensions:
        dims = {d.strip() for d in args.dimensions.split(",")}
        invalid = dims - all_dimensions
        if invalid:
            logger.warning("未知维度: %s，可用维度: %s", invalid, all_dimensions)
        selected_dims = dims & all_dimensions
        if not selected_dims:
            logger.warning("未指定有效维度，使用全部维度")
            selected_dims = None

    # ── 步骤 1: 加载数据集 ──
    dataset = load_dataset(args.dataset)
    if not dataset:
        logger.warning("数据集为空，将生成空报告")

    # ── 步骤 2: 构建 QualityGuard（Mock 模式） ──
    logger.info("构建 QualityGuard（Mock 模式）...")
    config = EvalSettings()
    guard = build_mock_guard(config=config)

    # ── 步骤 3: 执行批量评测 ──
    logger.info("开始批量评测（共 %d 条）...", len(dataset))
    t_start = time.time()

    results = run_evaluation(
        dataset=dataset,
        guard=guard,
        dimensions=selected_dims,
        verbose=args.verbose,
    )

    elapsed_total = time.time() - t_start

    # ── 步骤 4: 评分聚合 ──
    logger.info("聚合评分...")
    stats = aggregate_scores(results)

    # ── 步骤 5: 生成报告 ──
    logger.info("生成 Markdown 报告...")
    if args.verbose:
        print(f"\n{'='*60}")
        print(f"总耗时: {elapsed_total:.1f}s")
        print(f"Macro Avg: {stats['macro_avg']:.4f}")
        print(f"Micro Avg: {stats['micro_avg']:.4f}")
        print(f"总体通过率: {stats['overall_pass_rate']:.2%}")
        if stats.get("dimensions"):
            print(f"\n各维度评分:")
            for dim, dim_stat in stats["dimensions"].items():
                print(f"  {dim}: avg={dim_stat['avg_score']:.4f}, pass_rate={dim_stat['pass_rate']:.2%}")
        print(f"{'='*60}\n")

    report = generate_report(
        results=results,
        stats=stats,
        dataset_path=args.dataset,
        compare_path=args.compare,
        dimensions=selected_dims,
        elapsed_total=elapsed_total,
    )

    # ── 步骤 6: 写入报告 ──
    report_path = write_report(report, args.output)
    print(f"\n[OK] 评测报告已生成: {report_path}")
    print(f"   总条目: {stats['total']} | 有效: {stats['valid']} | "
          f"通过率: {stats['overall_pass_rate']:.2%}")
    print(f"   Macro Avg: {stats['macro_avg']:.4f} | Micro Avg: {stats['micro_avg']:.4f}")


if __name__ == "__main__":
    main()
