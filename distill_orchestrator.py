#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 three_tier_round1.py v4 重建，整合 prompt 精简和 Anthropic API 格式

目标：
1. 产出可以直接接入"小说商业帝国"知识底座候选流的标准知识条目
2. 保留 Tier 1 / Tier 2 / Tier 3 的原始与规范化产物
3. 不再把 Tier 3 结果停留在"高质量摘要"，而是输出可审批、可回流、可消费的 candidate knowledge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import hashlib
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    from ebooklib import ITEM_DOCUMENT, epub
except Exception:
    ITEM_DOCUMENT = None
    epub = None


# ============================================================================
# 配置
# ============================================================================

DEFAULT_SOURCE_BASE = Path("D:/03-AI相关/99-AI撒欢区/03-claude/01-经典作品库_整理版")
DEFAULT_OUTPUT_BASE = Path("D:/03-AI相关/99-AI撒欢区/03-claude/02-经典作品蒸馏结果")
DEFAULT_MODEL = "glm-4-plus"
SCRIPT_VERSION = "distill-orchestrator-v4"
PROMPT_VERSION = "prompt-v4-simplified"


def load_env_file(env_path: Path) -> None:
    """加载 .env 文件到环境变量"""
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key and value and key not in os.environ:
                    os.environ[key] = value


# 加载 .env 文件（优先级低于系统环境变量）
load_env_file(Path(__file__).parent / ".env")

SOURCE_BASE = Path(os.environ.get("THREE_TIER_SOURCE_BASE", str(DEFAULT_SOURCE_BASE)))
OUTPUT_BASE = Path(os.environ.get("THREE_TIER_OUTPUT_BASE", str(DEFAULT_OUTPUT_BASE)))
GLM_API_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY") or ""
GLM_API_URL = os.environ.get(
    "GLM_API_URL",
    "https://open.bigmodel.cn/api/anthropic",
)
GLM_MODEL = os.environ.get("GLM_MODEL", DEFAULT_MODEL)
GLM_TEMPERATURE = float(os.environ.get("GLM_TEMPERATURE", "0.4"))
GLM_RESCUE_TEMPERATURE = float(os.environ.get("GLM_RESCUE_TEMPERATURE", "0.2"))
GLM_MAX_RETRIES = int(os.environ.get("GLM_MAX_RETRIES", "5"))
GLM_RETRY_BACKOFF_SECONDS = int(os.environ.get("GLM_RETRY_BACKOFF_SECONDS", "5"))
STRUCTURED_OUTPUT_RESCUES = int(os.environ.get("STRUCTURED_OUTPUT_RESCUES", "1"))

# Ollama 本地推理后端
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.25"))  # 默认用于 Tier2
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
OLLAMA_REPEAT_PENALTY = float(os.environ.get("OLLAMA_REPEAT_PENALTY", "1.15"))
# Tier 专属温度（9B 小模型建议分开设置）
OLLAMA_TIER2_TEMPERATURE = float(os.environ.get("OLLAMA_TIER2_TEMPERATURE", "0.15"))  # 一致性优先，压低
OLLAMA_TIER3_TEMPERATURE = float(os.environ.get("OLLAMA_TIER3_TEMPERATURE", "0.45"))  # 归纳升华需要创意
LLM_BACKEND = os.environ.get("LLM_BACKEND", "glm").lower()  # "glm" | "ollama"

# P2优化：蒸馏专用 system prompt，提前定义角色，消除模型角色混淆
DISTILL_SYSTEM_PROMPT = os.environ.get(
    "DISTILL_SYSTEM_PROMPT",
    "你是一个严谨、专业、高效的知识蒸馏助手。你的唯一任务是根据给定的原文内容，严格按照输出格式要求，提取或归纳候选知识条目。不要输出任何思考过程、解释或额外文字，只输出符合要求的 JSON。"
)

ROUND1_BOOKS = [
    ("00-\u6d4b\u8bd5", "\u300a\u6781\u54c1\u660e\u541b\u300b(\u6821\u5bf9\u7248\u5168\u672c)\u4f5c\u8005_\u6674\u4e86_txt -- e06754bdd30875e12a357afd6877e7af -- Anna\u2019s Archive.txt"),
    ("00-\u6d4b\u8bd5", "\u300a\u5343\u592b\u65a9\u300b(\u6821\u5bf9\u7248\u5168\u672c)\u4f5c\u8005_\u6674\u4e86_txt -- 64d1a6271cb9c7b4f5abcd94f23262a0 -- Anna\u2019s Archive.txt"),
]

# HTTP 连接池（提升并发效率）
_http_session: Optional[requests.Session] = None


def _get_http_session() -> requests.Session:
    """获取或创建 HTTP 会话（连接池复用）"""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=0,  # 我们自己处理重试
        )
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
    return _http_session

def scan_books_from_source(min_size_mb: float = 0) -> List[Tuple[str, str]]:
    """自动扫描源目录下所有 txt/epub 文件"""
    books = []
    if not SOURCE_BASE.exists():
        return books
    skipped = 0
    for genre_dir in sorted(SOURCE_BASE.iterdir()):
        if not genre_dir.is_dir():
            continue
        genre = genre_dir.name
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in (".txt", ".epub"):
                if min_size_mb > 0:
                    size_mb = f.stat().st_size / 1024 / 1024
                    if size_mb < min_size_mb:
                        skipped += 1
                        continue
                books.append((genre, f.name))
    if skipped:
        print(f"  跳过 {skipped} 本（小于 {min_size_mb}MB）")
    return books

SCHEMA_VERSION = "knowledge-candidate-v4"


def get_tier2_max_chapters(total_chapters: int) -> int:
    """高价值章节上限：短篇取30%，长篇上限80章，保留足够的叙事覆盖面"""
    return min(max(int(total_chapters * 0.3), 12), 80)
TIER2_SLEEP_SECONDS = 0
TIER3_SLEEP_SECONDS = 0
# Tier2 并发数：Ollama 本地推理延迟低，可设置更高并发
_TIER2_CONCURRENCY_ENV = os.environ.get("TIER2_CONCURRENCY", "")
if _TIER2_CONCURRENCY_ENV:
    TIER2_CONCURRENCY = int(_TIER2_CONCURRENCY_ENV)
else:
    # 默认值：Ollama 8并发，GLM 15并发（云端API有速率限制）
    TIER2_CONCURRENCY = 8 if LLM_BACKEND == "ollama" else 15

# Tier1 并发（章节分析）
_TIER1_CONCURRENCY_ENV = os.environ.get("TIER1_CONCURRENCY", "")
if _TIER1_CONCURRENCY_ENV:
    TIER1_CONCURRENCY = int(_TIER1_CONCURRENCY_ENV)
else:
    TIER1_CONCURRENCY = 4 if LLM_BACKEND == "ollama" else 4


# ============================================================================
# 模型常量
# ============================================================================

WAREHOUSE_MAP = {
    "结构仓": "结构仓",
    "角色仓": "角色仓",
    "设定仓": "设定仓",
    "表达仓": "表达仓",
    "商业仓": "商业仓",
    # 旧格式兼容
    "结构推进": "结构仓",
    "人物关系": "角色仓",
    "世界规则": "设定仓",
    "表达呈现": "表达仓",
    "读者效果": "商业仓",
}

WAREHOUSE_DEFAULT_DOMAIN = {
    "结构仓": "结构设计",
    "角色仓": "角色塑造",
    "设定仓": "世界设定",
    "表达仓": "表达与叙事",
    "商业仓": "商业表现",
}

QUALITY_AXES = {"稳定", "牵引", "沉浸", "感染", "分享"}
TARGET_TAGS = {"人物", "关系", "设定", "场景", "叙事", "商业", "结构", "情绪"}
STAGE_TAGS = {"开篇", "立主线", "中段", "转折", "高潮", "卷末", "回收", "终局"}
READER_EFFECT_TAGS = {"代入", "共鸣", "揪心", "爽感", "好奇", "压迫感", "惊喜", "心疼", "愤怒", "讨论", "传播"}
CONTROL_TAGS = {"必开", "可选", "慎用", "高频", "低频", "可自动化", "需人工确认", "禁自动修"}
RISK_TAGS = {"易跑偏", "易水文", "易失真", "易失速", "易崩设定", "易降沉浸", "易降留存", "易模板化", "易过拟合经典", "易伤文风"}

KNOWLEDGE_TYPE_ALIASES = {
    "template": "template",
    "模板": "template",
    "rule": "rule",
    "规则": "rule",
    "pattern": "pattern",
    "模式": "pattern",
    "archetype": "archetype",
    "原型": "archetype",
    "graph": "graph",
    "图谱": "graph",
    "ledger": "ledger",
    "账本": "ledger",
    "sample": "sample",
    "样本": "sample",
    "counter_example": "counter_example",
    "counter-example": "counter_example",
    "反例": "counter_example",
    "trigger": "trigger",
    "触发器": "trigger",
    "signal": "signal",
    "信号": "signal",
    "arc": "arc",
    "转移": "arc",
    "弧线": "arc",
    "转移 / 弧线": "arc",
}

LEVEL_ALIASES = {
    "book": "book",
    "书级": "book",
    "volume": "volume",
    "卷级": "volume",
    "story_segment": "story_segment",
    "剧情段级": "story_segment",
    "chapter": "chapter",
    "章级": "chapter",
    "scene": "scene",
    "场景级": "scene",
    "beat": "beat",
    "节拍级": "beat",
    "beat级": "beat",
    "paragraph": "paragraph",
    "段落级": "paragraph",
    "sentence": "sentence",
    "句子级": "sentence",
    "对白级": "sentence",
}

DEFAULT_USABLE_MODULES = ["启动包", "质量引擎", "运行台"]
DEFAULT_USABLE_CHECKPOINTS = [1, 2, 3, 4, 5, 6, 7]

CHAPTER_PATTERNS = [
    r"^第[零一二三四五六七八九十百千万两\d]+[章节回集卷部篇][^\n]*",
    r"^[第]?\d+[\.\、\s][^\n]*",
    r"^卷[零一二三四五六七八九十百千万两\d]+[^\n]*",
    r"^[零一二三四五六七八九十百千万两]+[、\.][^\n]*",
    r"^Chapter\s*\d+[^\n]*",
    r"^CHAPTER\s*\d+[^\n]*",
    r"^[序楔引子尾声后记终章][^\n]{0,30}$",
    r"^番外[^\n]*",
]

DIALOGUE_PATTERNS = [
    r'"([^"]{1,120})"',
    r"「([^」]{1,120})」",
    r"『([^』]{1,120})』",
    r"'([^']{1,120})'",
]

CHAPTER_TYPE_RULES = {
    "战斗": ["战斗", "厮杀", "对决", "交手", "斩", "杀", "血", "攻势", "突破"],
    "政治": ["朝堂", "权谋", "大臣", "军政", "奏折", "皇帝", "议和", "政局"],
    "日常": ["吃饭", "闲聊", "练功", "休息", "家常", "琐事", "街市"],
    "感情": ["喜欢", "心动", "情绪", "拥抱", "吻", "眼神", "心疼", "依赖"],
    "悬疑": ["谜", "真相", "秘密", "阴谋", "线索", "诡异", "不对劲", "调查"],
    "旅行": ["赶路", "驿站", "渡口", "山路", "车队", "行军", "远行"],
}

HOOK_TYPE_RULES = {
    "悬念式": ["为什么", "到底", "真相", "秘密", "谜", "忽然", "竟然"],
    "转折式": ["然而", "却", "反而", "没想到", "竟", "突然"],
    "承诺式": ["明日", "下次", "再来", "一定", "等着", "下一步"],
    "情感式": ["心疼", "难过", "不舍", "揪心", "沉默", "眼泪"],
}


@dataclass
class ChapterRecord:
    chapter_num: int = 0
    title: str = ""
    content: str = ""


# ============================================================================
# 通用工具
# ============================================================================

def now_iso() -> str:
    return datetime.now().isoformat()


def stable_slug(value: str) -> str:
    text = re.sub(r"[《》（）【】\[\]作者：:_\-\s]+", "-", value or "").strip("-")
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\-]+", "", text)
    return text[:80] or "untitled"


def stable_id(*parts: str) -> str:
    raw = "||".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{stable_slug(parts[0] if parts else 'knowledge')}-{digest}"


def dedupe_signature(item: Dict[str, Any]) -> str:
    blob = " ".join(
        [
            str(item.get("warehouse") or ""),
            str(item.get("domain") or ""),
            str(item.get("title") or ""),
            str(item.get("topic") or ""),
            clip_text(str(item.get("principle") or ""), 120),
            clip_text(str(item.get("mechanism") or ""), 120),
        ]
    )
    keywords = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", blob)
    return " ".join(unique_preserve(keywords)[:12]).lower()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}_",
        suffix=f"{path.suffix}.tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def unique_preserve(values: Iterable[Any]) -> List[Any]:
    seen = set()
    ordered: List[Any] = []
    for value in values:
        # 对 dict/list 做 json.dumps；简单类型直接 str 避免序列化开销
        if isinstance(value, (dict, list)):
            normalized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(value)
    return ordered


def clip_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_chapter_content_window(content: str, head_chars: int = 3000, tail_chars: int = 1500) -> str:
    """P2优化：构建叙事弧内容窗口——章节开头（建置/上升）+ 结尾（高潮/收束）

    掐头去尾各留中间，让模型同时看到"怎么开始"和"怎么收尾"，
    对判断章节类型、钩子效果、高潮设计至关重要。
    """
    content = str(content or "")
    total = len(content)
    if total <= head_chars + tail_chars:
        return content
    head = content[:head_chars]
    tail = content[-tail_chars:]
    return f"{head}\n\n【本章结尾段落】\n{tail}"


def sanitize_model_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_json_candidate(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return text
    text = text.replace("\ufeff", "")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text.strip()


def _json_loads_with_repair(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        repaired = _repair_json_candidate(raw_text)
        if repaired == raw_text:
            raise
        return json.loads(repaired)


def _find_brace_range(text: str, open_char: str, close_char: str) -> Tuple[int, int]:
    """Find the range of a balanced brace pair, returning (start, end) indices."""
    # Skip any leading close_chars (e.g., text starts with "} ... { ... }")
    safe_start = 0
    while safe_start < len(text) and text[safe_start] == close_char:
        safe_start += 1
    search_text = text[safe_start:]
    start = search_text.find(open_char)
    if start < 0:
        return -1, -1
    # Now search for the matching close_char from start+1
    depth = 0
    for i, ch in enumerate(search_text[start:]):
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return start + safe_start, start + safe_start + i + 1
    return start + safe_start, -1


def extract_json_array(raw_text: str) -> List[Dict[str, Any]]:
    raw_text = sanitize_model_text(raw_text)
    if not raw_text:
        return []
    start, end = _find_brace_range(raw_text, "[", "]")
    if end <= start:
        return []
    try:
        payload = _json_loads_with_repair(raw_text[start:end])
        return payload if isinstance(payload, list) else []
    except json.JSONDecodeError:
        return []


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    raw_text = sanitize_model_text(raw_text)
    if not raw_text:
        return {}
    start, end = _find_brace_range(raw_text, "{", "}")
    if end <= start:
        return {}
    try:
        payload = _json_loads_with_repair(raw_text[start:end])
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def safe_parse_response(raw_text: str) -> Dict[str, Any]:
    raw_text = sanitize_model_text(raw_text)
    if not raw_text:
        return {"items": [], "parse_mode": "empty"}

    parsed_object = extract_json_object(raw_text)
    if parsed_object:
        if isinstance(parsed_object.get("items"), list):
            return {"items": parsed_object.get("items") or [], "parse_mode": "json_object"}
        for fallback_key in ["data", "result", "knowledge_items", "knowledge"]:
            if isinstance(parsed_object.get(fallback_key), list):
                return {"items": parsed_object.get(fallback_key) or [], "parse_mode": f"json_object.{fallback_key}"}
        return {"items": [], "parse_mode": "json_object_without_items"}

    parsed_array = extract_json_array(raw_text)
    if parsed_array:
        return {"items": parsed_array, "parse_mode": "json_array"}

    return {"items": [], "parse_mode": "unparsed"}


def normalize_choice(value: Any, aliases: Dict[str, str], default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return aliases.get(text, aliases.get(text.lower(), default))


def normalize_warehouse(value: Any) -> str:
    text = str(value or "").strip()
    return WAREHOUSE_MAP.get(text, "结构仓")


def normalize_tags(values: Any, allowed: set[str], defaults: List[str]) -> List[str]:
    if isinstance(values, str):
        split_values = re.split(r"[、,，/\s]+", values)
    elif isinstance(values, list):
        split_values = [str(item) for item in values]
    else:
        split_values = []
    normalized = [item for item in (value.strip() for value in split_values) if item in allowed]
    if not normalized:
        normalized = list(defaults)
    return unique_preserve(normalized)


def infer_quality_axes(text_blob: str, warehouse: str) -> List[str]:
    text_blob = str(text_blob or "")
    axes: List[str] = []
    if any(key in text_blob for key in ["代入", "共鸣", "心疼", "情绪", "余韵", "感染", "沉浸"]):
        axes.extend(["沉浸", "感染"])
    if any(key in text_blob for key in ["钩子", "悬念", "追更", "回报", "上头", "节奏", "牵引"]):
        axes.append("牵引")
    if any(key in text_blob for key in ["一致", "逻辑", "稳定", "设定", "边界", "回收"]):
        axes.append("稳定")
    if any(key in text_blob for key in ["讨论", "传播", "分享"]):
        axes.append("分享")
    if not axes:
        if warehouse == "商业仓":
            axes = ["牵引", "分享"]
        elif warehouse == "表达仓":
            axes = ["沉浸", "感染"]
        elif warehouse == "角色仓":
            axes = ["沉浸"]
        else:
            axes = ["稳定"]
    return unique_preserve([axis for axis in axes if axis in QUALITY_AXES])


def infer_target_tags(text_blob: str, warehouse: str) -> List[str]:
    text_blob = str(text_blob or "")
    targets: List[str] = []
    if any(key in text_blob for key in ["角色", "主角", "反派", "配角", "人物"]):
        targets.append("人物")
    if any(key in text_blob for key in ["关系", "对手", "盟友", "站队", "情感线"]):
        targets.append("关系")
    if any(key in text_blob for key in ["设定", "规则", "世界观", "能力", "体系"]):
        targets.append("设定")
    if any(key in text_blob for key in ["场景", "名场面", "对峙", "爆发"]):
        targets.append("场景")
    if any(key in text_blob for key in ["叙事", "视角", "信息差", "悬念", "对白"]):
        targets.append("叙事")
    if any(key in text_blob for key in ["平台", "留存", "付费", "传播", "读者"]):
        targets.append("商业")
    if any(key in text_blob for key in ["结构", "主线", "卷级", "节奏", "回收"]):
        targets.append("结构")
    if any(key in text_blob for key in ["情绪", "压迫感", "爽感", "揪心", "心疼"]):
        targets.append("情绪")
    if not targets:
        defaults = {
            "结构仓": ["结构"],
            "角色仓": ["人物", "关系"],
            "设定仓": ["设定"],
            "表达仓": ["叙事", "情绪"],
            "商业仓": ["商业", "结构"],
        }
        targets = defaults.get(warehouse, ["结构"])
    return unique_preserve([tag for tag in targets if tag in TARGET_TAGS])


def infer_stage_tags(text_blob: str, default_stage: str = "中段") -> List[str]:
    text_blob = str(text_blob or "")
    inferred: List[str] = []
    for stage in STAGE_TAGS:
        if stage in text_blob:
            inferred.append(stage)
    if not inferred:
        inferred = [default_stage]
    return unique_preserve(inferred)


def infer_reader_effect_tags(text_blob: str) -> List[str]:
    text_blob = str(text_blob or "")
    inferred = [tag for tag in READER_EFFECT_TAGS if tag in text_blob]
    if not inferred:
        if any(key in text_blob for key in ["悬念", "好奇"]):
            inferred.append("好奇")
        if any(key in text_blob for key in ["追更", "讨论"]):
            inferred.extend(["讨论", "传播"])
        if any(key in text_blob for key in ["心疼", "揪心", "共鸣", "代入"]):
            inferred.extend(["代入", "共鸣"])
    return unique_preserve(inferred or ["好奇"])


def infer_control_tags(knowledge_type: str, quality_axes: List[str]) -> List[str]:
    tags = ["需人工确认"]
    if knowledge_type in {"rule", "signal", "trigger"}:
        tags.append("必开")
    else:
        tags.append("可选")
    if "牵引" in quality_axes or "沉浸" in quality_axes:
        tags.append("高频")
    return unique_preserve([tag for tag in tags if tag in CONTROL_TAGS])


def infer_risk_tags(text_blob: str) -> List[str]:
    text_blob = str(text_blob or "")
    inferred = [tag for tag in RISK_TAGS if tag in text_blob]
    if not inferred:
        if "节奏" in text_blob or "失速" in text_blob:
            inferred.append("易失速")
        if "模板" in text_blob:
            inferred.append("易模板化")
        if "沉浸" in text_blob:
            inferred.append("易降沉浸")
    return unique_preserve(inferred or ["易模板化"])


def infer_usable_modules(warehouse: str, quality_axes: List[str]) -> List[str]:
    modules = ["启动包", "质量引擎"]
    if warehouse in {"商业仓", "结构仓"}:
        modules.append("运行台")
    if "沉浸" in quality_axes or "牵引" in quality_axes:
        modules.append("运行台")
    return unique_preserve(modules)


def infer_usable_checkpoints(stage_tags: List[str]) -> List[int]:
    checkpoints = {5, 6, 7}
    if "开篇" in stage_tags:
        checkpoints.update({2, 3, 4})
    if "卷末" in stage_tags or "高潮" in stage_tags:
        checkpoints.update({5, 6, 7})
    if "立主线" in stage_tags:
        checkpoints.add(1)
    return sorted(checkpoints)


def build_data_sources(
    *,
    tier: int,
    book_name: str,
    chapter_num: Optional[int] = None,
    chapter_title: str = "",
    source_files: Optional[List[str]] = None,
    source_fields: Optional[List[str]] = None,
    source_excerpt: str = "",
    stats_snapshot: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    sources = []
    for file_name in (f for f in (source_files or []) if f):
        sources.append(
            {
                "tier": tier,
                "file": file_name,
                "fields": list(source_fields or []),
                "book": book_name,
                "chapter_num": chapter_num,
                "chapter_title": chapter_title,
                "source_excerpt": clip_text(source_excerpt, 180),
                "stats_snapshot": deepcopy(stats_snapshot or {}),
            }
        )
    return sources


def validate_candidate_item(item: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    required_text_fields = ["id", "warehouse", "title", "principle", "mechanism", "application", "source_type", "source_work", "status"]
    for field in required_text_fields:
        if not str(item.get(field) or "").strip():
            errors.append(f"missing:{field}")

    if item.get("knowledge_type") not in KNOWLEDGE_TYPE_ALIASES.values():
        errors.append("invalid:knowledge_type")
    if item.get("level") not in LEVEL_ALIASES.values():
        errors.append("invalid:level")

    confidence = safe_float(item.get("confidence"), -1)
    if not (0.0 <= confidence <= 1.0):
        errors.append("invalid:confidence")
    elif confidence < 0.55:
        warnings.append("low_confidence")

    if not item.get("quality_axes"):
        errors.append("missing:quality_axes")
    if not item.get("targets"):
        errors.append("missing:targets")
    if not item.get("stages"):
        errors.append("missing:stages")
    if not item.get("usable_modules"):
        errors.append("missing:usable_modules")
    if not item.get("usable_checkpoints"):
        errors.append("missing:usable_checkpoints")
    elif not all(isinstance(value, int) and 1 <= value <= 7 for value in item.get("usable_checkpoints", [])):
        errors.append("invalid:usable_checkpoints")

    evidence = item.get("evidence") or []
    if not evidence:
        errors.append("missing:evidence")
    elif all(len(str(value).strip()) < 12 for value in evidence):
        warnings.append("weak_evidence")

    if len(str(item.get("principle") or "").strip()) < 30:
        warnings.append("short_principle")
    if len(str(item.get("mechanism") or "").strip()) < 30:
        warnings.append("short_mechanism")
    if len(str(item.get("application") or "").strip()) < 10:
        warnings.append("short_application")

    # P2优化：检测泛化/模板化输出，防止模型生成"万能废话"
    generic_patterns = [
        "生动形象", "细腻刻画", "深入人心", "引人入胜", "跌宕起伏",
        "精彩绝伦", "扣人心弦", "高潮迭起", "层层递进", "首尾呼应",
        "通过……来……", "运用……手法", "采用……方式",
        "增强了.*感染力", "提升了.*可读性", "展现了.*魅力",
        "使读者.*", "让读者.*", "令读者.*",
    ]
    principle_text = str(item.get("principle") or "")
    for pattern in generic_patterns:
        if re.search(pattern, principle_text):
            warnings.append("generic_principle")
            break

    data_sources = item.get("data_sources") or []
    if not data_sources:
        warnings.append("missing_data_sources")

    status = "accepted"
    if errors:
        status = "rejected"
    elif warnings:
        status = "repairable"

    return {
        "item_id": item.get("id"),
        "title": item.get("title"),
        "status": status,
        "errors": errors,
        "warnings": warnings,
    }


def split_validated_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    accepted: List[Dict[str, Any]] = []
    repairable: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    validations: List[Dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    warning_reasons: Counter[str] = Counter()

    for item in items:
        validation = validate_candidate_item(item)
        validations.append(validation)
        for error in validation["errors"]:
            rejection_reasons[error] += 1
        for warning in validation["warnings"]:
            warning_reasons[warning] += 1

        if validation["status"] == "accepted":
            accepted.append(item)
        elif validation["status"] == "repairable":
            repairable.append(item)
            item.setdefault("review_context", {})["validation_warnings"] = list(validation["warnings"])
        else:
            rejected.append(item)

    return {
        "accepted_items": accepted,
        "repairable_items": repairable,
        "rejected_items": rejected,
        "validations": validations,
        "rejection_reasons": dict(rejection_reasons),
        "warning_reasons": dict(warning_reasons),
    }


def build_quality_report(*, stage_name: str, items: List[Dict[str, Any]], validation_bundle: Dict[str, Any], dedupe_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    accepted = validation_bundle["accepted_items"]
    repairable = validation_bundle["repairable_items"]
    rejected = validation_bundle["rejected_items"]
    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "stage_name": stage_name,
        "total_items": len(items),
        "accepted_count": len(accepted),
        "repairable_count": len(repairable),
        "rejected_count": len(rejected),
        "rejection_reasons": validation_bundle["rejection_reasons"],
        "warning_reasons": validation_bundle["warning_reasons"],
        "warehouse_distribution": dict(Counter(item.get("warehouse", "未知") for item in accepted + repairable)),
        "knowledge_type_distribution": dict(Counter(item.get("knowledge_type", "unknown") for item in accepted + repairable)),
        "level_distribution": dict(Counter(item.get("level", "unknown") for item in accepted + repairable)),
        "tag_distribution": {
            "quality_axes": dict(Counter(tag for item in accepted + repairable for tag in item.get("quality_axes", []))),
            "targets": dict(Counter(tag for item in accepted + repairable for tag in item.get("targets", []))),
            "stages": dict(Counter(tag for item in accepted + repairable for tag in item.get("stages", []))),
        },
        "low_confidence_count": len([item for item in accepted + repairable if safe_float(item.get("confidence")) < 0.55]),
        "dedupe_report": dedupe_report or {},
        "generated_at": now_iso(),
    }


# ============================================================================
# GLM 调用 (Anthropic 兼容格式)
# ============================================================================

def require_api_key() -> None:
    if not GLM_API_KEY:
        raise RuntimeError("缺少 API Key 环境变量，请设置后再运行。")


def _setup_proxy_bypass() -> None:
    """Windows 系统设置 NO_PROXY 环境变量以避免代理干扰 API 调用"""
    if sys.platform == "win32":
        os.environ["NO_PROXY"] = "*"


GLM_TIMEOUT = int(os.environ.get("GLM_TIMEOUT", "300"))


def _normalize_glm_api_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1/messages"):
        return url
    if url.endswith("/api/anthropic"):
        return f"{url}/v1/messages"
    return url


def _extract_glm_text(result: Dict[str, Any]) -> str:
    """兼容 Anthropic 风格 content block 的多种返回结构。"""
    if result.get("success") is False:
        raise ValueError(f"GLM API error: {result.get('msg') or result}")
    content = result.get("content")
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif isinstance(block, str) and block.strip():
                parts.append(block)
        if parts:
            return "\n".join(parts)
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(result.get("output_text"), str) and result["output_text"].strip():
        return result["output_text"]
    raise ValueError(f"unexpected GLM response schema: {list(result.keys())}")


def _sleep_before_retry(retry: int, *, base_seconds: int | None = None) -> None:
    base = GLM_RETRY_BACKOFF_SECONDS if base_seconds is None else base_seconds
    # 指数退避：1s, 2s, 4s, 8s, 16s（最多等待 base * 2^retry）
    time.sleep(base * min(2 ** retry, 16))


def _call_glm_raw(prompt: str, *, max_tokens: int = 4000, temperature: Optional[float] = None, system_prompt: Optional[str] = None) -> Optional[str]:
    """GLM Anthropic 兼容格式调用"""
    _setup_proxy_bypass()
    request_url = _normalize_glm_api_url(GLM_API_URL)
    headers = {
        "x-api-key": GLM_API_KEY,
        "Content-Type": "application/json",
    }
    # P2优化：GLM API不支持system role，将system prompt合并到user message
    messages = []
    if system_prompt:
        # GLM不支持system role，改用user message承载
        combined_content = f"{system_prompt}\n\n{prompt}"
        messages.append({"role": "user", "content": combined_content})
    else:
        messages.append({"role": "user", "content": prompt})
    payload = {
        "model": str(GLM_MODEL).strip(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": GLM_TEMPERATURE if temperature is None else temperature,
    }
    session = _get_http_session()
    for retry in range(GLM_MAX_RETRIES):
        try:
            response = session.post(request_url, headers=headers, json=payload, timeout=(20, GLM_TIMEOUT))
            if response.status_code == 200:
                result = response.json()
                return _extract_glm_text(result)
            response_text = response.text[:300]
            retryable = response.status_code in {408, 409, 429, 500, 502, 503, 504}
            if retryable and retry < GLM_MAX_RETRIES - 1:
                print(f"      API transient error (retry {retry + 1}/{GLM_MAX_RETRIES}): {response.status_code} {response_text}")
                _sleep_before_retry(retry)
                continue
            print(f"      API error: {response.status_code} {response_text}")
        except requests.exceptions.Timeout as exc:
            if retry < GLM_MAX_RETRIES - 1:
                print(f"      API timeout (retry {retry + 1}/{GLM_MAX_RETRIES}): {exc}")
                _sleep_before_retry(retry)
            else:
                print(f"      API timeout after {GLM_MAX_RETRIES} retries: {exc}")
        except Exception as exc:  # noqa: BLE001
            if retry < GLM_MAX_RETRIES - 1:
                print(f"      API exception (retry {retry + 1}/{GLM_MAX_RETRIES}): {exc}")
                _sleep_before_retry(retry, base_seconds=3)
            else:
                print(f"      API failed: {exc}")
    return None


_ollama_last_health_check = 0.0
_ollama_healthy = True
OLLAMA_HEALTH_INTERVAL = 60  # 每 60 秒检查一次健康状态


def _check_ollama_health() -> bool:
    """检查 Ollama 服务和模型是否可用（服务在线 + 模型已注册）"""
    try:
        session = _get_http_session()
        r = session.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        if r.status_code != 200:
            print(f"      [WARN] Ollama 返回 {r.status_code}")
            return False
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if OLLAMA_MODEL not in models and not any(OLLAMA_MODEL.split(":")[0] in m for m in models):
            print(f"      [WARN] 模型 '{OLLAMA_MODEL}' 未找到，可用: {models}")
            return False
        return True
    except Exception as exc:
        print(f"      [WARN] Ollama 连接异常: {exc}")
        return False


def _call_ollama_raw(prompt: str, *, max_tokens: int = 4000, temperature: Optional[float] = None, system_prompt: Optional[str] = None) -> Optional[str]:
    """Ollama OpenAI 兼容格式调用，带健康检查和自动重启"""
    global _ollama_last_health_check, _ollama_healthy

    # 定期健康检查
    now = time.time()
    if now - _ollama_last_health_check > OLLAMA_HEALTH_INTERVAL:
        _ollama_last_health_check = now
        if not _check_ollama_health():
            _ollama_healthy = False
            print("      [INFO] Ollama 健康检查失败，尝试重启...")
            import subprocess
            subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True, timeout=5)
            time.sleep(3)
            subprocess.Popen(["ollama", "serve"], creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(8)
            if _check_ollama_health():
                _ollama_healthy = True
                print("      [INFO] Ollama 重启成功")
            else:
                print("      [WARN] Ollama 重启后仍不可用，将继续尝试请求")

    url = f"{OLLAMA_BASE_URL}/api/chat"
    # P2优化：支持 system_prompt，提前锁定角色，减少模型"幻觉"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "format": "json",  # 强制 JSON 输出，减少非 JSON 文本混入
        "temperature": OLLAMA_TEMPERATURE if temperature is None else temperature,
        "num_ctx": OLLAMA_NUM_CTX,
        "repeat_penalty": OLLAMA_REPEAT_PENALTY,
    }
    session = _get_http_session()
    for retry in range(3):
        try:
            response = session.post(url, json=payload, timeout=(10, 120))  # connect 10s, read 120s
            if response.status_code == 200:
                result = response.json()
                return result["message"]["content"]
            print(f"      Ollama error: {response.status_code} {response.text[:300]}")
            if response.status_code == 404:
                print(f"      [FATAL] Model '{OLLAMA_MODEL}' not found, aborting retries.")
                return None
        except requests.exceptions.ConnectionError:
            print(f"      Ollama connection failed (retry {retry+1}/3), waiting {2**retry}s...")
            time.sleep(min(2 ** retry, 30))
        except requests.exceptions.Timeout:
            print(f"      Ollama timeout (retry {retry+1}/3), waiting {2**retry}s...")
            time.sleep(min(2 ** retry, 30))
        except Exception as exc:  # noqa: BLE001
            print(f"      Ollama error (retry {retry+1}/3): {exc}")
            if retry < 2:
                time.sleep(min(2 ** retry, 30))
    print(f"      Ollama failed after 3 retries")
    return None


def call_glm_api(prompt: str, *, max_tokens: int = 4000, temperature: Optional[float] = None, system_prompt: Optional[str] = None) -> Optional[str]:
    """统一 LLM 调用入口，根据 LLM_BACKEND 路由"""
    if LLM_BACKEND == "ollama":
        return _call_ollama_raw(prompt, max_tokens=max_tokens, temperature=temperature, system_prompt=system_prompt)
    return _call_glm_raw(prompt, max_tokens=max_tokens, temperature=temperature, system_prompt=system_prompt)


# ============================================================================
# Tier 1
# ============================================================================

def read_epub_book(book_path: Path) -> Optional[str]:
    if epub is None or BeautifulSoup is None:
        return None
    try:
        book = epub.read_epub(str(book_path))
    except Exception:
        return None
    chunks: List[str] = []
    for item in book.get_items():
        if ITEM_DOCUMENT is None or item.get_type() != ITEM_DOCUMENT:
            continue
        file_name = str(getattr(item, "file_name", "") or "")
        if any(f in file_name.lower() for f in ("nav", "toc", "cover")):
            continue
        try:
            html = item.get_body_content()
        except Exception:
            try:
                html = item.get_content()
            except Exception:
                continue
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > 80:
            chunks.append(text)
    content = "\n\n".join(chunks).strip()
    return content or None


def read_book(book_path: Path) -> Optional[str]:
    file_size_mb = book_path.stat().st_size / 1024 / 1024
    if str(book_path.suffix).lower() == ".epub":
        content = read_epub_book(book_path)
        if content and len(content) > 1000:
            print(f"    EPUB: {len(content)} chars")
            return content
    encodings = ["utf-8", "gb18030", "gbk", "gb2312", "big5", "utf-16", "cp936"]
    for encoding in encodings:
        try:
            # 不用 errors='replace'，让编码错误直接抛异常，触发下一个编码的尝试
            content = book_path.read_text(encoding=encoding)
            if len(content) > 1000:
                print(f"    Encoding: {encoding}, {len(content)} chars")
                if file_size_mb > 15:
                    print(f"    [INFO] 文件较大 ({file_size_mb:.1f}MB)")
                return content
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:  # noqa: BLE001
            continue
    return None


def split_chapters_streaming(book_path: Path, max_chars: int = 0) -> List[ChapterRecord]:
    """流式分章：逐行读取，不一次性加载全文。max_chars=0 表示不限。"""
    # epub 文件需要专用解析器，不能当文本文件读
    if book_path.suffix.lower() == ".epub":
        content = read_epub_book(book_path)
        if content:
            print(f"    EPUB 流式解析: {len(content)} chars")
            chapters = split_chapters(content)
            if chapters:
                print(f"    分章完成: {len(chapters)} 章")
                return chapters
            # 有内容但分章为0，走普通文本路径尝试
            print("    EPUB 解析成功但未检测到章节，尝试文本模式...")
        else:
            print("    EPUB 解析失败，尝试文本模式...")
        # fall through to text-based streaming
    encodings = ["utf-8", "gb18030", "gbk", "gb2312", "big5", "utf-16", "cp936"]
    combined_pattern = re.compile("|".join(f"(?:{pattern})" for pattern in CHAPTER_PATTERNS), re.MULTILINE)
    chapters: List[ChapterRecord] = []
    current_title = "开头"
    current_lines: List[str] = []
    chapter_num = 0
    total_chars = 0

    for encoding in encodings:
        try:
            with open(book_path, "r", encoding=encoding) as fh:
                for line in fh:
                    # 严格编码：遇到解码错误则尝试下一个编码
                    if max_chars > 0 and total_chars >= max_chars:
                        break
                    total_chars += len(line)
                    stripped = line.strip()
                    if stripped and combined_pattern.match(stripped):
                        content = "\n".join(current_lines).strip()
                        if content:
                            chapters.append(ChapterRecord(chapter_num=chapter_num, title=current_title[:80], content=content))
                        chapter_num += 1
                        current_title = stripped[:80]
                        current_lines = []
                        continue
                    current_lines.append(line)
            # Flush last chapter
            content = "\n".join(current_lines).strip()
            if content:
                chapters.append(ChapterRecord(chapter_num=chapter_num, title=current_title[:80], content=content))
            print(f"    流式分章: {encoding}, {len(chapters)} 章, {total_chars} chars")
            # 编码正确但分章为 0 说明文件格式不符合章节模式，这也是成功只是无章节
            return chapters
        except (UnicodeDecodeError, UnicodeError):
            continue  # 编码错误，尝试下一个
        except Exception:  # noqa: BLE001
            continue
    return []


def split_chapters(text: str) -> List[ChapterRecord]:
    # 用 io.StringIO 替代 splitlines()，避免一次性创建数十万行的列表
    import io
    combined_pattern = re.compile("|".join(f"(?:{pattern})" for pattern in CHAPTER_PATTERNS), re.MULTILINE)
    chapters: List[ChapterRecord] = []
    current_title = "开头"
    current_lines: List[str] = []
    chapter_num = 0

    for line in io.StringIO(text):
        stripped = line.strip()
        if stripped and combined_pattern.match(stripped):
            content = "\n".join(current_lines).strip()
            if content:
                chapters.append(ChapterRecord(chapter_num=chapter_num, title=current_title[:80], content=content))
            chapter_num += 1
            current_title = stripped[:80]
            current_lines = []
            continue
        current_lines.append(line)

    final_content = "\n".join(current_lines).strip()
    if final_content:
        chapters.append(ChapterRecord(chapter_num=chapter_num, title=current_title[:80], content=final_content))

    return [chapter for chapter in chapters if len(chapter.content) > 300]


def count_dialogues(content: str) -> int:
    total = 0
    for pattern in DIALOGUE_PATTERNS:
        total += len(re.findall(pattern, content))
    return total


def estimate_emotion_axes(content: str) -> Dict[str, float]:
    # P2优化：大幅扩展情绪词库（30词→200+词），提升章节情感定位精度
    lexicon = {
        "positive": [
            "笑", "喜", "悦", "欢", "乐", "轻松", "愉快", "开心", "快活", "雀跃",
            "温暖", "安心", "满足", "欣慰", "舒心", "惬意", "舒适", "安逸",
            "希望", "曙光", "转机", "生机", "盼头", "期待", "憧憬",
            "感动", "触动", "鼻酸", "眼眶湿润", "热泪盈眶",
            "甜蜜", "幸福", "美满", "团圆", "和解", "释然", "轻松", "治愈",
        ],
        "negative": [
            "痛", "疼", "苦", "难受", "难熬", "煎熬", "折磨", "摧残",
            "恨", "怨", "憎", "厌恶", "恶心", "作呕", "愤怒", "怒火", "暴怒",
            "怕", "恐惧", "害怕", "惊", "畏", "惶恐", "颤抖", "发抖",
            "绝望", "死寂", "无望", "万念俱灰", "心如死灰",
            "崩溃", "垮", "瘫", "碎", "裂",
            "屈辱", "羞辱", "羞耻", "脸红", "无地自容",
            "悲伤", "悲痛", "哀伤", "凄凉", "惨淡", "萧索",
            "沉重", "压抑", "沉闷", "郁结", "憋闷",
        ],
        "tension": [
            "杀", "斩", "劈", "砍", "血", "亡", "死", "尸", "亡命",
            "对决", "决战", "激战", "苦战", "血战", "缠斗", "厮杀",
            "追击", "逃", "奔逃", "逃亡", "奔命", "追赶",
            "危机", "险境", "生死存亡", "千钧一发", "九死一生",
            "压迫", "压制", "笼罩", "窒息", "紧迫", "急迫",
            "硬撑", "强撑", "咬牙", "撑住", "死撑",
            "危险", "凶险", "险恶", "惊险",
            "反击", "逆袭", "翻盘", "绝地反击", "反杀",
            "对抗", "对峙", "僵持", "胶着",
            "阴谋", "阳谋", "算计", "布局", "陷阱", "圈套",
        ],
        "suspense": [
            "秘密", "隐情", "隐衷", "隐私", "隐秘",
            "真相", "实情", "内幕", "隐情", "玄机",
            "谜", "谜团", "谜题", "悬念", "疑云",
            "阴谋", "黑手", "幕后", "主谋", "真相",
            "不对劲", "异样", "奇怪", "蹊跷", "反常", "异常",
            "线索", "痕迹", "迹象", "端倪", "蛛丝马迹",
            "诡异", "诡异", "阴森", "古怪", "邪门",
            "调查", "追查", "探寻", "追究", "追问",
            "暴露", "败露", "揭开", "浮出水面",
        ],
        "release": [
            "回报", "回馈", "收获", "成果",
            "揭晓", "公布", "公开", "揭露", "揭示",
            "成功", "胜利", "大胜", "凯旋", "旗开得胜",
            "击败", "打败", "战胜", "压倒", "碾压",
            "拥抱", "相拥", "搂住", "抱紧", "依偎",
            "释然", "放下", "解脱", "轻松", "释怀",
            "兑现", "实现", "完成", "达成", "如愿",
            "报仇", "复仇", "雪恨", "讨回", "清算",
            "翻盘", "逆袭", "逆转", "反杀",
            "加冕", "登基", "称王", "成神",
        ],
    }
    scores: Dict[str, float] = {}
    denominator = max(len(content), 1)
    for axis, words in lexicon.items():
        hit_count = sum(content.count(word) for word in words)
        scores[axis] = round(min(1.0, hit_count / max(denominator / 250.0, 1)), 3)
    return scores


def classify_chapter_type(content: str, dialogue_rate: float, emotion_axes: Dict[str, float]) -> str:
    scores = {}
    for chapter_type, keywords in CHAPTER_TYPE_RULES.items():
        scores[chapter_type] = sum(content.count(keyword) for keyword in keywords)
    if dialogue_rate > 0.12:
        scores["日常"] += 2
        scores["感情"] += 1
    scores["战斗"] += int(emotion_axes["tension"] * 10)
    scores["悬疑"] += int(emotion_axes["suspense"] * 10)
    scores["感情"] += int((emotion_axes["positive"] + emotion_axes["negative"]) * 6)
    return max(scores, key=scores.get)


def infer_hook_type(content: str, emotion_axes: Dict[str, float]) -> Tuple[str, float]:
    tail = content[-320:]
    scores = {hook_type: sum(tail.count(keyword) for keyword in keywords) for hook_type, keywords in HOOK_TYPE_RULES.items()}
    scores["悬念式"] += int(emotion_axes["suspense"] * 5)
    scores["情感式"] += int((emotion_axes["positive"] + emotion_axes["negative"]) * 3)
    scores["承诺式"] += 1 if "明天" in tail or "下一章" in tail else 0
    hook_type = max(scores, key=scores.get)
    raw_strength = scores[hook_type] / 8.0
    return hook_type, round(min(1.0, max(0.05, raw_strength)), 3)


def extract_key_dialogues(content: str, limit: int = 5) -> List[str]:
    dialogues: List[str] = []
    for pattern in DIALOGUE_PATTERNS:
        for match in re.findall(pattern, content):
            text = clip_text(match, 60)
            if len(text) >= 8:
                dialogues.append(text)
    return unique_preserve(dialogues)[:limit]


def extract_potential_characters(content: str, limit: int = 100) -> List[str]:
    pattern = re.compile(r"([\u4e00-\u9fa5]{2,4})(?:说|道|问|笑|看着|想着|点头|摇头|喊道|怒道|叹道|哭道)")
    names = [match.group(1) for match in pattern.finditer(content)]
    filtered = [name for name in names if name not in {"自己", "众人", "对方", "其中", "如果", "大家", "那人", "那人"}]
    return unique_preserve(filtered)[:limit]


def build_character_cooccurrence(chapters: List[ChapterRecord]) -> Dict[str, Dict[str, int]]:
    matrix: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for chapter in chapters:
        # P2优化：从每章提取更多角色（12→30），构建更完整的人物共现图
        characters = extract_potential_characters(chapter.content, limit=30)
        unique_characters = unique_preserve(characters)
        for i, left in enumerate(unique_characters):
            for right in unique_characters[i + 1 :]:
                matrix[left][right] += 1
                matrix[right][left] += 1
    return {name: dict(targets) for name, targets in matrix.items()}


def extract_plot_keywords(chapters: List[ChapterRecord]) -> Dict[str, List[int]]:
    keyword_map: Dict[str, List[int]] = defaultdict(list)
    keywords = [
        "真相", "秘密", "反击", "站队", "复仇", "权谋", "情绪", "回报", "名场面",
        "误会", "联盟", "背叛", "试探", "线索", "回收", "摊牌", "压迫", "成长",
    ]
    for chapter in chapters:
        for keyword in keywords:
            if keyword in chapter.content:
                keyword_map[keyword].append(chapter.chapter_num)
    return {keyword: positions for keyword, positions in keyword_map.items()}


def tier1_analyze(text: str, book_name: str) -> Tuple[Dict[str, Any], List[ChapterRecord], List[Dict[str, Any]]]:
    print("    [Tier 1] 分析中...")
    chapters = split_chapters(text)
    return _tier1_analyze_from_chapters(chapters, book_name)


def _tier1_analyze_from_chapters(chapters: List[ChapterRecord], book_name: str) -> Tuple[Dict[str, Any], List[ChapterRecord], List[Dict[str, Any]]]:
    """Tier1 分析（接受已分好的 chapters，流式模式用）"""
    print(f"    [Tier 1] 分析中... ({len(chapters)} 章)")
    chapter_stats: List[Dict[str, Any]] = []

    for chapter in chapters:
        word_count = len(chapter.content)
        dialogue_count = count_dialogues(chapter.content)
        dialogue_rate = round(dialogue_count / max(word_count, 1), 4)
        emotion_axes = estimate_emotion_axes(chapter.content)
        chapter_type = classify_chapter_type(chapter.content, dialogue_rate, emotion_axes)
        hook_type, hook_strength = infer_hook_type(chapter.content, emotion_axes)
        chapter_stats.append(
            {
                "chapter_num": chapter.chapter_num,
                "title": chapter.title,
                "word_count": word_count,
                "dialogue_count": dialogue_count,
                "dialogue_rate": dialogue_rate,
                "emotion_axes": emotion_axes,
                "chapter_type": chapter_type,
                "hook_type": hook_type,
                "hook_strength": hook_strength,
                "scene_switch_count": chapter.content.count("\n\n"),
                "key_dialogues": extract_key_dialogues(chapter.content),
            }
        )

    scored_stats = sorted(
        chapter_stats,
        key=lambda item: (
            safe_float(item["emotion_axes"].get("tension"))
            + safe_float(item["emotion_axes"].get("suspense"))
            + safe_float(item.get("hook_strength"))
        ),
        reverse=True,
    )
    max_hv = get_tier2_max_chapters(len(chapters))
    high_value = scored_stats[: min(max_hv, len(scored_stats))]
    character_map = build_character_cooccurrence(chapters)
    plot_keywords = extract_plot_keywords(chapters)
    # P2优化：扩展到全章节（原来只取前50章），提取更多角色
    full_content = "\n".join(ch.content for ch in chapters)
    potential_characters = extract_potential_characters(full_content[:300000])

    overview = {
        "book_name": book_name,
        "total_chapters": len(chapters),
        "total_words": sum(len(ch.content) for ch in chapters),
        "avg_dialogue_rate": round(
            sum(item["dialogue_rate"] for item in chapter_stats) / max(len(chapter_stats), 1),
            4,
        ),
        "dominant_chapter_types": Counter(item["chapter_type"] for item in chapter_stats).most_common(5),
        "dominant_hook_types": Counter(item["hook_type"] for item in chapter_stats).most_common(5),
        "potential_characters": potential_characters,
        "created_at": now_iso(),
    }

    result = {
        "schema_version": SCHEMA_VERSION,
        "book_name": book_name,
        "overview": overview,
        "chapter_stats": chapter_stats,
        "high_value_chapters": [
            {
                "chapter_num": item["chapter_num"],
                "title": item["title"],
                "reason": f"tension={item['emotion_axes']['tension']} suspense={item['emotion_axes']['suspense']} hook={item['hook_type']}",
            }
            for item in high_value
        ],
        "character_cooccurrence": character_map,
        "plot_keywords": plot_keywords,
        "analyzed_at": now_iso(),
    }
    print(f"      总章节: {len(chapters)}, 高价值章节: {len(high_value)}")
    return result, chapters, high_value


# ============================================================================
# 标准知识条目规范化
# ============================================================================

def build_tier2_prompt(
    book_name: str,
    chapter_num: int,
    chapter_title: str,
    content: str,
    chapter_stats: Dict[str, Any],
    *,
    book_overview: Optional[Dict[str, Any]] = None,
    plot_keywords: Optional[Dict[str, List[int]]] = None,
) -> str:
    """构建 Tier2 prompt：加入 CoT 引导、全书上下文、内容扩展到 6000 字符"""

    # 全书上下文（来自 Tier1）
    total_chapters = book_overview.get("total_chapters", 0) if book_overview else 0
    potential_chars = book_overview.get("potential_characters", []) if book_overview else []
    dominant_types = book_overview.get("dominant_chapter_types", []) if book_overview else []
    dominant_hooks = book_overview.get("dominant_hook_types", []) if book_overview else []

    # 判断章节在全书的位置
    if total_chapters > 0:
        pct = chapter_num / total_chapters
        if pct < 0.15:
            book_position = "开篇阶段（前15%）"
        elif pct < 0.4:
            book_position = "前期（15%-40%）"
        elif pct < 0.7:
            book_position = "中段（40%-70%）"
        elif pct < 0.9:
            book_position = "后期（70%-90%）"
        else:
            book_position = "终局阶段（90%以上）"
    else:
        book_position = "未知"

    chars_str = ", ".join(potential_chars[:20]) if potential_chars else "未知"
    types_str = ", ".join([t[0] for t in dominant_types[:3]]) if dominant_types else "未知"
    hooks_str = ", ".join([h[0] for h in dominant_hooks[:3]]) if dominant_hooks else "未知"

    # 剧情关键词上下文
    kw_context = ""
    if plot_keywords:
        active_kws = [kw for kw, chapters in plot_keywords.items() if chapter_num in chapters]
        if active_kws:
            kw_context = f"\n本章涉及剧情关键词：{', '.join(active_kws[:8])}"

    # P0优化：内容从3000扩展到6000字符，覆盖更完整叙事弧
    # P2优化：增加 CoT 思维链引导，帮助9B小模型先思考再输出
    return f"""
【角色扮演】你是一位严格执行"小说知识底座规范"的网文蒸馏专家。

【任务】从单章内容中提取 3-5 条高质量"候选知识条目"，每条都能直接进入审核/批准/回流流程。

【Chain-of-Thought 引导】（先思考，再输出）
Step 1 - 判断仓库：这一章最核心的知识属于哪个仓库？（结构仓=叙事推进/节奏，角色仓=人物塑造/关系，设定仓=世界观/规则，表达仓=叙事技巧/文笔，商业仓=读者体验/传播）
Step 2 - 判断粒度：这个知识是章节级、场景级还是节拍级？
Step 3 - 提炼principle：为什么这个写法/设计对读者有效？背后的核心原理是什么？
Step 4 - 提炼mechanism：具体怎么操作？有哪些关键细节？
Step 5 - 输出JSON：按格式严格输出，不要输出思考过程本身。

【全书上下文】
- 书名：{book_name}
- 全书共 {total_chapters} 章
- 当前章节位置：第{chapter_num}章 / 共{total_chapters}章（{book_position}）
- 全书主要章节类型：{types_str}
- 全书主要钩子类型：{hooks_str}
- 本书核心人物：{chars_str}{kw_context}

【当前章节信息】
章节：第{chapter_num}章 - {chapter_title}
章节类型：{chapter_stats.get('chapter_type', '未知')}
对白率：{chapter_stats.get('dialogue_rate', 0):.2%}
钩子类型：{chapter_stats.get('hook_type', '未知')}（强度：{chapter_stats.get('hook_strength', 0):.2f}）
情绪轴：{json.dumps(chapter_stats.get('emotion_axes', {}), ensure_ascii=False)}
关键对白：{json.dumps(chapter_stats.get('key_dialogues', []), ensure_ascii=False)}

【章节内容】（完整原文，6000字符上限）
{content}

【输出格式】
只输出一个 JSON 对象，不要输出任何额外文字：
{{
  "items": [
    {{
      "warehouse": "结构仓/角色仓/设定仓/表达仓/商业仓",
      "title": "12字内可复用标题",
      "knowledge_type": "template/rule/pattern/archetype/graph/ledger/sample/counter_example/trigger/signal/arc",
      "level": "book/volume/story_segment/chapter/scene/beat/paragraph/sentence",
      "principle": "为什么有效，核心原理，30-50字",
      "mechanism": "具体做法，关键细节，30-50字",
      "application": "适用场景，10-20字",
      "evidence": ["最有力的原文片段1-2条"],
      "confidence": 0.0,
      "reader_effect_tags": ["代入", "共鸣", "爽感", "揪心", "好奇"],
      "quality_axes": ["沉浸", "感染", "牵引", "稳定", "分享"],
      "targets": ["人物", "关系", "结构", "情绪", "设定", "叙事"],
      "stages": ["开篇", "立主线", "中段", "转折", "高潮", "卷末", "回收", "终局"],
      "control_tags": ["必开", "可选", "高频", "低频", "需人工确认"],
      "risk_tags": ["易跑偏", "易水文", "易失真", "易模板化"],
      "usable_modules": ["启动包", "质量引擎", "运行台"],
      "usable_checkpoints": [1, 2, 3, 4, 5, 6, 7]
    }}
  ]
}}

【质量要求】
1. 不要读后感，不要泛泛夸奖，每条必须有可操作的 principle + mechanism
2. principle/ mechanism/ application 必须写满规定字数，宁多勿少
3. knowledge_type 和 level 必须使用英文枚举值
4. quality_axes / targets / stages / reader_effect_tags / control_tags / risk_tags 必须是数组
5. confidence 为 0-1 小数，证据必须来自原文，不要捏造
6. 如果本章缺乏足够证据支撑某个知识类型，宁可少写一条也不要硬凑
"""


def build_tier3_prompt(book_name: str, warehouse: str, items: List[Dict[str, Any]]) -> str:
    # P2优化：增加摘要条目上限（16→32），并补充更多字段供归纳参考
    summary_items = items[:32]
    total_items = len(items)
    summary_lines = []
    for item in summary_items:
        summary_lines.append(
            f"""
【{item.get('title', '未命名')}】
- knowledge_type: {item.get('knowledge_type', '')}
- level: {item.get('level', '')}
- principle: {clip_text(item.get('principle', ''), 80)}
- mechanism: {clip_text(item.get('mechanism', ''), 80)}
- application: {clip_text(item.get('application', ''), 80)}
- reader_effect_tags: {json.dumps(item.get('reader_effect_tags', []), ensure_ascii=False)}
- quality_axes: {json.dumps(item.get('quality_axes', []), ensure_ascii=False)}
- targets: {json.dumps(item.get('targets', []), ensure_ascii=False)}
- stages: {json.dumps(item.get('stages', []), ensure_ascii=False)}
"""
        )

    # P2优化：Tier3 也增加 CoT 引导，帮助模型进行归纳演绎
    return f"""
【角色扮演】你是一位"小说知识底座"的深度提炼专家，擅长从候选知识中归纳出可审批的核心知识条目。

【任务】对同一仓库下的 Tier 2 候选知识进行去重、归纳、升格，生成 3-6 条真正可审批的 Tier 3 候选知识。

【Chain-of-Thought 引导】
Step 1 - 聚类：扫描所有来源条目，找出知识类型相同/ principle 相近的条目
Step 2 - 合并：对近似条目，提取各自最有力的 evidence，合并为一条更强的 principle
Step 3 - 升格：站在全书视角，判断这个知识是否具有跨章节/跨卷的通用性
Step 4 - 定级：确认这条知识是"规则级"还是"模式级"还是"模板级"
Step 5 - 输出：按格式严格输出 JSON，不要输出思考过程

书籍：{book_name}
仓库：{warehouse}
来源条目总数：{total_items}（本次摘要：{len(summary_items)} 条）

来源条目摘要：
{''.join(summary_lines)}

【输出格式】
只输出一个 JSON 对象，不要输出任何额外文字：
{{
  "items": [
    {{
      "warehouse": "{warehouse}",
      "title": "12字内标题",
      "knowledge_type": "template/rule/pattern/archetype/graph/ledger/sample/counter_example/trigger/signal/arc",
      "level": "book/volume/story_segment/chapter/scene/beat/paragraph/sentence",
      "principle": "核心原理，50-80字，揭示为什么有效",
      "mechanism": "核心做法，50-80字，具体操作步骤",
      "application": "适用场景，20-40字，使用边界",
      "evidence": ["最有力的原文片段，最多2条"],
      "confidence": 0.0,
      "reader_effect_tags": ["代入", "共鸣", "爽感", "揪心", "好奇", "讨论", "传播"],
      "quality_axes": ["牵引", "沉浸", "感染", "稳定", "分享"],
      "targets": ["人物", "关系", "结构", "情绪", "设定", "叙事", "商业"],
      "stages": ["开篇", "立主线", "中段", "转折", "高潮", "卷末", "回收", "终局"],
      "control_tags": ["必开", "可选", "高频", "低频", "需人工确认", "可自动化"],
      "risk_tags": ["易跑偏", "易水文", "易失真", "易模板化", "易过拟合经典"],
      "usable_modules": ["启动包", "质量引擎", "运行台"],
      "usable_checkpoints": [1, 2, 3, 4, 5, 6, 7]
    }}
  ]
}}

【质量要求】
1. 只输出可进入 candidate review 的条目，不要输出泛泛总结或读后感
2. 多个相似条目必须合并为一条更强的 principle，不平铺重复
3. principle/mechanism/application 必须写满规定字数
4. knowledge_type 和 level 必须使用英文枚举值
5. confidence 为 0-1 小数，综合评估所有来源条目的置信度
6. 优先保留具有跨章节通用性、能直接改善启动包和质量裁决的知识
"""


def normalize_tier2_item(
    raw_item: Dict[str, Any],
    *,
    book_name: str,
    genre: str,
    chapter_num: int,
    chapter_title: str,
    chapter_stats: Dict[str, Any],
    chapter_excerpt: str,
) -> Dict[str, Any]:
    warehouse = normalize_warehouse(raw_item.get("warehouse"))
    principle = clip_text(raw_item.get("principle") or raw_item.get("mechanism") or raw_item.get("topic") or "", 500)
    mechanism = clip_text(raw_item.get("mechanism") or raw_item.get("implementation") or raw_item.get("principle") or "", 500)
    application = clip_text(raw_item.get("application") or raw_item.get("applicable_conditions") or "", 300)
    reader_effect_tags = normalize_tags(raw_item.get("reader_effect_tags") or raw_item.get("reader_effect") or "", READER_EFFECT_TAGS, ["好奇"])
    quality_axes = normalize_tags(raw_item.get("quality_axes") or infer_quality_axes(f"{principle} {mechanism} {application}", warehouse), QUALITY_AXES, infer_quality_axes(f"{principle} {mechanism} {application}", warehouse))
    targets = normalize_tags(raw_item.get("targets") or infer_target_tags(f"{principle} {mechanism} {application}", warehouse), TARGET_TAGS, infer_target_tags(f"{principle} {mechanism} {application}", warehouse))
    stages = normalize_tags(raw_item.get("stages") or infer_stage_tags(f"{principle} {mechanism} {application}", "中段"), STAGE_TAGS, infer_stage_tags(f"{principle} {mechanism} {application}", "中段"))
    knowledge_type = normalize_choice(raw_item.get("knowledge_type") or raw_item.get("unit_type"), KNOWLEDGE_TYPE_ALIASES, "pattern")
    level = normalize_choice(raw_item.get("level") or raw_item.get("granularity"), LEVEL_ALIASES, "chapter")
    control_tags = normalize_tags(raw_item.get("control_tags") or infer_control_tags(knowledge_type, quality_axes), CONTROL_TAGS, infer_control_tags(knowledge_type, quality_axes))
    risk_tags = normalize_tags(raw_item.get("risk_tags") or infer_risk_tags(f"{principle} {mechanism} {application}"), RISK_TAGS, infer_risk_tags(f"{principle} {mechanism} {application}"))

    evidence_value = raw_item.get("evidence") or raw_item.get("evidence_text") or []
    if isinstance(evidence_value, str):
        evidence = [clip_text(evidence_value, 180)] if evidence_value.strip() else []
    else:
        evidence = [clip_text(item, 180) for item in evidence_value if str(item).strip()]
    evidence = unique_preserve(evidence)[:2]

    title = clip_text(raw_item.get("title") or "未命名知识", 40)
    topic = title
    domain = WAREHOUSE_DEFAULT_DOMAIN.get(warehouse, "结构设计")
    confidence = min(1.0, max(0.0, safe_float(raw_item.get("confidence"), 0.68)))
    source_work = f"{genre}/{book_name}"
    data_sources = build_data_sources(
        tier=2,
        book_name=book_name,
        chapter_num=chapter_num,
        chapter_title=chapter_title,
        source_files=["chapter_stats.json", "high_value_chapters.json", "tier2_summary.json"],
        source_fields=["emotion_axes", "hook_type", "key_dialogues"],
        source_excerpt=chapter_excerpt,
        stats_snapshot={
            "chapter_type": chapter_stats.get("chapter_type"),
            "hook_type": chapter_stats.get("hook_type"),
            "hook_strength": chapter_stats.get("hook_strength"),
            "dialogue_rate": chapter_stats.get("dialogue_rate"),
            "emotion_axes": chapter_stats.get("emotion_axes", {}),
        },
    )
    item_id = stable_id(source_work, warehouse, title, str(chapter_num), knowledge_type, level)

    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "id": item_id,
        "warehouse": warehouse,
        "domain": domain,
        "knowledge_type": knowledge_type,
        "level": level,
        "source_type": "classic_book_tier2",
        "source_work": source_work,
        "quality_axes": quality_axes,
        "targets": targets,
        "stages": stages,
        "usable_modules": list(raw_item.get("usable_modules") or infer_usable_modules(warehouse, quality_axes)),
        "usable_checkpoints": list(raw_item.get("usable_checkpoints") or infer_usable_checkpoints(stages)),
        "auto_callable": bool(raw_item.get("auto_callable", False)),
        "human_confirm_required": bool(raw_item.get("human_confirm_required", True)),
        "confidence": confidence,
        "status": "candidate",
        "title": title,
        "version": 1,
        "notes": clip_text(raw_item.get("notes") or principle, 300),
        "topic": topic,
        "principle": principle,
        "mechanism": mechanism,
        "application": application,
        "reader_effect_tags": reader_effect_tags,
        "control_tags": control_tags,
        "risk_tags": risk_tags,
        "evidence": evidence,
        "data_sources": data_sources,
        "source_books": [book_name],
        "source_chapters": [chapter_num],
        "tier": 2,
        "created_at": now_iso(),
        "review_context": {
            "decision_reason": clip_text(raw_item.get("decision_reason") or principle, 220),
            "stage_name": "章节蒸馏",
        },
        "raw_capture": {
            "source_chapter_title": chapter_title,
            "book_name": book_name,
            "genre": genre,
        },
    }


def normalize_tier3_item(
    raw_item: Dict[str, Any],
    *,
    book_name: str,
    genre: str,
    source_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    warehouse = normalize_warehouse(raw_item.get("warehouse"))
    principle = clip_text(raw_item.get("principle") or raw_item.get("mechanism") or raw_item.get("topic") or "", 700)
    mechanism = clip_text(raw_item.get("mechanism") or raw_item.get("principle") or "", 500)
    application = clip_text(raw_item.get("application") or "", 320)
    title = clip_text(raw_item.get("title") or "未命名知识", 40)
    topic = title
    domain = WAREHOUSE_DEFAULT_DOMAIN.get(warehouse, "结构设计")

    source_blob = " ".join(
        [
            title,
            topic,
            principle,
            mechanism,
            application,
            " ".join(item.get("topic", "") for item in source_items[:6]),
        ]
    )
    reader_effect_tags = normalize_tags(raw_item.get("reader_effect_tags") or raw_item.get("reader_effect") or "", READER_EFFECT_TAGS, infer_reader_effect_tags(source_blob))
    quality_axes = normalize_tags(raw_item.get("quality_axes") or infer_quality_axes(source_blob, warehouse), QUALITY_AXES, infer_quality_axes(source_blob, warehouse))
    targets = normalize_tags(raw_item.get("targets") or infer_target_tags(source_blob, warehouse), TARGET_TAGS, infer_target_tags(source_blob, warehouse))
    stages = normalize_tags(raw_item.get("stages") or infer_stage_tags(source_blob, "卷末"), STAGE_TAGS, infer_stage_tags(source_blob, "卷末"))
    knowledge_type = normalize_choice(raw_item.get("knowledge_type") or raw_item.get("unit_type"), KNOWLEDGE_TYPE_ALIASES, "pattern")
    level = normalize_choice(raw_item.get("level") or raw_item.get("granularity"), LEVEL_ALIASES, "scene")
    control_tags = normalize_tags(raw_item.get("control_tags") or infer_control_tags(knowledge_type, quality_axes), CONTROL_TAGS, infer_control_tags(knowledge_type, quality_axes))
    risk_tags = normalize_tags(raw_item.get("risk_tags") or infer_risk_tags(source_blob), RISK_TAGS, infer_risk_tags(source_blob))

    evidence_value = raw_item.get("evidence") or []
    if isinstance(evidence_value, str):
        evidence = [clip_text(evidence_value, 180)] if evidence_value.strip() else []
    else:
        evidence = [clip_text(item, 180) for item in evidence_value if str(item).strip()]
    evidence = unique_preserve(evidence)[:2]
    source_books = unique_preserve([str(item.get("source_work", "")).split("/", 1)[-1] for item in source_items if item.get("source_work")])
    source_chapters = sorted({int(ch) for item in source_items for ch in item.get("source_chapters", []) if isinstance(ch, int)})
    confidence = min(1.0, max(0.0, safe_float(raw_item.get("confidence"), 0.68)))
    source_work = f"{genre}/{book_name}"
    # 从 source_items 中提取章节级溯源信息
    tier3_data_sources = []
    seen_chapters = set()
    for src_item in source_items:
        for src_ds in (src_item.get("data_sources") or []):
            ch_num = src_ds.get("chapter_num")
            ch_title = src_ds.get("chapter_title", "")
            if ch_num is not None and ch_num not in seen_chapters:
                seen_chapters.add(ch_num)
                tier3_data_sources.append(
                    build_data_sources(
                        tier=3,
                        book_name=book_name,
                        chapter_num=ch_num,
                        chapter_title=ch_title,
                        source_files=[src_ds.get("file", "tier2_output/knowledge_items.json")],
                        source_fields=src_ds.get("fields", []),
                        source_excerpt=clip_text(src_ds.get("source_excerpt", ""), 180),
                        stats_snapshot=src_ds.get("stats_snapshot", {}),
                    )[0]
                )
        if len(tier3_data_sources) >= 4:
            break
    data_sources = tier3_data_sources if tier3_data_sources else build_data_sources(
        tier=3,
        book_name=book_name,
        source_files=["tier2_output/knowledge_items.json"],
        source_fields=["tier2_items"],
        source_excerpt=" / ".join(clip_text(item.get("title", ""), 30) for item in source_items[:3]),
        stats_snapshot={
            "source_items_count": len(source_items),
        },
    )
    item_id = stable_id(source_work, warehouse, title, "tier3", knowledge_type, level)

    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "id": item_id,
        "warehouse": warehouse,
        "domain": domain,
        "knowledge_type": knowledge_type,
        "level": level,
        "source_type": "classic_book_tier3",
        "source_work": source_work,
        "quality_axes": quality_axes,
        "targets": targets,
        "stages": stages,
        "usable_modules": list(raw_item.get("usable_modules") or infer_usable_modules(warehouse, quality_axes)),
        "usable_checkpoints": list(raw_item.get("usable_checkpoints") or infer_usable_checkpoints(stages)),
        "auto_callable": bool(raw_item.get("auto_callable", False)),
        "human_confirm_required": bool(raw_item.get("human_confirm_required", True)),
        "confidence": confidence,
        "status": "candidate",
        "title": title,
        "version": 1,
        "notes": clip_text(raw_item.get("notes") or principle, 320),
        "topic": topic,
        "principle": principle,
        "mechanism": mechanism,
        "application": application,
        "reader_effect_tags": reader_effect_tags,
        "control_tags": control_tags,
        "risk_tags": risk_tags,
        "evidence": evidence,
        "data_sources": data_sources,
        "source_books": source_books or [book_name],
        "source_chapters": source_chapters,
        "source_items_count": len(source_items),
        "tier": 3,
        "created_at": now_iso(),
        "review_context": {
            "decision_reason": clip_text(raw_item.get("decision_reason") or principle, 220),
            "stage_name": "深度提炼",
        },
    }


# ============================================================================
# Tier 2 / Tier 3
# ============================================================================

MAX_CHAPTER_RETRIES = 2  # 章节级最大重试次数


def build_json_rescue_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "补充要求：\n"
        "1. 只返回一个合法 JSON 对象。\n"
        "2. 不要输出解释、前言、后记、Markdown、代码块。\n"
        '3. 顶层必须是 {"items": [...]}。\n'
        '4. 如果确实无法抽取，也必须返回 {"items": []}。\n'
    )


def call_structured_items(prompt: str, *, max_tokens: int = 3000, temperature: Optional[float] = None, system_prompt: Optional[str] = None) -> Dict[str, Any]:
    """调用结构化输出，temperature 为 None 时使用后端默认温度"""
    attempts: List[Dict[str, Any]] = []
    last_error = "API 返回为空"
    last_raw_result: Optional[str] = None
    last_parsed = {"items": [], "parse_mode": "empty"}

    # 确定基础温度（temperature 为 None 时使用后端默认值）
    if temperature is None:
        if LLM_BACKEND == "ollama":
            temperature = OLLAMA_TEMPERATURE
        else:
            temperature = GLM_TEMPERATURE

    total_attempts = 1 + max(0, STRUCTURED_OUTPUT_RESCUES)
    for attempt in range(total_attempts):
        use_rescue = attempt > 0
        current_prompt = build_json_rescue_prompt(prompt) if use_rescue else prompt
        # Rescue 模式使用稍高温度（给模型更多空间修正）
        if LLM_BACKEND == "ollama":
            rescue_temp = temperature  # Ollama 不区分 rescue 温度
        else:
            rescue_temp = GLM_RESCUE_TEMPERATURE
        current_temperature = rescue_temp if use_rescue else temperature
        raw_result = call_glm_api(current_prompt, max_tokens=max_tokens, temperature=current_temperature, system_prompt=system_prompt)
        parsed = safe_parse_response(raw_result or "")
        raw_items = parsed.get("items") or []
        attempts.append(
            {
                "attempt": attempt + 1,
                "mode": "json_rescue" if use_rescue else "primary",
                "temperature": current_temperature,
                "has_response": bool(raw_result),
                "parse_mode": parsed.get("parse_mode"),
                "items_count": len(raw_items),
                "created_at": now_iso(),
            }
        )
        last_raw_result = raw_result
        last_parsed = parsed
        if raw_items:
            return {
                "success": True,
                "raw_result": raw_result,
                "parsed": parsed,
                "raw_items": raw_items,
                "attempts": attempts,
                "error": "",
                "used_rescue_prompt": use_rescue,
                "final_prompt": current_prompt,
            }
        last_error = "API 返回为空" if not raw_result else f"解析失败 (parse_mode={parsed.get('parse_mode')})"

    return {
        "success": False,
        "raw_result": last_raw_result,
        "parsed": last_parsed,
        "raw_items": [],
        "attempts": attempts,
        "error": last_error,
        "used_rescue_prompt": bool(attempts and attempts[-1]["mode"] == "json_rescue"),
        "final_prompt": build_json_rescue_prompt(prompt) if total_attempts > 1 else prompt,
    }


def _process_tier2_chapter(book_name, genre, chapter_num, chapter_title, content, chapter_stats, raw_dir, invalid_dir, book_overview=None, plot_keywords=None):
    """处理单个章节的 Tier2 提取（可并发调用），失败自动重试"""
    prompt = build_tier2_prompt(book_name, chapter_num, chapter_title, content, chapter_stats, book_overview=book_overview, plot_keywords=plot_keywords)
    # Tier2 温度：一致性优先，较低温度
    tier2_temp = OLLAMA_TIER2_TEMPERATURE if LLM_BACKEND == "ollama" else GLM_TEMPERATURE
    last_error = ""
    for attempt in range(MAX_CHAPTER_RETRIES + 1):
        # P2优化：max_tokens 4500 + system_prompt 强化角色定义
        result = call_structured_items(prompt, max_tokens=4500, temperature=tier2_temp, system_prompt=DISTILL_SYSTEM_PROMPT)
        raw_result = result["raw_result"]
        dump_json(
            raw_dir / f"chapter_{chapter_num:04d}.json",
            {
                "prompt": result["final_prompt"],
                "prompt_version": PROMPT_VERSION,
                "script_version": SCRIPT_VERSION,
                "response": raw_result,
                "attempts": result["attempts"],
                "used_rescue_prompt": result["used_rescue_prompt"],
                "created_at": now_iso(),
            },
        )
        if not result["success"]:
            last_error = result["error"] or "API 返回为空"
            if attempt < MAX_CHAPTER_RETRIES:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            dump_json(
                invalid_dir / f"chapter_{chapter_num:04d}.json",
                {
                    "prompt": result["final_prompt"],
                    "response": raw_result,
                    "error": last_error,
                    "attempts": result["attempts"],
                    "parse_mode": result["parsed"].get("parse_mode"),
                    "created_at": now_iso(),
                },
            )
            return []
        raw_items = result["raw_items"]
        normalized_items = [
            normalize_tier2_item(
                item,
                book_name=book_name,
                genre=genre,
                chapter_num=chapter_num,
                chapter_title=chapter_title,
                chapter_stats=chapter_stats,
                chapter_excerpt=content[:600],
            )
            for item in raw_items
            if isinstance(item, dict)
        ]
        return normalized_items
    return []


def tier2_extract(
    *,
    book_name: str,
    genre: str,
    high_value: List[Dict[str, Any]],
    chapter_map: Dict[int, ChapterRecord],
    chapter_stats_index: Dict[int, Dict[str, Any]],
    tier2_dir: Path,
    book_overview: Optional[Dict[str, Any]] = None,
    plot_keywords: Optional[Dict[str, List[int]]] = None,
) -> List[Dict[str, Any]]:
    print(f"    [Tier 2] 提取标准候选知识条目... (并发={TIER2_CONCURRENCY})")
    all_items: List[Dict[str, Any]] = []
    raw_dir = ensure_dir(tier2_dir / "raw_responses")
    invalid_dir = ensure_dir(tier2_dir / "invalid_responses")

    if TIER2_CONCURRENCY <= 1:
        # 串行模式（原逻辑）
        for index, candidate in enumerate(high_value, start=1):
            chapter_num = int(candidate["chapter_num"])
            chapter = chapter_map.get(chapter_num)
            if not chapter:
                continue
            chapter_stats = chapter_stats_index.get(chapter_num, {})
            # P2优化：叙事弧内容窗口（开头3000+结尾1500=4500字符），保留完整起承转合
            content = build_chapter_content_window(chapter.content, head_chars=3000, tail_chars=1500)
            print(f"      章节 {chapter_num} ({index}/{len(high_value)})...")
            items = _process_tier2_chapter(
                book_name, genre, chapter_num, chapter.title, content, chapter_stats,
                raw_dir, invalid_dir, book_overview=book_overview, plot_keywords=plot_keywords
            )
            all_items.extend(items)
            dump_json(tier2_dir / "_progress.json", {
                "stage": "tier2", "chapter": chapter_num,
                "index": index, "total": len(high_value),
                "items_so_far": len(all_items), "updated_at": now_iso(),
            })
            time.sleep(TIER2_SLEEP_SECONDS)
    else:
        # 并发模式
        import concurrent.futures
        import threading
        progress_lock = threading.Lock()
        result_lock = threading.Lock()
        completed_count = [0]

        def _worker(task):
            index, candidate = task
            chapter_num = int(candidate["chapter_num"])
            chapter = chapter_map.get(chapter_num)
            if not chapter:
                return []
            chapter_stats = chapter_stats_index.get(chapter_num, {})
            # P2优化：叙事弧内容窗口（开头3000+结尾1500=4500字符）
            content = build_chapter_content_window(chapter.content, head_chars=3000, tail_chars=1500)
            items = _process_tier2_chapter(
                book_name, genre, chapter_num, chapter.title, content, chapter_stats,
                raw_dir, invalid_dir, book_overview=book_overview, plot_keywords=plot_keywords
            )
            with progress_lock:
                completed_count[0] += 1
                dump_json(tier2_dir / "_progress.json", {
                    "stage": "tier2", "chapter": chapter_num,
                    "index": completed_count[0], "total": len(high_value),
                    "items_so_far": "~concurrency", "updated_at": now_iso(),
                })
                print(f"      章节 {chapter_num} ({completed_count[0]}/{len(high_value)})... {len(items)}条")
            time.sleep(TIER2_SLEEP_SECONDS)
            return items

        tasks = list(enumerate(high_value, start=1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=TIER2_CONCURRENCY) as executor:
            futures = {executor.submit(_worker, t): t for t in tasks}
            for future in concurrent.futures.as_completed(futures):
                try:
                    items = future.result()
                    with result_lock:
                        all_items.extend(items)
                except Exception as exc:  # noqa: BLE001
                    print(f"      [ERROR] {exc}")

        # 修正最终进度
        dump_json(tier2_dir / "_progress.json", {
            "stage": "tier2", "chapter": "done",
            "index": len(high_value), "total": len(high_value),
            "items_so_far": len(all_items), "updated_at": now_iso(),
        })

    deduped, dedupe_report = dedupe_items(all_items)
    validation_bundle = split_validated_items(deduped)
    dump_json(tier2_dir / "repairable_items.json", validation_bundle["repairable_items"])
    dump_json(tier2_dir / "rejected_items.json", validation_bundle["rejected_items"])
    dump_json(tier2_dir / "validation_results.json", validation_bundle["validations"])
    dump_json(tier2_dir / "dedupe_report.json", dedupe_report)
    dump_json(
        tier2_dir / "quality_report.json",
        build_quality_report(
            stage_name="tier2",
            items=deduped,
            validation_bundle=validation_bundle,
            dedupe_report=dedupe_report,
        ),
    )
    final_items = validation_bundle["accepted_items"] + validation_bundle["repairable_items"]
    print(f"      总计: {len(final_items)} 条（accepted {len(validation_bundle['accepted_items'])} / repairable {len(validation_bundle['repairable_items'])} / rejected {len(validation_bundle['rejected_items'])}）")
    return final_items


def tier3_distill(
    *,
    book_name: str,
    genre: str,
    tier2_items: List[Dict[str, Any]],
    tier3_dir: Path,
) -> List[Dict[str, Any]]:
    print("    [Tier 3] 深度提炼为可审批知识条目...")
    if not tier2_items:
        return []

    # Tier3 温度：需要一点创造性，稍高温度
    tier3_temp = OLLAMA_TIER3_TEMPERATURE if LLM_BACKEND == "ollama" else GLM_TEMPERATURE

    raw_dir = ensure_dir(tier3_dir / "raw_responses")
    invalid_dir = ensure_dir(tier3_dir / "invalid_responses")
    by_warehouse: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in tier2_items:
        by_warehouse[str(item.get("warehouse") or "结构仓")].append(item)

    all_items: List[Dict[str, Any]] = []
    warehouse_list = list(by_warehouse.items())
    for idx, (warehouse, warehouse_items) in enumerate(warehouse_list, start=1):
        print(f"      处理 {warehouse} ({len(warehouse_items)}条) ({idx}/{len(warehouse_list)})...")
        prompt = build_tier3_prompt(book_name, warehouse, warehouse_items)
        # P2优化：max_tokens 4000 + system_prompt 强化角色定义
        result = call_structured_items(prompt, max_tokens=4000, temperature=tier3_temp, system_prompt=DISTILL_SYSTEM_PROMPT)
        raw_result = result["raw_result"]
        dump_json(
            raw_dir / f"{warehouse}.json",
            {
                "prompt": result["final_prompt"],
                "prompt_version": PROMPT_VERSION,
                "script_version": SCRIPT_VERSION,
                "response": raw_result,
                "attempts": result["attempts"],
                "used_rescue_prompt": result["used_rescue_prompt"],
                "created_at": now_iso(),
            },
        )
        if not result["success"]:
            print(f"        [WARN] {warehouse} {result['error']}，跳过")
            dump_json(
                invalid_dir / f"{warehouse}.json",
                {
                    "prompt": result["final_prompt"],
                    "response": raw_result,
                    "error": result["error"],
                    "attempts": result["attempts"],
                    "parse_mode": result["parsed"].get("parse_mode"),
                    "created_at": now_iso(),
                },
            )
            continue
        raw_items = result["raw_items"]
        normalized_items = [
            normalize_tier3_item(
                item,
                book_name=book_name,
                genre=genre,
                source_items=warehouse_items,
            )
            for item in raw_items
            if isinstance(item, dict)
        ]
        all_items.extend(normalized_items)
        print(f"        提炼 {len(normalized_items)} 条精华")
        dump_json(tier3_dir / "_progress.json", {
            "stage": "tier3",
            "warehouse": warehouse,
            "index": idx,
            "total": len(warehouse_list),
            "items_so_far": len(all_items),
            "updated_at": now_iso(),
        })
        time.sleep(TIER3_SLEEP_SECONDS)

    deduped, dedupe_report = dedupe_items(all_items)
    validation_bundle = split_validated_items(deduped)
    dump_json(tier3_dir / "repairable_items.json", validation_bundle["repairable_items"])
    dump_json(tier3_dir / "rejected_items.json", validation_bundle["rejected_items"])
    dump_json(tier3_dir / "validation_results.json", validation_bundle["validations"])
    dump_json(tier3_dir / "dedupe_report.json", dedupe_report)
    dump_json(
        tier3_dir / "quality_report.json",
        build_quality_report(
            stage_name="tier3",
            items=deduped,
            validation_bundle=validation_bundle,
            dedupe_report=dedupe_report,
        ),
    )
    final_items = validation_bundle["accepted_items"] + validation_bundle["repairable_items"]
    print(f"      总计: {len(final_items)} 条精华（accepted {len(validation_bundle['accepted_items'])} / repairable {len(validation_bundle['repairable_items'])} / rejected {len(validation_bundle['rejected_items'])}）")
    return final_items


def dedupe_items(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    merge_examples: List[Dict[str, Any]] = []
    for item in items:
        key = stable_id(
            item.get("source_work", ""),
            item.get("warehouse", ""),
            dedupe_signature(item),
            item.get("knowledge_type", ""),
            item.get("level", ""),
        )
        if key not in grouped:
            grouped[key] = deepcopy(item)
            grouped[key]["id"] = key
            continue
        existing = grouped[key]
        merge_examples.append(
            {
                "id": key,
                "title": existing.get("title"),
                "warehouse": existing.get("warehouse"),
                "merged_source_chapters": sorted({*existing.get("source_chapters", []), *item.get("source_chapters", [])}),
            }
        )
        existing["confidence"] = max(safe_float(existing.get("confidence")), safe_float(item.get("confidence")))
        existing["quality_axes"] = unique_preserve(list(existing.get("quality_axes") or []) + list(item.get("quality_axes") or []))
        existing["targets"] = unique_preserve(list(existing.get("targets") or []) + list(item.get("targets") or []))
        existing["stages"] = unique_preserve(list(existing.get("stages") or []) + list(item.get("stages") or []))
        existing["reader_effect_tags"] = unique_preserve(list(existing.get("reader_effect_tags") or []) + list(item.get("reader_effect_tags") or []))
        existing["control_tags"] = unique_preserve(list(existing.get("control_tags") or []) + list(item.get("control_tags") or []))
        existing["risk_tags"] = unique_preserve(list(existing.get("risk_tags") or []) + list(item.get("risk_tags") or []))
        existing["evidence"] = unique_preserve(list(existing.get("evidence") or []) + list(item.get("evidence") or []))[:2]
        existing["source_books"] = unique_preserve(list(existing.get("source_books") or []) + list(item.get("source_books") or []))
        existing["source_chapters"] = sorted({*existing.get("source_chapters", []), *item.get("source_chapters", [])})
        existing["data_sources"] = unique_preserve(list(existing.get("data_sources") or []) + list(item.get("data_sources") or []))
    deduped = list(grouped.values())
    report = {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "before_count": len(items),
        "after_count": len(deduped),
        "merged_count": max(0, len(items) - len(deduped)),
        "merge_examples": merge_examples[:20],
        "generated_at": now_iso(),
    }
    return deduped, report


# ============================================================================
# 输出组织
# ============================================================================

def build_knowledge_base(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_warehouse: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_warehouse[str(item.get("warehouse") or "结构仓")].append(item)
    return {warehouse: entries for warehouse, entries in sorted(by_warehouse.items())}


def build_tier2_summary(book_name: str, items: List[Dict[str, Any]], high_value_count: int) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "book_name": book_name,
        "status": "completed",
        "total_items": len(items),
        "chapters_processed": high_value_count,
        "warehouse_distribution": dict(Counter(item.get("warehouse", "未知") for item in items)),
        "knowledge_type_distribution": dict(Counter(item.get("knowledge_type", "unknown") for item in items)),
        "stage_distribution": dict(Counter(stage for item in items for stage in item.get("stages", []))),
        "processed_at": now_iso(),
    }


def build_tier3_summary(book_name: str, tier2_items: List[Dict[str, Any]], tier3_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "book_name": book_name,
        "status": "candidate_ready",
        "total_items": len(tier3_items),
        "source_items": len(tier2_items),
        "compression_ratio": round(len(tier3_items) / max(len(tier2_items), 1), 4),
        "warehouse_distribution": dict(Counter(item.get("warehouse", "未知") for item in tier3_items)),
        "knowledge_type_distribution": dict(Counter(item.get("knowledge_type", "unknown") for item in tier3_items)),
        "processed_at": now_iso(),
    }


def build_round_report(round_num: int, processed_books: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "round": round_num,
        "books_processed": len([book for book in processed_books if book["success"]]),
        "books_failed": len([book for book in processed_books if not book["success"]]),
        "processed_at": now_iso(),
        "books": processed_books,
    }


def update_progress(processed_books: List[Dict[str, Any]], *, round_num: int, total_books: int = 0) -> None:
    progress_file = OUTPUT_BASE / "three_tier_progress.json"
    # 写前备份（保留最近3份）
    if progress_file.exists():
        import shutil
        for i in range(2, 0, -1):
            backup = OUTPUT_BASE / f"three_tier_progress.json.bak{i}"
            prev = OUTPUT_BASE / f"three_tier_progress.json.bak{i-1}"
            if prev.exists():
                shutil.copy2(prev, backup)
        shutil.copy2(progress_file, OUTPUT_BASE / "three_tier_progress.json.bak1")
    progress = load_json(
        progress_file,
        {
            "schema_version": SCHEMA_VERSION,
            "script_version": SCRIPT_VERSION,
            "total_books": 0,
            "processed_books": [],
            "current_round": 0,
            "started_at": now_iso(),
            "status": "in_progress",
        },
    )
    # Deduplicate by (genre, book_file) to avoid double-counting on re-runs
    # 同一本书在不同目录下（如genre变化）不会被误判为重复
    existing_keys = {(b["genre"], b["book_file"]) for b in progress["processed_books"]}
    existing_indexes = {
        (b.get("genre"), b.get("book_file")): idx
        for idx, b in enumerate(progress["processed_books"])
    }
    for book in processed_books:
        key = (book["genre"], book["book_file"])
        if key in existing_indexes:
            progress["processed_books"][existing_indexes[key]] = book
        else:
            progress["processed_books"].append(book)
            existing_indexes[key] = len(progress["processed_books"]) - 1
    progress["current_round"] = round_num
    if total_books > 0:
        progress["total_books"] = total_books
    progress["last_update"] = now_iso()
    progress["status"] = "in_progress"
    # 原子写入：先写临时文件，再 rename（避免写到一半崩溃导致文件损坏）
    import tempfile
    tmp = progress_file.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        tmp.replace(progress_file)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def select_books(args: argparse.Namespace) -> List[Tuple[str, str]]:
    if args.scan:
        books = scan_books_from_source(min_size_mb=args.min_size)
        print(f"  扫描到 {len(books)} 本书")
    else:
        books = list(ROUND1_BOOKS)
    if args.genre:
        books = [item for item in books if item[0] == args.genre]
    if args.book:
        requested = Path(args.book)
        requested_name = requested.name
        requested_stem = requested.stem
        books = [
            item
            for item in books
            if item[1] == requested_name or Path(item[1]).stem == requested_stem
        ]
    if args.max_books and args.max_books > 0:
        books = books[: args.max_books]
    return books


def should_skip_book(book_name: str, genre: str, book_file: str, args: argparse.Namespace) -> bool:
    """根据进度文件判断是否跳过本书

    唯一键为 (genre, book_file)，而非 book_name，
    保证同名不同书或同书名不同目录都能正确处理。
    """
    if args.force:
        return False  # --force 优先级最高，跳过所有检查
    if not (args.resume or args.rerun_failed_only):
        return False
    progress_file = OUTPUT_BASE / "three_tier_progress.json"
    progress = load_json(progress_file, {"processed_books": []})
    # 用 (genre, book_file) 精确匹配，不依赖 book_name
    matched = [
        item
        for item in progress.get("processed_books", [])
        if item.get("genre") == genre and item.get("book_file") == book_file
    ]
    if not matched:
        return False
    latest = matched[-1]
    if args.rerun_failed_only:
        return bool(latest.get("success"))  # 上次成功(success=True)→跳过；上次失败(success=False)→重跑
    # resume 模式：跳过所有已记录的书（不论成功或失败），避免无限重试
    return True


# ============================================================================
# 主流程
# ============================================================================

def process_book(genre: str, book_file: str, round_num: int, book_index: int, args: argparse.Namespace) -> bool:
    # 支持扁平目录（genre 不作为子目录）
    full_path = SOURCE_BASE / genre / book_file
    book_path = full_path if full_path.exists() else SOURCE_BASE / book_file
    book_name = book_path.stem
    print(f"\n{'=' * 60}")
    print(f"[第{round_num}轮 - 书{book_index}] {book_name[:40]}...")
    print(f"  类型: {genre}")
    print(f"{'=' * 60}")

    if not book_path.exists():
        print("  源文件不存在")
        return False

    # 文件大小检查
    file_size_mb = book_path.stat().st_size / 1024 / 1024
    min_size = float(os.environ.get("DISTILL_MIN_SIZE", "0"))
    if min_size > 0 and file_size_mb < min_size:
        print(f"  跳过: {file_size_mb:.1f}MB < {min_size}MB")
        return True  # 算成功，不重试

    book_output = OUTPUT_BASE / genre / book_name
    if args.force and book_output.exists():
        import shutil
        print(f"  --force: 清理旧输出 {book_output.name}...")
        shutil.rmtree(book_output, ignore_errors=True)
    ensure_dir(book_output)

    tier3_file = book_output / "tier3_output" / "knowledge_items.json"
    if tier3_file.exists() and not args.force:
        existing = load_json(tier3_file, [])
        if isinstance(existing, list) and existing:
            print(f"  已处理，跳过: {len(existing)} 条 Tier3")
            return True

    print("  [1/4] 读取书籍...")
    use_streaming = file_size_mb > 5  # 5MB 以上走流式，避免 splitlines() OOM
    if use_streaming:
        print(f"  [1/4] 流式分章 (文件 {file_size_mb:.1f}MB)...")
        chapters = split_chapters_streaming(book_path)
        text = None  # 不加载全文
        if not chapters:
            print("  流式分章失败")
            return False
    else:
        text = read_book(book_path)
        if not text or len(text) < 10000:
            print("  读取失败或内容过短")
            return False
        chapters = split_chapters(text)

    tier1_dir = ensure_dir(book_output / "tier1_output")
    if args.skip_tier1:
        print("  [2/4] 跳过 Tier 1，复用现有输出...")
        tier1_result = load_json(tier1_dir / "chapter_stats.json", {})
        high_value = load_json(tier1_dir / "high_value_chapters.json", [])
        if not tier1_result:
            raise RuntimeError("指定了 --skip-tier1，但缺少现有 tier1_output/chapter_stats.json")
    else:
        print("  [2/4] Tier 1 分析...")
        if use_streaming:
            # 流式模式：chapters 已分好，跳过 split_chapters 步骤
            tier1_result, _, high_value = _tier1_analyze_from_chapters(chapters, book_name)
        else:
            tier1_result, chapters, high_value = tier1_analyze(text, book_name)
        dump_json(tier1_dir / "chapter_stats.json", tier1_result)
        dump_json(tier1_dir / "high_value_chapters.json", tier1_result["high_value_chapters"])
        dump_json(tier1_dir / "character_cooccurrence.json", tier1_result["character_cooccurrence"])
        dump_json(tier1_dir / "plot_keywords.json", tier1_result["plot_keywords"])
        dump_json(tier1_dir / "overview.json", tier1_result["overview"])

    chapter_map = {chapter.chapter_num: chapter for chapter in chapters}
    chapter_stats_index = {item["chapter_num"]: item for item in tier1_result["chapter_stats"]}

    if args.max_high_value_chapters and args.max_high_value_chapters > 0:
        high_value = high_value[: args.max_high_value_chapters]

    print("  [3/4] Tier 2 知识提取...")
    tier2_dir = ensure_dir(book_output / "tier2_output")
    if args.skip_tier2:
        tier2_items = load_json(tier2_dir / "knowledge_items.json", [])
        print(f"    跳过 Tier 2，复用现有 {len(tier2_items)} 条")
    else:
        tier2_items = tier2_extract(
            book_name=book_name,
            genre=genre,
            high_value=high_value,
            chapter_map=chapter_map,
            chapter_stats_index=chapter_stats_index,
            tier2_dir=tier2_dir,
            book_overview=tier1_result.get("overview"),
            plot_keywords=tier1_result.get("plot_keywords"),
        )
    dump_json(tier2_dir / "knowledge_items.json", tier2_items)
    dump_json(tier2_dir / "tier2_summary.json", build_tier2_summary(book_name, tier2_items, len(high_value)))
    if not tier2_items:
        print("    [WARN] Tier 2 产出为 0，视为失败，保留现场供排查")
        return False

    print("  [4/4] Tier 3 深度提炼...")
    tier3_dir = ensure_dir(book_output / "tier3_output")
    if args.skip_tier3:
        tier3_items = load_json(tier3_dir / "knowledge_items.json", [])
        print(f"    跳过 Tier 3，复用现有 {len(tier3_items)} 条")
    else:
        tier3_items = tier3_distill(
            book_name=book_name,
            genre=genre,
            tier2_items=tier2_items,
            tier3_dir=tier3_dir,
        )
    dump_json(tier3_dir / "knowledge_items.json", tier3_items)
    dump_json(tier3_dir / "knowledge_base.json", build_knowledge_base(tier3_items))
    dump_json(tier3_dir / "tier3_summary.json", build_tier3_summary(book_name, tier2_items, tier3_items))
    if not tier3_items:
        print("    [WARN] Tier 3 产出为 0，视为失败，保留现场供排查")
        return False

    print(f"\n  完成: Tier2 {len(tier2_items)}条 → Tier3 {len(tier3_items)}条")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="三层蒸馏（标准知识条目版）")
    parser.add_argument("--round", type=int, default=1, help="轮次编号，仅用于报告标记")
    parser.add_argument("--genre", type=str, default="", help="只处理指定分类")
    parser.add_argument("--book", type=str, default="", help="只处理匹配书名的作品")
    parser.add_argument("--max-books", type=int, default=0, help="最多处理多少本书")
    parser.add_argument("--max-high-value-chapters", type=int, default=0, help="限制进入 Tier2 的高价值章节数")
    parser.add_argument("--resume", action="store_true", help="根据进度文件跳过已成功处理的书")
    parser.add_argument("--rerun-failed-only", action="store_true", help="只重跑历史失败作品")
    parser.add_argument("--force", action="store_true", help="即使已有输出也强制重跑")
    parser.add_argument("--scan", action="store_true", help="自动扫描源目录所有书籍（替代 ROUND1_BOOKS 硬编码）")
    parser.add_argument("--min-size", type=float, default=0, help="跳过小于此大小(MB)的文件，如 --min-size 1")
    parser.add_argument("--skip-tier1", action="store_true", help="跳过 Tier1，复用现有输出")
    parser.add_argument("--skip-tier2", action="store_true", help="跳过 Tier2，复用现有输出")
    parser.add_argument("--skip-tier3", action="store_true", help="跳过 Tier3，复用现有输出")
    parser.add_argument("--model", type=str, default="", help="临时覆盖模型名")
    parser.add_argument("--temperature", type=float, default=0.4, help="模型温度")
    return parser.parse_args()


def main() -> None:
    global GLM_MODEL, GLM_TEMPERATURE
    args = parse_args()
    # Ollama 后端不需要 API key
    if LLM_BACKEND != "ollama":
        require_api_key()
    if args.model:
        GLM_MODEL = args.model
    GLM_TEMPERATURE = args.temperature
    print("=" * 60)
    print(f"三层蒸馏 - 第{args.round}轮（标准知识条目版）")
    print("=" * 60)
    if LLM_BACKEND == "ollama":
        print(f"LLM 后端: Ollama ({OLLAMA_MODEL} @ {OLLAMA_BASE_URL})")
        print(f"  temperature={OLLAMA_TEMPERATURE}, num_ctx={OLLAMA_NUM_CTX}, repeat_penalty={OLLAMA_REPEAT_PENALTY}")
        if TIER2_CONCURRENCY > 1:
            print(f"  tier2_concurrency={TIER2_CONCURRENCY}")
        # Health check（统一使用 _check_ollama_health，含冷启动探测）
        if not _check_ollama_health():
            print(f"  [FATAL] Ollama 健康检查失败，请先启动: ollama serve")
            sys.exit(1)
    else:
        print(f"LLM 后端: GLM ({GLM_MODEL})")
    print(f"SOURCE_BASE = {SOURCE_BASE}")
    print(f"OUTPUT_BASE = {OUTPUT_BASE}")
    print("=" * 60)

    books = select_books(args)
    processed: List[Dict[str, Any]] = []
    for index, (genre, book_file) in enumerate(books, start=1):
        book_name = Path(book_file).stem
        if should_skip_book(book_name, genre, book_file, args):
            continue
        error_message = ""
        try:
            success = process_book(genre, book_file, round_num=args.round, book_index=index, args=args)
        except Exception as exc:  # noqa: BLE001
            success = False
            error_message = str(exc)
            print(f"[失败] {book_name}: {exc}")
        book_record = {
            "genre": genre,
            "book_file": book_file,
            "book_name": book_name,
            "success": success,
            "error_message": error_message,
            "processed_at": now_iso(),
        }
        processed.append(book_record)

    update_progress(processed, round_num=args.round, total_books=len(books))
    report = build_round_report(args.round, processed)
    dump_json(OUTPUT_BASE / f"round{args.round}_report.json", report)

    print("\n" + "=" * 60)
    print(f"第{args.round}轮处理完成")
    print("=" * 60)
    print(f"成功: {report['books_processed']}本")
    print(f"失败: {report['books_failed']}本")
    print(f"进度文件: {OUTPUT_BASE / 'three_tier_progress.json'}")
    print(f"本轮报告: {OUTPUT_BASE / f'round{args.round}_report.json'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
