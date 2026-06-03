"""Keyword filter — 安全关键词预筛模块。

提供 KeywordFilter 类，对输入文本进行快速关键词匹配，
作为两阶段安全评估的第一阶段（快速预筛）。

工作流程：
  1. KeywordFilter.prefilter(text) 命中关键词 → 直接判定违规（不调 LLM）
  2. 不命中时 → 交由 SafetyChecker（LLM 深度评估）做第二阶段判断

支持三种匹配方式：
  - 精确匹配（子串匹配）：str.find() 定位
  - 正则匹配：re.finditer() 定位
  - 中文分词兼容：含中文的关键词额外做 jieba 分词匹配
"""

from __future__ import annotations

import logging
import re

from src.quality.config import (
    KeywordMatch,
    SafetyCategory,
    get_default_safety_categories,
)

logger = logging.getLogger(__name__)

# ── Lazy-load jieba ─────────────────────────────────────
_jieba = None
_jieba_unavailable = False


def _get_jieba():
    """惰性加载 jieba 分词库，避免强制依赖。"""
    global _jieba, _jieba_unavailable
    if _jieba is None and not _jieba_unavailable:
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
                import jieba

            jieba.setLogLevel(logging.WARNING)
            _jieba = jieba
        except ModuleNotFoundError:
            _jieba_unavailable = True
            logger.warning("jieba not installed — Chinese keyword matching will use substring only")
    return _jieba


def _has_cjk(text: str) -> bool:
    """检查文本是否包含中文字符（CJK Unified Ideographs）。"""
    return bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))


# ── KeywordFilter 主类 ─────────────────────────────────


class KeywordFilter:
    """关键词过滤器：对输入文本进行安全关键词预筛。

    用法示例：
        >>> filter = KeywordFilter()
        >>> matches = filter.prefilter("这是一个测试文本，包含暴力内容")
        >>> len(matches)
        1
        >>> matches[0].category
        'harmful_content'
        >>> matches[0].match_type
        'exact'
    """

    def __init__(
        self,
        categories: list[SafetyCategory] | None = None,
        yaml_path: str | None = None,
    ):
        """初始化 KeywordFilter。

        加载顺序：
          1. 内置默认安全类别（get_default_safety_categories）
          2. 合并自定义类别（categories 参数）
          3. 合并 YAML 文件中定义的类别（yaml_path 参数）

        Args:
            categories: 自定义 SafetyCategory 列表，与内置类别合并
            yaml_path: YAML 文件路径，从文件加载自定义类别
        """
        # 加载内置默认类别
        self._categories: list[SafetyCategory] = get_default_safety_categories()

        # 合并自定义类别
        if categories:
            self._categories.extend(categories)

        # 从 YAML 加载
        if yaml_path:
            from src.quality.config import load_safety_categories_from_yaml

            yaml_categories = load_safety_categories_from_yaml(yaml_path)
            self._categories.extend(yaml_categories)

    # ── 公开方法 ─────────────────────────────────────────

    def prefilter(self, text: str) -> list[KeywordMatch]:
        """对输入文本进行关键词预筛，返回所有匹配结果。

        按类别遍历所有关键词和正则模式：
          - 精确匹配：str.find() 子串匹配
          - 正则匹配：re.finditer() 找到所有匹配
          - 中文兼容：含中文的关键词额外做 jieba 分词匹配

        Args:
            text: 待检查的输入文本

        Returns:
            list[KeywordMatch]: 按位置排序的所有匹配结果（去重）
        """
        if not text:
            return []

        results: list[KeywordMatch] = []
        # 去重 key：(category, keyword, position)
        seen: set[tuple[str, str, int]] = set()

        for category in self._categories:
            # ── 精确匹配（关键词列表） ──
            for keyword in category.keywords:
                self._collect_exact_matches(text, keyword, category.name, results, seen)

            # ── 正则匹配 ──
            for pattern in category.regex_patterns:
                self._collect_regex_matches(text, pattern, category.name, results, seen)

        # 按位置升序排列
        results.sort(key=lambda x: x.position)
        return results

    def add_category(self, category: SafetyCategory) -> None:
        """动态添加自定义安全类别。

        如果同名类别已存在则跳过（记录 warning 日志）。

        Args:
            category: 要添加的安全分类实例
        """
        for existing in self._categories:
            if existing.name == category.name:
                logger.warning("category '%s' already exists, skip adding", category.name)
                return
        self._categories.append(category)
        logger.info("added custom category '%s' with %d keywords", category.name, len(category.keywords))

    def get_categories(self) -> list[SafetyCategory]:
        """返回当前加载的所有安全类别（防御性拷贝）。

        Returns:
            list[SafetyCategory]: 当前加载的安全分类列表
        """
        return list(self._categories)

    def reload(self) -> None:
        """热加载类别列表：从内置默认配置重新加载。

        注意：通过 add_category 添加的自定义类别会丢失，
        需要重新调用 add_category 添加。
        """
        self._categories = get_default_safety_categories()
        logger.info("KeywordFilter categories reloaded from defaults")

    # ── 内部匹配方法 ────────────────────────────────────

    def _collect_exact_matches(
        self,
        text: str,
        keyword: str,
        category_name: str,
        results: list[KeywordMatch],
        seen: set[tuple[str, str, int]],
    ) -> None:
        """收集所有精确匹配（子串 + jieba 分词兼容）。"""
        # 1. 子串匹配：找到所有出现位置
        for pos in self._find_all_substring(text, keyword):
            key = (category_name, keyword, pos)
            if key not in seen:
                seen.add(key)
                results.append(
                    KeywordMatch(
                        category=category_name,
                        keyword=keyword,
                        position=pos,
                        match_type="exact",
                    )
                )

        # 2. 中文分词兼容匹配（仅对含中文的关键词生效）
        if _has_cjk(keyword):
            jieba = _get_jieba()
            if jieba is not None:
                self._collect_jieba_matches(text, keyword, category_name, results, seen)

    def _collect_jieba_matches(
        self,
        text: str,
        keyword: str,
        category_name: str,
        results: list[KeywordMatch],
        seen: set[tuple[str, str, int]],
    ) -> None:
        """通过 jieba 分词做中文关键词匹配。

        将关键词和文本都做 jieba 分词，然后在文本的 token 序列中
        查找关键词 token 序列的连续匹配。
        """
        jieba = _get_jieba()
        if jieba is None:
            return

        try:
            # tokenize() 返回 (word, start, end) 元组
            text_tokens = list(jieba.tokenize(text))
            keyword_tokens = list(jieba.cut(keyword))
        except Exception as exc:
            logger.warning("jieba tokenization error for keyword '%s': %s", keyword, exc)
            return

        kt_len = len(keyword_tokens)
        if kt_len == 0:
            return

        # 在文本 token 序列中查找连续匹配
        for i in range(len(text_tokens) - kt_len + 1):
            segment = [t[0] for t in text_tokens[i : i + kt_len]]
            if segment == keyword_tokens:
                # 取第一个 token 的起始位置
                pos = text_tokens[i][1]
                key = (category_name, keyword, pos)
                if key not in seen:
                    seen.add(key)
                    results.append(
                        KeywordMatch(
                            category=category_name,
                            keyword=keyword,
                            position=pos,
                            match_type="exact",
                        )
                    )
                break  # 一个关键词只记录首次 token 匹配位置

    def _collect_regex_matches(
        self,
        text: str,
        pattern: str,
        category_name: str,
        results: list[KeywordMatch],
        seen: set[tuple[str, str, int]],
    ) -> None:
        """收集所有正则匹配结果。"""
        try:
            compiled = re.compile(pattern)
            for match in compiled.finditer(text):
                pos = match.start()
                key = (category_name, pattern, pos)
                if key not in seen:
                    seen.add(key)
                    results.append(
                        KeywordMatch(
                            category=category_name,
                            keyword=pattern,
                            position=pos,
                            match_type="regex",
                        )
                    )
        except re.error as exc:
            logger.warning("invalid regex pattern '%s': %s", pattern, exc)

    @staticmethod
    def _find_all_substring(text: str, keyword: str) -> list[int]:
        """找到关键字在文本中的所有出现位置。

        Args:
            text: 待搜索的文本
            keyword: 要查找的关键词

        Returns:
            list[int]: 所有匹配位置的列表（升序），无匹配时返回空列表
        """
        positions: list[int] = []
        start = 0
        while True:
            pos = text.find(keyword, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + 1
        return positions
