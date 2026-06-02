#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/benchmark_quality.py — QualityGuard 质检延迟基准测试脚本

测量 QualityGuard 各阶段延迟（毫秒），输出终端表格 + Markdown 报告。

阶段划分:
  1. 关键词预筛时间  — KeywordFilter.prefilter()
  2. Safety LLM Judge   — SafetyChecker.evaluate()（含关键词预筛 + LLM 调用）
  3. Factuality LLM     — FactualityChecker.evaluate()
  4. Relevance LLM      — RelevanceChecker.evaluate()
  5. 干预执行时间       — InterventionEngine.run_all()
  6. 质检总附加时间     — QualityGuard.run() 总耗时（最关键指标）

用法:
    python scripts/benchmark_quality.py --mock          # Mock 模式（默认，不调真实 API）
    python scripts/benchmark_quality.py                  # 真实模式（需配置 API Key）
    python scripts/benchmark_quality.py --samples 50     # 50 次采样
    python scripts/benchmark_quality.py --verbose        # 显示每次迭代详情
    python scripts/benchmark_quality.py --output ./reports  # 指定输出目录
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 将项目根目录加入 sys.path，确保模块可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 第三方库 ─────────────────────────────────────────────
# psutil 用于内存检测（可选）
try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ── 项目模块 ─────────────────────────────────────────────
from src.config import Settings
from src.quality.base import QualityVerdict
from src.quality.factuality import FactualityChecker
from src.quality.guard import QualityGuard
from src.quality.intervention import InterventionEngine
from src.quality.keyword_filter import KeywordFilter
from src.quality.relevance import RelevanceChecker
from src.quality.safety import SafetyChecker


# ═══════════════════════════════════════════════════════════
# 计时包装器
# ═══════════════════════════════════════════════════════════


class _TimedFilter:
    """计时包装器：包裹 KeywordFilter，记录 prefilter 调用耗时。"""

    def __init__(self, kw_filter: KeywordFilter, name: str = "keyword_filter"):
        self._filter = kw_filter
        self.name = name
        self.times: list[float] = []  # 每次调用的耗时（秒）

    def prefilter(self, text: str) -> list[Any]:
        start = time.perf_counter()
        result = self._filter.prefilter(text)
        elapsed = time.perf_counter() - start
        self.times.append(elapsed)
        return result


class _TimedJudge:
    """计时包装器：包裹 QualityJudge，记录 evaluate 调用耗时。"""

    def __init__(self, judge: Any, name: str):
        self._judge = judge
        self.name = name
        self.times: list[float] = []  # 每次调用的耗时（秒）

    def evaluate(self, query: str, answer: str, context: str | None = None) -> QualityVerdict:
        start = time.perf_counter()
        result = self._judge.evaluate(query, answer, context)
        elapsed = time.perf_counter() - start
        self.times.append(elapsed)
        return result

    # 透传 QualityJudge 属性，确保兼容
    def __getattr__(self, name: str) -> Any:
        return getattr(self._judge, name)


class _TimedIntervention:
    """计时包装器：包裹 InterventionEngine，记录 run_all 调用耗时。"""

    def __init__(self, intervention: InterventionEngine):
        self._intervention = intervention
        self.times: list[float] = []  # 每次调用的耗时（秒）

    def run_all(
        self,
        verdicts: list[QualityVerdict],
        original_response: dict[str, Any],
    ) -> tuple[dict[str, Any], Any]:
        start = time.perf_counter()
        result = self._intervention.run_all(verdicts, original_response)
        elapsed = time.perf_counter() - start
        self.times.append(elapsed)
        return result

    # 透传其他属性（如 rules）
    def __getattr__(self, name: str) -> Any:
        return getattr(self._intervention, name)


# ═══════════════════════════════════════════════════════════
# 基准测试数据
# ═══════════════════════════════════════════════════════════

# 模拟的测试数据：多种类型的 query + answer + context 组合
# 设计原因：使用多样化的测试数据，避免单一场景的偏差
_BENCHMARK_SAMPLES = [
    {
        "query": "什么是 RAG 技术？",
        "answer": (
            "RAG（Retrieval-Augmented Generation）是一种结合检索和生成的 AI 技术框架。"
            "它通过从知识库中检索相关文档来增强大语言模型的生成能力，"
            "有效解决了大模型知识更新和幻觉问题。"
        ),
        "context": (
            "RAG（Retrieval-Augmented Generation）是一种将信息检索与文本生成相结合的技术。"
            "它首先从外部知识库检索相关文档，然后将检索结果作为上下文输入给大语言模型，"
            "从而生成更准确、更有根据的回答。"
        ),
    },
    {
        "query": "劳动合同的解除条件有哪些？",
        "answer": (
            "根据《劳动合同法》，劳动合同的解除条件包括："
            "1. 协商一致解除；2. 劳动者提前30日通知解除；"
            "3. 用人单位存在过错时劳动者可随时解除；"
            "4. 用人单位在特定情形下可单方解除（如严重违纪）。"
        ),
        "context": (
            "《劳动合同法》规定了解除劳动合同的多种情形："
            "第三十六条 用人单位与劳动者协商一致，可以解除劳动合同。"
            "第三十七条 劳动者提前三十日书面通知，可以解除劳动合同。"
            "第三十八条 用人单位有下列情形之一的，劳动者可以解除劳动合同。"
            "第三十九条 劳动者有下列情形之一的，用人单位可以解除劳动合同。"
        ),
    },
    {
        "query": "如何配置 PostgreSQL 数据库？",
        "answer": (
            "配置 PostgreSQL 数据库需要以下步骤：1. 安装 PostgreSQL；"
            "2. 设置 pg_hba.conf 配置文件控制客户端认证；"
            "3. 调整 postgresql.conf 设置内存、连接数等参数；"
            "4. 创建数据库和用户。常用命令包括 CREATE DATABASE 和 CREATE USER。"
        ),
        "context": (
            "PostgreSQL 配置主要涉及 postgresql.conf（数据库服务参数）"
            "和 pg_hba.conf（客户端认证控制）。常见的配置项包括："
            "shared_buffers、work_mem、max_connections 等。"
            "配置完成后需要重启服务生效。"
        ),
    },
]

# 模拟的 sources 数据（用于检索质量评估）
_BENCHMARK_SOURCES = [
    {"doc_id": "doc_001", "filename": "rag_intro.pdf", "score": 0.92, "snippet": "RAG 结合检索和生成..."},
    {"doc_id": "doc_002", "filename": "rag_advanced.pdf", "score": 0.85, "snippet": "RAG 的高级应用场景..."},
    {"doc_id": "doc_003", "filename": "llm_basics.pdf", "score": 0.78, "snippet": "大语言模型的工作原理..."},
]


# ═══════════════════════════════════════════════════════════
# 统计计算
# ═══════════════════════════════════════════════════════════


@dataclass
class PhaseStats:
    """单个阶段的统计结果。"""

    name: str  # 阶段名称
    times_ms: list[float]  # 每次迭代的耗时（毫秒）
    count: int  # 有效采样数

    @property
    def min_ms(self) -> float:
        return min(self.times_ms) if self.times_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.times_ms) if self.times_ms else 0.0

    @property
    def avg_ms(self) -> float:
        return statistics.mean(self.times_ms) if self.times_ms else 0.0

    @property
    def median_ms(self) -> float:
        """P50 中位数"""
        return statistics.median(self.times_ms) if self.times_ms else 0.0

    @property
    def p95_ms(self) -> float:
        """P95 百分位"""
        return self._percentile(95)

    @property
    def p99_ms(self) -> float:
        """P99 百分位"""
        return self._percentile(99)

    def _percentile(self, p: int) -> float:
        if not self.times_ms:
            return 0.0
        sorted_t = sorted(self.times_ms)
        k = (p / 100.0) * (len(sorted_t) - 1)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_t[int(k)]
        return sorted_t[f] * (c - k) + sorted_t[c] * (k - f)


def compute_stats(name: str, times_s: list[float | None]) -> PhaseStats:
    """将秒级耗时列表转为毫秒并计算统计量。

    Args:
        name: 阶段名称
        times_s: 每次迭代的耗时（秒）。None 表示该次无效。

    Returns:
        PhaseStats: 统计结果
    """
    valid = [t * 1000 for t in times_s if t is not None]
    return PhaseStats(name=name, times_ms=valid, count=len(valid))


# ═══════════════════════════════════════════════════════════
# 输出格式化
# ═══════════════════════════════════════════════════════════


def _fmt(val: float) -> str:
    """格式化毫秒值为字符串，保留 2 位小数。"""
    return f"{val:.2f}"


def format_table(stats_list: list[PhaseStats]) -> str:
    """生成终端表格字符串。

    Args:
        stats_list: 各阶段的统计结果列表

    Returns:
        str: 格式化表格
    """
    header = f"{'阶段':<28} {'采样数':>6} {'min(ms)':>10} {'max(ms)':>10} {'avg(ms)':>10} {'p50(ms)':>10} {'p95(ms)':>10} {'p99(ms)':>10}"
    sep = "-" * len(header)
    lines = [header, sep]

    for s in stats_list:
        lines.append(
            f"{s.name:<28} {s.count:>6} {_fmt(s.min_ms):>10} {_fmt(s.max_ms):>10} "
            f"{_fmt(s.avg_ms):>10} {_fmt(s.median_ms):>10} {_fmt(s.p95_ms):>10} {_fmt(s.p99_ms):>10}"
        )

    return "\n".join(lines)


def format_markdown(
    stats_list: list[PhaseStats],
    args: argparse.Namespace,
    total_duration_s: float,
    system_info: dict[str, str],
) -> str:
    """生成 Markdown 格式报告。

    Args:
        stats_list: 各阶段的统计结果列表
        args: 命令行参数
        total_duration_s: 总耗时（秒）
        system_info: 系统信息字典

    Returns:
        str: Markdown 格式的报告
    """
    lines = [
        "# QualityGuard 质检延迟基准测试报告",
        "",
        f"- **测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **模式**: {'Mock（模拟）' if args.mock else 'Real（真实 API）'}",
        f"- **采样次数**: {args.samples}",
        f"- **总耗时**: {total_duration_s:.2f}s",
        f"- **并行评估**: {args.parallel}",
        "",
        "## 系统环境",
        "",
    ]
    for k, v in system_info.items():
        lines.append(f"- **{k}**: {v}")

    lines += [
        "",
        "## 各阶段延迟统计",
        "",
        "| 阶段 | 采样数 | min(ms) | max(ms) | avg(ms) | p50(ms) | p95(ms) | p99(ms) |",
        "|------|--------|---------|---------|---------|---------|---------|---------|",
    ]
    for s in stats_list:
        lines.append(
            f"| {s.name} | {s.count} | {_fmt(s.min_ms)} | {_fmt(s.max_ms)} | "
            f"{_fmt(s.avg_ms)} | {_fmt(s.median_ms)} | {_fmt(s.p95_ms)} | {_fmt(s.p99_ms)} |"
        )

    lines += [
        "",
        "## 分析说明",
        "",
        "- **关键词预筛**: KeywordFilter.prefilter() 的耗时，纯文本匹配，极低延迟。",
        "- **Safety LLM Judge**: SafetyChecker.evaluate() 总耗时，包含关键词预筛 + 可选的 LLM 调用。",
        "- **Factuality LLM Judge**: FactualityChecker.evaluate() 总耗时，包含 LLM 调用。",
        "- **Relevance LLM Judge**: RelevanceChecker.evaluate() 总耗时，包含 LLM 调用。",
        "- **干预执行**: InterventionEngine.run_all() 的耗时，纯逻辑计算。",
        "- **质检总附加时间**: QualityGuard.run() 的总耗时，是衡量质检对响应时间影响的关键指标。",
        "",
        "### Mock 模式说明",
        "",
        "Mock 模式使用 MockLLMJudge 替代真实 LLM，测量的是编排开销而非 LLM 延迟。",
        "各 Judge 的延迟反映的是 Mock 调用的响应速度（通常 < 1ms），",
        "在真实场景中每次 LLM Judge 调用约 1-3 秒。",
        "",
        "### 并行 vs 串行",
        "",
        "并行模式下，质检总附加时间 ≈ 关键词预筛 + max(Safety, Factuality, Relevance) + 干预执行。",
        "串行模式下，质检总附加时间 ≈ 各阶段耗时之和。",
    ]

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 系统信息采集
# ═══════════════════════════════════════════════════════════


def _get_system_info() -> dict[str, str]:
    """采集系统环境信息。"""
    info: dict[str, str] = {}
    info["Python 版本"] = sys.version.split()[0]

    try:
        import platform

        info["操作系统"] = f"{platform.system()} {platform.release()}"
    except Exception:
        info["操作系统"] = "未知"

    if _HAS_PSUTIL:
        try:
            info["CPU 核心数"] = str(psutil.cpu_count(logical=True))
            mem = psutil.virtual_memory()
            info["内存总量"] = f"{mem.total / 1024 / 1024 / 1024:.1f} GB"
        except Exception:
            pass

    return info


# ═══════════════════════════════════════════════════════════
# 核心基准测试逻辑
# ═══════════════════════════════════════════════════════════


def _create_mock_provider() -> Any:
    """创建 Mock LLM Judge 实例。

    MockLLMJudge 定义在 tests/conftest.py 中，是一个不依赖真实 API 的模拟 LLM。
    它根据 Prompt 中的 [EVALUATION_TASK] 标记返回预设 JSON，延迟 < 1ms。

    Returns:
        MockLLMJudge 实例
    """
    from tests.conftest import MockLLMJudge

    return MockLLMJudge(mode="normal")


def _create_config_dict(settings: Settings) -> dict[str, Any]:
    """从 Settings 对象构造 checkers 需要的配置字典。

    Args:
        settings: Settings 全局配置

    Returns:
        dict: 配置字典，包含 quality_* 相关配置
    """
    return {
        "quality_judge_provider": settings.quality_judge_provider,
        "quality_judge_model": settings.quality_judge_model,
        "quality_judge_timeout_s": settings.quality_judge_timeout_s,
        "quality_fail_closed_for_safety": settings.quality_fail_closed_for_safety,
        "quality_fail_open_for_others": settings.quality_fail_open_for_others,
        "prompts_dir": settings.prompts_dir,
    }


def _build_guard(
    mock: bool,
    settings: Settings,
    parallel: bool,
) -> tuple[QualityGuard, dict[str, _TimedJudge], _TimedFilter, _TimedIntervention]:
    """构造带计时包装的 QualityGuard 实例。

    创建流程：
    1. 创建 LLM provider（mock 或真实）
    2. 构造各维度的 Checker 并包裹计时器
    3. 构造带计时包装的 InterventionEngine
    4. 构造 QualityGuard（使用包裹后的组件）
    5. 将 guard 的干预引擎替换为计时包装版本
    6. 返回 guard 和所有计时器引用

    Args:
        mock: 是否使用 Mock 模式
        settings: Settings 配置
        parallel: 是否启用并行评估

    Returns:
        tuple: (guard, timed_judges, timed_filter, timed_intervention)
    """
    config_dict = _create_config_dict(settings)

    # ── 1. LLM Provider ────────────────────────────────
    if mock:
        llm_provider = _create_mock_provider()
    else:
        from src.llm.router import get_llm

        llm_provider = get_llm()

    # ── 2. 构造各维度 Checker ──────────────────────────
    # SafetyChecker（两阶段：关键词预筛 + LLM Judge）
    # 为 SafetyChecker 创建带计时的 KeywordFilter
    raw_filter = KeywordFilter()
    timed_filter = _TimedFilter(raw_filter, "keyword_filter")

    # 创建 SafetyChecker 时注入带计时的 KeywordFilter
    safety_checker = SafetyChecker(
        llm_provider=llm_provider,
        config=config_dict,
        keyword_filter=timed_filter,  # 注入计时版本
    )
    timed_safety = _TimedJudge(safety_checker, "safety")

    # FactualityChecker
    raw_factuality = FactualityChecker(llm_provider=llm_provider, config=config_dict)
    timed_factuality = _TimedJudge(raw_factuality, "factuality")

    # RelevanceChecker
    raw_relevance = RelevanceChecker(
        llm_provider=llm_provider,
        prompt_dir=f"{settings.prompts_dir}/quality",
    )
    timed_relevance = _TimedJudge(raw_relevance, "relevance")

    timed_judges: dict[str, _TimedJudge] = {
        "safety": timed_safety,
        "factuality": timed_factuality,
        "relevance": timed_relevance,
    }

    # ── 3. InterventionEngine ──────────────────────────
    intervention = InterventionEngine()
    timed_intervention = _TimedIntervention(intervention)

    # ── 4. QualityGuard ────────────────────────────────
    guard = QualityGuard(
        checkers=timed_judges,  # 类型上符合 dict[str, QualityJudge]
        intervention=timed_intervention,  # 在 guard 内部会调用 run_all
        config=settings,
    )

    return guard, timed_judges, timed_filter, timed_intervention


def run_benchmark(args: argparse.Namespace) -> list[PhaseStats]:
    """执行基准测试主循环。

    对每个采样:
    1. 从 _BENCHMARK_SAMPLES 中选择一个测试用例（轮询）
    2. 调用 guard.run() 并记录各阶段耗时
    3. 收集各计时器的耗时数据

    Args:
        args: 命令行参数

    Returns:
        list[PhaseStats]: 各阶段的统计结果
    """
    settings = Settings()

    # 覆盖并行配置
    if not args.parallel:
        settings.quality_parallel_eval = False

    guard, timed_judges, timed_filter, timed_intervention = _build_guard(
        mock=args.mock,
        settings=settings,
        parallel=args.parallel,
    )

    # 采样数
    n = args.samples

    # 各阶段的耗时记录（秒）
    total_times: list[float | None] = [None] * n

    if args.verbose:
        print(f"\n{'=' * 70}")
        print(f"  开始基准测试: {n} 次采样, {'Mock' if args.mock else 'Real'} 模式")
        print(f"{'=' * 70}\n")

    # ── 预热 ──────────────────────────────────────────────
    # 执行 2 次预热，消除 jieba 惰性加载和 Prompt 文件 IO 的影响
    warmup_sample = _BENCHMARK_SAMPLES[0]
    for _ in range(2):
        guard.run(
            query=warmup_sample["query"],
            answer=warmup_sample["answer"],
            context=warmup_sample["context"],
            sources=_BENCHMARK_SOURCES,
        )
    # 预热后重置计时器，避免预热数据计入统计
    timed_filter.times.clear()
    for j in timed_judges.values():
        j.times.clear()
    timed_intervention.times.clear()

    if args.verbose:
        print(f"  预热完成，开始 {n} 次正式采样\n")

    # ── 基准测试主循环 ────────────────────────────────
    for i in range(n):
        # 轮询选择测试数据
        sample = _BENCHMARK_SAMPLES[i % len(_BENCHMARK_SAMPLES)]
        sources = _BENCHMARK_SOURCES

        # 记录总耗时
        start_total = time.perf_counter()

        try:
            guard.run(
                query=sample["query"],
                answer=sample["answer"],
                context=sample["context"],
                sources=sources,
            )
            total_times[i] = time.perf_counter() - start_total
        except Exception as exc:
            if args.verbose:
                print(f"  [{i + 1}/{n}] 异常: {exc}")
            total_times[i] = None

    # ── 收集各阶段计时数据 ────────────────────────────
    # 注意：各阶段计时器内部已记录每次调用的耗时
    stats_list: list[PhaseStats] = [
        compute_stats("关键词预筛", timed_filter.times),
        compute_stats("Safety LLM Judge", timed_judges["safety"].times),
        compute_stats("Factuality LLM Judge", timed_judges["factuality"].times),
        compute_stats("Relevance LLM Judge", timed_judges["relevance"].times),
        compute_stats("干预执行", timed_intervention.times),
        compute_stats("质检总附加时间", total_times),
    ]

    return stats_list


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        argparse.Namespace: 解析后的参数
    """
    parser = argparse.ArgumentParser(
        description="QualityGuard 质检延迟基准测试脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s --mock               # Mock 模式（默认）\n"
            "  %(prog)s --samples 50          # 50 次采样\n"
            "  %(prog)s --mock --verbose      # 显示详细日志\n"
            "  %(prog)s --output ./reports    # 指定输出目录\n"
            "  %(prog)s --no-parallel         # 串行模式\n"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="使用 Mock LLM（不调真实 API，默认开启）",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=20,
        help="采样次数（默认: 20）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="docs",
        help="输出目录（默认: docs）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="显示每次迭代详情",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=True,
        help="启用并行评估（默认开启）",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_false",
        dest="parallel",
        help="禁用并行评估（串行模式）",
    )
    parser.add_argument(
        "--no-mock",
        action="store_false",
        dest="mock",
        help="使用真实 API 而非 Mock",
    )
    return parser.parse_args(argv)


def main() -> None:
    """主入口函数。"""
    args = parse_args()
    start_time = time.perf_counter()

    # 校验参数
    if args.samples < 1:
        print("[错误] --samples 必须 >= 1")
        sys.exit(1)

    # ── 执行基准测试 ──────────────────────────────────
    try:
        stats_list = run_benchmark(args)
    except ImportError as exc:
        print(f"[错误] 导入失败: {exc}")
        print("提示: 请确保在项目根目录运行此脚本")
        sys.exit(1)
    except Exception as exc:
        print(f"[错误] 基准测试异常: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    total_duration_s = time.perf_counter() - start_time

    # ── 输出终端表格 ──────────────────────────────────
    print("\n" + "=" * 100)
    print("  QualityGuard 质检延迟基准测试结果")
    print("=" * 100)
    print(f"  模式: {'Mock（模拟）' if args.mock else 'Real（真实 API）'}")
    print(f"  采样次数: {args.samples}")
    print(f"  总耗时: {total_duration_s:.2f}s")
    print(f"  并行评估: {'是' if args.parallel else '否'}")
    print()
    print(format_table(stats_list))
    print()

    # ── 写入 Markdown 报告 ────────────────────────────
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"latency_benchmark_{timestamp}.md"

    system_info = _get_system_info()
    markdown_content = format_markdown(stats_list, args, total_duration_s, system_info)

    report_path.write_text(markdown_content, encoding="utf-8")
    print(f"  报告已保存: {report_path.resolve()}")
    print()


if __name__ == "__main__":
    main()
