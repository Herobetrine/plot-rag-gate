#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


DISABLE_ENV = "PLOT_RAG_GATE_DISABLED"
_SESSION_CLOSE_LOCK_GUARD = threading.Lock()
_SESSION_CLOSE_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_SESSION_CLOSE_LOCK_TIMEOUT_SECONDS = 2.0
_SESSION_CLOSE_LOCK_POLL_SECONDS = 0.05
SHORT_CONTINUE = {
    "继续",
    "继续推进",
    "接着来",
    "往下",
    "下一步",
    "继续写",
    "接着写",
    "往下写",
    "写下一章",
}
GENERIC_CONTINUE_RE = re.compile(
    r"^(?:"
    r"继续|继续推进|接着来|往下|下一步|继续写|接着写|往下写|"
    r"开始|开始吧|开干|照此执行|按这个来|按计划推进|"
    r"一口气推进到底(?:，?最后再审查)?|继续推进到底(?:，?最后再审查)?"
    r")[。！？!?.…，,;；:：]*$"
)
NEUTRAL_RE = re.compile(
    r"^(?:好|好的|行|可以|明白|收到|谢谢|辛苦了|就这样)[。！？!?.…，,;；:：]*$"
)
PLOT_TERMS = "剧情|情节|故事|事件链|场景|正文|章节|章纲|卷纲|主线|支线|第[一二三四五六七八九十百千0-9]+章"
ACTION_TERMS = "继续|推进|展开|开始|进入|重新设计|设计|重新规划|规划|创作|写|续写|安排|发展"
NARRATIVE_TERMS = "推演|演绎|接下来|然后呢|下一幕|下一场|后续|会发生什么|怎么发展|如何发展"
NEGATION_TERMS = "不要|不再|停止|暂停|先别|暂时不要|暂不|无需|不用|不需要"
TASK_NEGATION_RE = re.compile(
    rf"(?:{NEGATION_TERMS}|(?<!区)别|禁止|不准|切勿|严禁|莫要)"
    rf".{{0,12}}(?:{ACTION_TERMS}|{PLOT_TERMS})"
)
ANALYSIS_PREFIXES = ("分析", "审查", "检查", "解释", "总结", "评价", "讨论", "研究", "查询", "检索")
META_SUBJECT_RE = re.compile(
    r"(?:插件|门禁|钩子|触发器|关键词|指令|命令|功能|机制|工作原理|执行流程|"
    r"(?<![A-Za-z0-9_])(?:hook|rag)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
META_BEHAVIOR_RE = re.compile(
    r"(?:会|将会|是否|能否|什么|怎样|怎么|如何|为什么|何时|触发|启动|执行|运行|工作|调用|作用|用途|流程)"
)
META_MENTION_RE = re.compile(
    r"(?:提到|说出|输入|使用|写下|出现).{0,12}[“\"'‘]?(?:剧情推演|推演剧情)[”\"'’]?"
)
META_DEFINITION_RE = re.compile(
    r"(?:剧情推演|推演剧情).{0,20}(?:是什么|什么意思|怎么用|如何用|怎么进行|如何进行|有哪些步骤|步骤是什么|会怎样|会做什么|什么流程|触发什么|是否触发|如何工作|怎么工作)"
)
PLOT_GATE_META_RE = re.compile(
    r"(?:剧情推演|推演剧情|剧情RAG|RAG门禁).{0,24}(?:"
    r"(?:功能|执行流程|流程|步骤|关键词|正则|触发器|钩子|代码|脚本|版本|机制|工作原理)|"
    r"(?:增加|添加|补充|完善).{0,8}(?:测试|用例|功能)|"
    r"(?:升级|优化|修复|审查|检查|验证|调试).{0,10}"
    r"(?:功能|流程|关键词|正则|触发器|钩子|插件|代码|脚本|测试|机制)"
    r")|"
    r"(?:修复|修改|优化|升级|重构|测试|审查|检查|验证|调试|开发|实现|维护|完善|增加|添加|补充)"
    r".{0,24}(?:剧情推演|推演剧情|剧情RAG|RAG门禁).{0,16}"
    r"(?:功能|流程|关键词|正则|触发器|钩子|插件|代码|脚本|测试|机制|用例)|"
    r"(?:升级|优化|修复|重构|测试|审查|检查|验证|完善).{0,16}"
    r"(?:剧情|网文|写作).{0,16}"
    r"(?:功能|机制|流程|插件|门禁|钩子|触发器|关键词|正则|代码|脚本|测试)",
    re.IGNORECASE,
)
CREATIVE_TRIGGER_PHRASE = (
    r"(?:剧情推演|推演剧情|推演下一章|写下一章|续一章|再来一章|"
    r"把下一章写出来|接着上一章写|把章纲扩成正文)"
)
TRIGGER_PHRASE_DISCUSSION_RE = re.compile(
    rf"(?:当我|如果我|我)(?:说|输入|提到|写下|使用).{{0,12}}"
    rf"[“\"'‘]?{CREATIVE_TRIGGER_PHRASE}[”\"'’]?.{{0,12}}"
    r"(?:会|是否|能否).{0,8}(?:触发|启动|进入|调用)"
    r"(?:剧情RAG|插件|门禁|Grill)?|"
    r"(?:当我|如果我|我)(?:说|输入|提到|写下|使用).{0,24}"
    r"(?:会|是否|能否).{0,8}(?:触发|启动|进入|调用)"
    r"(?:剧情RAG|插件|门禁|Grill)|"
    rf"[“\"'‘]{CREATIVE_TRIGGER_PHRASE}[”\"'’].{{0,12}}"
    r"(?:会|是否|能否).{0,8}(?:触发|启动|进入|调用)"
    r"(?:剧情RAG|插件|门禁|Grill)?|"
    rf"{CREATIVE_TRIGGER_PHRASE}.{{0,8}}(?:的)?"
    r"(?:触发规则|触发条件|触发机制|触发逻辑|判定规则)"
    r".{0,12}(?:是什么|怎么|如何|有哪些)?|"
    rf"{CREATIVE_TRIGGER_PHRASE}.{{0,12}}"
    r"(?:会|是否|能否).{0,8}(?:触发|启动|进入|调用)"
    r"(?:剧情RAG|插件|门禁|Grill)",
    re.IGNORECASE,
)
DIRECT_PLOT_COMMAND_RE = re.compile(
    r"(?:请|直接|马上|立即|帮我)(?:用|使用|通过)?(?:这个)?(?:剧情RAG)?(?:插件|门禁)?(?:来)?"
    r"(?:推演|推进|规划|设计|写|续写)(?:下一章|第[一二三四五六七八九十百千0-9]+章|"
    r"后续剧情|剧情|情节|故事|事件链|场景|正文|章纲|卷纲)|"
    r"(?:现在)?(?:开始|继续|接着)(?:进行)?(?:剧情|情节|故事)(?:推演|推进|规划|创作|写作|续写|写)?|"
    r"(?:现在)?(?:开始|继续|接着)(?:进行)?(?:推演|推进|规划|创作|写作|续写|写)"
    r"(?:剧情|情节|故事|下一章|第[一二三四五六七八九十百千0-9]+章|事件链|章纲|卷纲|正文|场景)|"
    r"(?:剧情|情节|故事)(?:推演|推进|规划|创作|写作|续写)|"
    r"(?:推演|推进|规划|设计|续写|写)(?:下一章|第[一二三四五六七八九十百千0-9]+章|"
    r"后续剧情|事件链|章纲|卷纲|正文|场景)"
)
NATURAL_PLOT_COMMAND_RE = re.compile(
    r"(?:续|再来|再写)(?:上|下|这|本|当前|前)?(?:一)?"
    r"(?:章|回|节|幕|场)(?:正文)?|"
    r"接着(?:上|前|这|本|当前)?(?:一)?(?:章|回|节|幕|场)"
    r"(?:继续)?(?:写|续写)?|"
    r"(?:把|将)?(?:下一章|上一章|这一章|本章|新章|当前章节|这一幕|下一幕|"
    r"这段正文|当前段落|章纲|卷纲).{0,12}"
    r"(?:写(?:完|出来|下去)?|续写|扩写|补写|展开|扩成|改成|整理成)"
    r"(?:为|成)?(?:正文|场景)?|"
    r"(?:给我)?(?:来|写|创作)(?:一|这|下)?(?:段|篇|章|回|幕|场)?"
    r"(?:正文|章节|场景)|"
    r"(?:规划|设计|安排|推演|展开).{0,12}"
    r"(?:本卷(?:后半段)?|后半卷|卷末)"
    r"(?:.{0,12}(?:冲突|情节|事件|发展|剧情|戏))?|"
    r"(?:规划|设计|安排|推演|展开).{0,12}"
    r"(?:接下来(?:的)?|后续(?:的)?|后面(?:的)?|下一场|下一幕)"
    r".{0,12}(?:冲突|情节|事件|发展|剧情|戏)"
)
QUOTED_TEXT_RE = re.compile(r"[“\"'‘]([^”\"'’]{1,120})[”\"'’]")
QUOTED_PLOT_META_QUESTION_RE = re.compile(
    r"(?:为什么|为何|是什么意思|什么含义|如何理解|怎么理解|是否|能否|会不会)|"
    r"(?:会|将会).{0,8}(?:触发|启动|进入|调用|执行|运行)|"
    r"(?:触发|启动|进入|调用|执行|运行).{0,8}(?:什么|怎样|怎么|如何|吗)"
)
META_TASK_RE = re.compile(
    r"(?:插件|门禁|钩子|仓库|代码|脚本|实现|重构|修复|测试|"
    r"版本|升级计划|改造计划|初始化|结构化|数据模型|协议|"
    r"流程设计|框架设计|功能设计|运行面|缓存|发布|校验|审查插件|"
    r"(?<![A-Za-z0-9_])(?:hook|rag|git|schema|cli|mcp)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
ACTIVE_GRILL_META_FOLLOWUP_RE = re.compile(
    r"(?:刚才(?:那个|这个)?问题|这个问题|那个问题|"
    r"继续.{0,8}(?:修|改|查|审|测|处理|解决)|"
    r"重启后|现在(?:状态|情况|怎么样)|它为什么|彻底(?:修|改))",
    re.IGNORECASE,
)
ACTIVE_GRILL_STORY_CONTEXT_RE = re.compile(
    r"(?:主角|角色|人物|反派|守卫|宗门|法器|功法|古籍|符文|法术|"
    r"魔法|修仙|真元|灵气|境界|能力|技能|道具|世界|剧情|情节|"
    r"故事|章节|正文|网文|悬疑|背叛|放行)"
)
INJECTED_CONTEXT_RE = re.compile(
    r"<(?P<tag>environment_context|recommended_plugins|system-reminder|app-context|permissions)"
    r"\b[^>]*>.*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _assistant_text(payload: dict[str, Any]) -> str:
    for key in (
        "last_assistant_message",
        "assistant_response",
        "assistant_text",
        "response",
        "output",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(transcript, str) or not transcript.strip():
        return ""
    try:
        lines = Path(transcript).expanduser().read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        message = item.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {None, "text", "output_text"}
            ).strip()
            if text:
                return text
    return ""


def _cwd(payload: dict[str, Any]) -> Path:
    value = payload.get("cwd") or payload.get("project_dir") or os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(str(value or os.getcwd())).expanduser().resolve()


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts"


def _load_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from plot_rag import load_config, locate_project_root

    return locate_project_root, load_config


def _load_state_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from state_rag import doctor
    from v1_runtime import prepare_plot_turn, propose_plot_turn

    return prepare_plot_turn, propose_plot_turn, doctor


def _load_event_experience_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import event_experience

    return event_experience


def _load_event_experience_orchestrator():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import event_experience_runtime

    return event_experience_runtime


def _load_extraction_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import extraction_jobs

    return extraction_jobs


def _infer_artifact_context(prompt: str) -> dict[str, Any]:
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from v1_runtime import infer_artifact_context

    return dict(infer_artifact_context(prompt))


def _load_initialization_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from plot_init import (
        PlotInitService,
        arbitrate_initialization_hook,
        is_initialization_meta_prompt,
    )
    from v1_runtime import init_service

    return (
        PlotInitService,
        arbitrate_initialization_hook,
        init_service,
        is_initialization_meta_prompt,
    )


def _load_grill_runtime():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import grill_gate

    return grill_gate


def _quoted_plot_meta_discussion(prompt: str) -> bool:
    if not QUOTED_PLOT_META_QUESTION_RE.search(prompt):
        return False
    return any(
        DIRECT_PLOT_COMMAND_RE.search(quoted)
        or NATURAL_PLOT_COMMAND_RE.search(quoted)
        or re.search(CREATIVE_TRIGGER_PHRASE, quoted)
        for quoted in QUOTED_TEXT_RE.findall(prompt)
    )


def is_meta_plot_discussion(prompt: str) -> bool:
    """Return true when plot terms are mentioned to discuss the gate itself."""

    text = re.sub(r"\s+", "", str(prompt or "")).strip()
    if not text:
        return False
    mentions_trigger_phrase = bool(META_MENTION_RE.search(text))
    defines_trigger_phrase = bool(META_DEFINITION_RE.search(text))
    discusses_gate_work = bool(PLOT_GATE_META_RE.search(text))
    discusses_trigger_wording = bool(TRIGGER_PHRASE_DISCUSSION_RE.search(text))
    discusses_runtime = bool(META_SUBJECT_RE.search(text) and META_BEHAVIOR_RE.search(text))
    return bool(
        defines_trigger_phrase
        or (mentions_trigger_phrase and META_BEHAVIOR_RE.search(text))
        or discusses_gate_work
        or discusses_trigger_wording
        or discusses_runtime
        or _quoted_plot_meta_discussion(text)
    )


def _plain_prompt(prompt: str) -> str:
    text = re.sub(r"\s+", "", str(prompt or "")).strip()
    return re.sub(r"[。！？!?.…，,;；:：]+$", "", text)


def _raw_plot_progression(plain: str) -> bool:
    if not plain:
        return False
    if TASK_NEGATION_RE.search(plain):
        return False
    if plain.startswith(ANALYSIS_PREFIXES):
        return False
    if is_meta_plot_discussion(plain):
        return False
    if re.fullmatch(r"(?:继续|接着|往下)(?:写|创作|续写)", plain):
        return True
    forward = re.search(rf"(?:{ACTION_TERMS}).{{0,14}}(?:{PLOT_TERMS})", plain)
    reverse = re.search(rf"(?:{PLOT_TERMS}).{{0,14}}(?:{ACTION_TERMS})", plain)
    narrative = re.search(rf"(?:{NARRATIVE_TERMS}).{{0,18}}(?:{PLOT_TERMS})", plain)
    implicit = re.search(rf"(?:{PLOT_TERMS}).{{0,18}}(?:{NARRATIVE_TERMS})", plain)
    direct_question = bool(re.search(r"(?:接下来|然后|随后|下一步).{0,20}(?:发生|发展|做什么|怎么办)", plain))
    return bool(forward or reverse or narrative or implicit or direct_question)


def _classification_prompt(
    prompt: str,
    skip_phrases: Sequence[str],
) -> str:
    plain = _plain_prompt(prompt)
    if not plain or not skip_phrases:
        return plain
    try:
        runtime = _load_grill_runtime()
        return _plain_prompt(
            runtime.mask_terms_in_phrases(
                plain,
                skip_phrases,
                NEGATION_TERMS.split("|"),
            )
        )
    except Exception:
        return plain


def classify_task_family(
    prompt: str,
    *,
    skip_phrases: Sequence[str] = (),
) -> str:
    """Classify a user turn without treating generic continuation as plot."""

    raw_plain = _plain_prompt(prompt)
    if raw_plain and is_meta_plot_discussion(raw_plain):
        return "meta_init"
    plain = _classification_prompt(prompt, skip_phrases)
    if not plain:
        return "neutral"
    if TASK_NEGATION_RE.search(plain):
        return "other_work"
    if GENERIC_CONTINUE_RE.fullmatch(plain):
        return "continuation"
    if NEUTRAL_RE.fullmatch(plain):
        return "neutral"
    if is_meta_plot_discussion(plain):
        return "meta_init"
    if (
        DIRECT_PLOT_COMMAND_RE.search(plain)
        or NATURAL_PLOT_COMMAND_RE.search(plain)
        or plain == "写下一章"
    ):
        return "plot"
    if META_TASK_RE.search(plain):
        return "meta_init"
    if _raw_plot_progression(plain):
        return "plot"
    return "other_work"


def is_unrelated_grill_meta_work(prompt: str) -> bool:
    """Keep an active Grill intact when the user temporarily switches to plugin work."""

    text = str(prompt or "").strip()
    if not text:
        return False
    try:
        _, _, _, classifier = _load_initialization_runtime()
    except Exception:
        return False
    return bool(classifier(text))


def is_plot_progression(
    prompt: str,
    *,
    allow_short_continue: bool = True,
    skip_phrases: Sequence[str] = (),
) -> bool:
    family = classify_task_family(prompt, skip_phrases=skip_phrases)
    return family == "plot" or (family == "continuation" and allow_short_continue)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {None, "text", "input_text", "output_text"}
        ).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "message"):
            value = content.get(key)
            text = _content_text(value)
            if text:
                return text
    return ""


def _transcript_user_message(item: dict[str, Any]) -> tuple[str, str] | None:
    """Extract Codex or Claude JSONL user messages, ignoring unknown records."""

    if item.get("type") == "response_item":
        message = item.get("payload")
        if (
            isinstance(message, dict)
            and message.get("type") == "message"
            and message.get("role") == "user"
        ):
            metadata = message.get("internal_chat_message_metadata_passthrough")
            turn_id = (
                str(metadata.get("turn_id") or "")
                if isinstance(metadata, dict)
                else ""
            )
            return _content_text(message.get("content")), turn_id
    message = item.get("message")
    if item.get("type") == "user" and isinstance(message, dict):
        return _content_text(message.get("content")), str(
            item.get("turn_id") or item.get("turnId") or ""
        )
    if item.get("role") == "user":
        return _content_text(item.get("content")), str(
            item.get("turn_id") or item.get("turnId") or ""
        )
    return None


def _clean_transcript_prompt(text: str) -> str:
    return INJECTED_CONTEXT_RE.sub("", str(text or "")).strip()


def _effective_initialization_turn_identity(
    payload: dict[str, Any],
    prompt: str,
    *,
    max_lines: int = 400,
) -> str:
    """Resolve a caller-stable turn identity without inventing one from state."""

    turn_id = str(payload.get("turn_id") or payload.get("turnId") or "").strip()
    if turn_id:
        return turn_id
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(transcript, str) or not transcript.strip():
        return ""
    try:
        lines = (
            Path(transcript)
            .expanduser()
            .read_text(encoding="utf-8-sig")
            .splitlines()
        )
    except OSError:
        return ""
    current_plain = _plain_prompt(prompt)
    if not current_plain:
        return ""
    first_index = max(0, len(lines) - max_lines)
    for index in range(len(lines) - 1, first_index - 1, -1):
        line = lines[index]
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        extracted = _transcript_user_message(item)
        if extracted is None:
            continue
        text, item_turn_id = extracted
        if _plain_prompt(_clean_transcript_prompt(text)) != current_plain:
            continue
        if item_turn_id:
            return item_turn_id
        digest = hashlib.sha256(
            f"{index}\n{line}".encode("utf-8")
        ).hexdigest()
        return f"transcript:{digest}"
    return ""


def recent_task_classification(
    payload: dict[str, Any],
    current_prompt: str,
    *,
    max_lines: int = 400,
    skip_phrases: Sequence[str] = (),
) -> str | None:
    """Return the latest decisive user task before the current continuation."""

    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    try:
        lines = Path(transcript).expanduser().read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    current_turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
    current_plain = _plain_prompt(current_prompt)
    skipped_current = False
    for line in reversed(lines[-max_lines:]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        extracted = _transcript_user_message(item)
        if extracted is None:
            continue
        text, item_turn_id = extracted
        text = _clean_transcript_prompt(text)
        if not text:
            continue
        if not skipped_current and _plain_prompt(text) == current_plain:
            if not current_turn_id or not item_turn_id or item_turn_id == current_turn_id:
                skipped_current = True
                continue
        family = classify_task_family(text, skip_phrases=skip_phrases)
        if family in {"continuation", "neutral"}:
            continue
        return family
    return None


def is_unrelated_grill_meta_turn(
    payload: dict[str, Any],
    prompt: str,
) -> bool:
    """Recognize explicit maintenance work and terse follow-ups to such work."""

    if is_unrelated_grill_meta_work(prompt):
        return True
    plain = _plain_prompt(prompt)
    if ACTIVE_GRILL_STORY_CONTEXT_RE.search(plain):
        return False
    if not plain or not ACTIVE_GRILL_META_FOLLOWUP_RE.search(plain):
        return False
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(transcript, str) or not transcript.strip():
        return False
    try:
        lines = (
            Path(transcript)
            .expanduser()
            .read_text(encoding="utf-8-sig")
            .splitlines()
        )
    except OSError:
        return False

    current_plain = _plain_prompt(prompt)
    skipped_current = False
    for line in reversed(lines[-400:]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        extracted = _transcript_user_message(item)
        if extracted is None:
            continue
        text, _ = extracted
        text = _clean_transcript_prompt(text)
        if not text:
            continue
        if not skipped_current and _plain_prompt(text) == current_plain:
            skipped_current = True
            continue
        if is_unrelated_grill_meta_work(text):
            return True
        previous_plain = _plain_prompt(text)
        if ACTIVE_GRILL_STORY_CONTEXT_RE.search(previous_plain):
            return False
        if ACTIVE_GRILL_META_FOLLOWUP_RE.search(previous_plain):
            continue
        if classify_task_family(text) in {"continuation", "neutral"}:
            continue
        return False
    return False


def resolve_plot_progression(
    payload: dict[str, Any],
    prompt: str,
    *,
    allow_short_continue: bool,
    skip_phrases: Sequence[str] = (),
) -> bool:
    family = classify_task_family(prompt, skip_phrases=skip_phrases)
    if family == "plot":
        return True
    if family != "continuation" or not allow_short_continue:
        return False
    previous = recent_task_classification(
        payload,
        prompt,
        skip_phrases=skip_phrases,
    )
    if previous is None:
        return True
    return previous == "plot"


def _advantage_hook_context(
    state_result: Mapping[str, Any] | None,
) -> str:
    if not isinstance(state_result, Mapping):
        return ""
    direct = state_result.get("advantage_context")
    longform = state_result.get("longform")
    nested = (
        longform.get("advantage_context")
        if isinstance(longform, Mapping)
        else None
    )
    value = direct if isinstance(direct, Mapping) else nested
    if not isinstance(value, Mapping) or not bool(value.get("required")):
        return ""
    triggers = (
        dict(value.get("triggers") or {})
        if isinstance(value.get("triggers"), Mapping)
        else {}
    )
    stable_ids = (
        dict(value.get("stable_ids") or {})
        if isinstance(value.get("stable_ids"), Mapping)
        else {}
    )

    def joined(raw: Any) -> str:
        if not isinstance(raw, Sequence) or isinstance(
            raw,
            (str, bytes, bytearray),
        ):
            return ""
        return ",".join(str(item) for item in raw if str(item))

    lines = [
        "[PLOT_RAG_ADVANTAGE_HOOK]",
        "phase: post_locked_intent_and_event_experience_prepare",
        f"status: {value.get('status') or 'unknown'}",
        "required: true",
        (
            "gate_action: proceed"
            if str(value.get("status") or "").casefold() == "ready"
            else "gate_action: block"
        ),
        "mandatory_context_marker: [ACCEPTED_ADVANTAGE_CONTEXT]",
        f"trigger_layers: {joined(triggers.get('layers'))}",
        f"special_terms: {joined(triggers.get('special_terms'))}",
        f"actions: {joined(triggers.get('actions'))}",
        (
            "continuity_signals: "
            + joined(triggers.get("continuity_signals"))
        ),
        (
            "requested_advantage_ids: "
            + joined(stable_ids.get("advantage_ids"))
        ),
        "requested_module_ids: " + joined(stable_ids.get("module_ids")),
        (
            "selected_advantage_ids: "
            + joined(value.get("selected_advantage_ids"))
        ),
        (
            "selected_module_ids: "
            + joined(value.get("selected_module_ids"))
        ),
        (
            "Advantage 上下文已在 Intent 与 EventExperience 锁定后由 "
            "Prepare 注入；生成时必须复用稳定 ID，并服从 knowledge_plane、"
            "reveal_stage、runtime、ledger 与 exposure。"
        ),
        "[/PLOT_RAG_ADVANTAGE_HOOK]",
    ]
    if str(value.get("status") or "").casefold() != "ready":
        lines.insert(
            -1,
            (
                "mandatory Advantage context is not ready; do not generate, "
                "propose, or record plot progression for this turn."
            ),
        )
    return "\n".join(lines)


def _context(
    project_root: Path,
    request_id: str,
    config_error: str | None = None,
    state_result: dict[str, Any] | None = None,
) -> str:
    cli = _scripts_dir() / "plot_rag.py"
    lines = [
            "[PLOT_RAG_GATE:剧情推进检索门禁]",
            f"request_id: {request_id}",
            f"project_root: {project_root}",
    ]
    if config_error:
        lines.extend(
            [
                "gate_status: INDEX_UNAVAILABLE",
                f"reason: {config_error}",
                "当前请求不得继续推进剧情；先修复本插件的项目配置或索引。",
            ]
        )
        return "\n".join(lines)
    if state_result is not None:
        state_status = str(state_result.get("status") or "failed")
        receipt_id = str(state_result.get("receipt_id") or state_result.get("receipt") or "")
        lifecycle_mode = str(state_result.get("lifecycle_mode") or "legacy_commit")
        lines.extend(
            [
                f"state_retrieval_status: {state_status}",
                f"state_receipt_id: {receipt_id or 'unavailable'}",
                f"lifecycle_mode: {lifecycle_mode}",
                "以下内容已由 hook 自动检索；精确当前状态以 SQLite 投影为准，语义 RAG 只负责召回：",
                str(state_result.get("context") or "[STATE_RAG_CONTEXT_UNAVAILABLE]"),
            ]
        )
        advantage_context = _advantage_hook_context(state_result)
        if advantage_context:
            lines.append(advantage_context)
        if state_status in {"failed", "error"}:
            lines.extend(
                [
                    f"state_retrieval_reason: {state_result.get('reason', 'unknown')}",
                    "状态检索故障不得解释成事实缺失；不要覆盖或猜测当前状态。",
                ]
            )
    lines.extend(
        [
            "自动 broad preflight 不能替代原子事实核验。在提出情节、章纲或正文前，仍需从目标拆出1至5条原子事实需求。",
            "对自动上下文未覆盖的关键事实，运行独立查询器并至少提供一个同义问法：",
            f'python -B -X utf8 "{cli}" --project-root "{project_root}" --request-id "{request_id}" --need "<事实需求>" --alias "<同义问法>"',
            "状态处理：",
            "- HIT_CONFIRMED：只使用返回的原文证据推进，不扩设定。",
            "- AMBIGUOUS：读取命中原文或请用户裁决，不扩设定。",
            "- INDEX_UNAVAILABLE：先修复项目配置或索引，不得当作设定缺失。",
            "- MISS_CONFIRMED：暂停剧情，仅就当前缺口进入交互式设定拓展；用户确认并写回权威文件后，重跑查询变为HIT才能继续。",
            "禁止把搜索报错、单次空结果、低相关候选或自己的常识推断视为MISS_CONFIRMED。",
            "若上下文包含 [CRAFT_RAG_GUIDANCE]，必须在内部按任务层级选择并组合方法卡，把方法落实为人物目标、对立行动、预期落差、困难选择、状态变化和后续问题；不得机械套模板，也不得让方法覆盖权威事实。",
            "输出完成后 Stop hook 只生成带逐字证据的状态 proposal；config v3 下必须经过一次性 approval grant、CAS 和显式 accept 才能进入正典事件账本，备选、假设和未来计划不得覆盖当前状态。",
        ]
    )
    return "\n".join(lines)


def _find_project(start: Path):
    try:
        locate_project_root, load_config = _load_runtime()
    except Exception as exc:
        return _fallback_project(start), None, f"独立检索器加载失败: {exc}"
    root = locate_project_root(start)
    if root is None:
        return None, None, None
    try:
        config = load_config(root)
    except Exception as exc:
        return root, None, str(exc)
    return root, config, None


def _effective_grill_config(
    project_root: Path,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(config, dict) and isinstance(config.get("grill"), dict):
        return dict(config["grill"])
    runtime = _load_grill_runtime()
    return {
        "enabled": True,
        "schema_version": runtime.INTENT_SCHEMA_VERSION,
        "database_path": str(project_root / ".plot-rag" / "grill.sqlite3"),
        "one_question_per_turn": True,
        "recommend_answer": True,
        "explore_project_first": True,
        "max_questions": 6,
        "session_ttl_seconds": 21600,
        "required_fields": list(runtime.DEFAULT_REQUIRED_FIELDS),
        "skip_phrases": list(runtime.DEFAULT_SKIP_PHRASES),
        "cancel_phrases": list(runtime.DEFAULT_CANCEL_PHRASES),
    }


def _grill_service(
    project_root: Path,
    config: dict[str, Any] | None,
) -> tuple[Any, Any, dict[str, Any]]:
    runtime = _load_grill_runtime()
    grill_config = _effective_grill_config(project_root, config)
    database_path = Path(
        str(
            grill_config.get("database_path")
            or project_root / ".plot-rag" / "grill.sqlite3"
        )
    ).expanduser()
    if not database_path.is_absolute():
        database_path = project_root / database_path
    return runtime, runtime.GrillGateService(database_path), grill_config


def _initialization_start_requested(
    payload: dict[str, Any],
    prompt: str,
) -> bool:
    try:
        _, arbitrate, _, _ = _load_initialization_runtime()
        decision = arbitrate(
            {
                **payload,
                "hook_event_name": "UserPromptSubmit",
                "prompt": prompt,
            },
            active_session=None,
        )
    except Exception:
        return False
    return str(decision.get("action") or "") == "start"


def _grill_project_probe(
    project_root: Path,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    sources = (
        config.get("authority_sources")
        if isinstance(config, dict)
        and isinstance(config.get("authority_sources"), list)
        else []
    )
    return {
        "project_root": str(project_root),
        "config_loaded": isinstance(config, dict),
        "authority_rules": len(sources),
        "continuity_store_exists": (
            project_root / ".plot-rag" / "state.sqlite3"
        ).is_file(),
        "initialization_store_exists": (
            project_root / ".plot-rag" / "init.sqlite3"
        ).is_file(),
    }


def _grill_context(
    decision: dict[str, Any],
    *,
    project_root: Path,
    config: dict[str, Any],
    project_config: dict[str, Any] | None,
) -> str:
    action = str(decision.get("action") or "ask")
    lines = [
        "[PLOT_RAG_GRILL]",
        f"schema_version: {decision.get('contract', {}).get('schema_version') or 'plot-rag-intent/v1'}",
        f"action: {action}",
        f"reason: {decision.get('reason') or 'unknown'}",
        f"grill_session_id: {decision.get('grill_session_id') or ''}",
        f"task_family: {decision.get('task_family') or ''}",
    ]
    if bool(config.get("explore_project_first", True)):
        lines.extend(
            [
                "project_probe:",
                json.dumps(
                    _grill_project_probe(project_root, project_config),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "角色位置、道具、力量状态、关系和故事时间等可从项目检索的事实不得反问用户；合同锁定后由 RAG 读取。",
            ]
        )

    if action in {"ask", "inspect"}:
        question = (
            decision.get("question")
            if isinstance(decision.get("question"), dict)
            else {}
        )
        if action == "inspect":
            lines.append(
                f"先用一句话说明当前还剩 {decision.get('remaining_questions', 0)} 个未锁定字段，然后重复当前问题。"
            )
        lines.extend(
            [
                "本轮由 Grill 独占：不得执行剧情检索、剧情推演、初始化、规划或写作；不得创建 receipt。",
                "只处理下面这一个问题，不追加第二个问题、方案、正文或任务结果。",
                f"Q{question.get('index', 1)}/{question.get('total', 1)}: {question.get('text') or ''}",
            ]
        )
        if bool(config.get("recommend_answer", True)):
            lines.extend(
                [
                    f"Recommended answer: {question.get('recommended_answer') or ''}",
                    f"Reason: {question.get('recommendation_rationale') or ''}",
                ]
            )
        if bool(config.get("recommend_answer", True)):
            lines.append(
                "只有“按推荐答案 / 你来定”等明确委托才视为接受推荐；“继续 / 开始吧 / 下一步”只重复当前问题。"
            )
        else:
            lines.append(
                "请直接回答当前问题；“继续 / 开始吧 / 下一步”只重复当前问题。"
            )
        skip_phrases = list(config.get("skip_phrases") or [])
        if skip_phrases:
            lines.append(
                "用户可显式跳过："
                + " / ".join(str(item) for item in skip_phrases[:3])
            )
    elif action == "cancel":
        lines.extend(
            [
                "本轮 Grill 已结束；只简短确认，不执行原剧情任务，也不生成剧情 receipt。",
            ]
        )
    elif action == "conflict":
        lines.extend(
            [
                "同一 turn_id 收到了不同请求；保持既有 Grill 状态不变。",
                "只提示用户重新发送本轮回答，不执行剧情任务。",
            ]
        )
    elif action == "proceed":
        lines.extend(
            [
                "shared_understanding_reached: true",
                "Intent Contract 已锁定；本轮直接进入对应工作流，不再重复盘问。",
                "locked_contract:",
                json.dumps(
                    decision.get("contract") or {},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            ]
        )
    lines.append("[/PLOT_RAG_GRILL]")
    return "\n".join(lines)


def _prepend_additional_context(
    output: dict[str, Any],
    context: str,
) -> dict[str, Any]:
    specific = output.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        return output
    existing = str(specific.get("additionalContext") or "")
    specific["additionalContext"] = (
        f"{context}\n\n{existing}" if existing else context
    )
    return output


def _initialization_handoff_succeeded(output: dict[str, Any]) -> bool:
    specific = output.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        return False
    context = str(specific.get("additionalContext") or "")
    status_match = re.search(r"(?m)^status:\s*(\S+)", context)
    session_match = re.search(r"(?m)^session_id:\s*(\S+)", context)
    status = str(status_match.group(1) if status_match else "").upper()
    session_id = str(session_match.group(1) if session_match else "")
    return bool(session_id and status not in {"", "ERROR", "FAILED"})


def _fallback_project(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for candidate in (current, *current.parents):
        if (candidate / ".plot-rag").exists():
            return candidate
        pointer = candidate / ".plot-rag-current-project"
        if pointer.is_file():
            try:
                raw = pointer.read_text(encoding="utf-8-sig").strip().strip('"')
                if raw:
                    target = Path(raw).expanduser()
                    return (target if target.is_absolute() else pointer.parent / target).resolve()
            except OSError:
                return candidate
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class InitializationLookupError(RuntimeError):
    """Raised when an existing initialization store cannot be inspected safely."""


def _initialization_lookup_failure_output(reason: str) -> dict[str, Any]:
    message = (
        "初始化状态库读取失败；为避免跨工作流写入，剧情 prepare、初始化推进和 "
        f"Stop 抽取均已抑制。reason={reason}"
    )
    return {
        "decision": "block",
        "reason": "initialization_state_lookup_failed",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(
                (
                    "[PLOT_RAG_INITIALIZATION_ERROR]",
                    "status: INDEX_UNAVAILABLE",
                    "action: block",
                    message,
                    "[/PLOT_RAG_INITIALIZATION_ERROR]",
                )
            ),
        },
    }


def _initialization_services(
    cwd: Path,
    project_root: Path | None,
) -> list[Any]:
    """Find existing init stores without creating a database during arbitration."""

    try:
        PlotInitService, _, init_service, _ = _load_initialization_runtime()
    except Exception as exc:
        candidate_paths = [
            candidate / ".plot-rag-init" / "init.sqlite3"
            for candidate in (cwd, *cwd.parents)
        ]
        if project_root is not None:
            candidate_paths.append(
                project_root / ".plot-rag" / "init.sqlite3"
            )
        if any(path.is_file() for path in candidate_paths):
            raise InitializationLookupError(str(exc)) from exc
        return []
    services: list[Any] = []
    seen: set[str] = set()

    def add(service: Any) -> None:
        database = Path(service.database_path).expanduser().resolve()
        key = str(database).casefold()
        if key in seen or not database.is_file():
            return
        seen.add(key)
        services.append(service)

    if project_root is not None:
        try:
            add(init_service(project_root, project_root=project_root))
        except Exception as exc:
            if (project_root / ".plot-rag" / "init.sqlite3").is_file():
                raise InitializationLookupError(str(exc)) from exc
        add(
            PlotInitService(
                project_root,
                database_path=project_root / ".plot-rag" / "init.sqlite3",
            )
        )
    for candidate in (cwd, *cwd.parents):
        add(
            PlotInitService(
                candidate,
                database_path=candidate / ".plot-rag-init" / "init.sqlite3",
            )
        )
    return services


def _session_matches_context(
    session: dict[str, Any],
    *,
    cwd: Path,
    project_root: Path | None,
    workspace_root: Path,
) -> bool:
    raw_target = session.get("project_root")
    if raw_target:
        try:
            target = Path(str(raw_target)).expanduser().resolve()
        except OSError:
            return False
        if project_root is not None and target == project_root.resolve():
            return True
        return _inside(cwd, target) or _inside(target, cwd)
    return _inside(cwd, workspace_root) or _inside(workspace_root, cwd)


def _active_initialization(
    cwd: Path,
    project_root: Path | None,
    *,
    host_session_id: str = "",
) -> tuple[Any, dict[str, Any]] | None:
    candidates: list[tuple[Any, dict[str, Any]]] = []
    for service in _initialization_services(cwd, project_root):
        try:
            sessions = service.list(active_only=True).get("sessions") or []
        except Exception as exc:
            raise InitializationLookupError(
                f"{service.database_path}: {exc}"
            ) from exc
        for session in sessions:
            if _session_matches_context(
                session,
                cwd=cwd,
                project_root=project_root,
                workspace_root=Path(service.workspace_root),
            ):
                candidates.append((service, session))
    if not candidates:
        return None
    if host_session_id:
        exact = [
            item
            for item in candidates
            if str(item[1].get("host_session_id") or "")
            == str(host_session_id)
        ]
        if len(exact) > 1:
            matches = ", ".join(
                (
                    f"{Path(item[0].database_path).resolve()}"
                    f"#{item[1].get('session_id') or ''}"
                )
                for item in exact
            )
            raise InitializationLookupError(
                "ambiguous_active_initialization: "
                f"host_session_id={host_session_id}; matches={matches}"
            )
        if exact:
            return exact[0]
        return None
    return None


def _initialization_idempotency_key(
    payload: dict[str, Any],
    *,
    action: str,
    prompt: str,
    turn_identity: str,
) -> str:
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    digest = hashlib.sha256(
        f"{action}\n{session_id}\n{turn_identity}\n{prompt}".encode("utf-8")
    ).hexdigest()
    return f"hook:{action}:{digest}"


def _initialization_turn_replay(
    service: Any,
    session_id: str,
    payload: dict[str, Any],
    *,
    prompt: str,
    turn_identity: str,
) -> tuple[dict[str, Any], str] | None:
    """Replay a previously persisted mutation before reading the new revision."""

    candidates = (
        ("advance", f"{session_id}:advance"),
        ("answer", f"{session_id}:answer"),
        ("answer", f"{session_id}:advance"),
        ("propose", f"{session_id}:propose"),
        ("cancel", f"{session_id}:cancel"),
    )
    for action, scope in candidates:
        key = _initialization_idempotency_key(
            payload,
            action=action,
            prompt=prompt,
            turn_identity=turn_identity,
        )
        replay = service.storage.lookup_idempotency_key(scope, key)
        if replay is not None:
            return replay, action
    return None


def _initialization_context(
    result: dict[str, Any],
    *,
    classified_action: str,
    attempted_action: str,
    executed_action: str,
) -> str:
    session = result.get("session") if isinstance(result.get("session"), dict) else {}
    questions = (
        result.get("questions")
        or result.get("current_questions")
        or session.get("current_questions")
        or []
    )
    proposal = (
        result.get("proposal")
        if isinstance(result.get("proposal"), dict)
        else session.get("proposal")
        if isinstance(session.get("proposal"), dict)
        else {}
    )
    lines = [
        "[PLOT_RAG_INITIALIZATION]",
        f"action: {executed_action}",
        f"classified_action: {classified_action}",
        f"attempted_action: {attempted_action}",
        f"executed_action: {executed_action}",
        f"status: {result.get('status') or session.get('status') or 'unknown'}",
        f"stage: {result.get('stage') or session.get('stage') or 'unknown'}",
        f"session_id: {result.get('session_id') or session.get('session_id') or ''}",
        "初始化工作流已优先占用本轮；不得创建剧情 receipt，也不得从本轮 Stop 抽取剧情事件。",
    ]
    revision = result.get("session_revision") or session.get("session_revision")
    if revision is not None:
        lines.append(f"session_revision: {revision}")
    if questions:
        lines.extend(
            [
                "current_questions:",
                json.dumps(questions, ensure_ascii=False, indent=2),
            ]
        )
    if proposal:
        lines.append(f"proposal_id: {proposal.get('proposal_id') or ''}")
    if result.get("reason"):
        lines.append(f"reason: {result.get('reason')}")
    if str(result.get("status") or session.get("status") or "") == "PROPOSAL_FROZEN":
        lines.append(
            "提案已冻结；后续只可由真实交互式宿主签发一次性 grant，再由 apply 消费，hook 本身没有验收权限。"
        )
    lines.append("[/PLOT_RAG_INITIALIZATION]")
    return "\n".join(lines)


def _initialization_start_request(prompt: str) -> tuple[str, str | None]:
    """Separate an initialization operation command from creative seed content.

    Existing-project commands such as "整理现有作品" describe the requested
    operation, not a new story requirement. Feeding that command back as a seed
    makes auto routing select ``hybrid`` and can trigger unrelated genre
    questions. Explicit additions after the command still opt into hybrid.
    """

    text = str(prompt or "").strip()
    compact = re.sub(r"\s+", "", text)
    existing_markers = (
        "整理现有作品",
        "整理现有内容",
        "整理成标准结构",
        "整理成标准的结构化格式",
        "导入现有小说",
        "导入现有作品",
        "把现有正文",
        "将现有正文",
    )
    new_markers = (
        "从零初始化",
        "初始化一部作品",
        "创建新作",
        "建立一部新作",
    )
    if any(marker in compact for marker in existing_markers):
        addition_markers = (
            "并补充",
            "同时补充",
            "并新增",
            "同时新增",
            "并强化",
            "同时强化",
            "还希望",
            "同时希望",
        )
        addition_index = min(
            (
                text.find(marker)
                for marker in addition_markers
                if text.find(marker) >= 0
            ),
            default=-1,
        )
        if addition_index >= 0:
            seed = text[addition_index:].strip(" ，。；;：:")
            return "hybrid", seed or None
        return "ingest", None
    if any(marker in compact for marker in new_markers):
        return "new", text
    return "auto", text or None


def _handle_initialization_submit(
    payload: dict[str, Any],
    *,
    cwd: Path,
    project_root: Path | None,
    prompt: str,
    forced_action: str | None = None,
    active_initialization: tuple[Any, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    try:
        PlotInitService, arbitrate, init_service, _ = (
            _load_initialization_runtime()
        )
    except Exception:
        return None
    host_session_id = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    )
    if not host_session_id:
        return None
    active = active_initialization
    if active is None:
        active = _active_initialization(
            cwd,
            project_root,
            host_session_id=host_session_id,
        )
    active_session = active[1] if active else None
    turn_identity = _effective_initialization_turn_identity(payload, prompt)
    if active is not None and turn_identity:
        service, session = active
        try:
            replay = _initialization_turn_replay(
                service,
                str(session["session_id"]),
                payload,
                prompt=prompt,
                turn_identity=turn_identity,
            )
        except Exception as exc:
            result = {
                "status": "ERROR",
                "stage": session.get("stage") or "UNKNOWN",
                "session_id": session.get("session_id"),
                "reason": str(exc),
            }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _initialization_context(
                        result,
                        classified_action=str(forced_action or "replay"),
                        attempted_action="replay",
                        executed_action="error",
                    ),
                }
            }
        if replay is not None:
            result, replayed_action = replay
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _initialization_context(
                        result,
                        classified_action=replayed_action,
                        attempted_action=replayed_action,
                        executed_action="replay",
                    ),
                }
            }
    decision = arbitrate(
        {
            **payload,
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        },
        active_session=active_session,
    )
    classified_action = str(
        forced_action or decision.get("action") or "none"
    )
    if classified_action == "none":
        return None

    attempted_action = classified_action
    executed_action = classified_action
    mutating_actions = {"start", "advance", "answer", "propose", "cancel"}
    if classified_action in mutating_actions and not turn_identity:
        if active is not None:
            service, session = active
            try:
                result = dict(
                    service.inspect(
                        str(session["session_id"]),
                        view=(
                            "proposal"
                            if str(session.get("status") or "")
                            == "PROPOSAL_FROZEN"
                            else "summary"
                        ),
                    )
                )
                result.setdefault(
                    "reason",
                    "host turn identity unavailable; mutation suppressed",
                )
                executed_action = "inspect"
            except Exception as exc:
                result = {
                    "status": "ERROR",
                    "stage": session.get("stage") or "UNKNOWN",
                    "session_id": session.get("session_id"),
                    "reason": str(exc),
                }
                executed_action = "error"
        else:
            result = {
                "status": "IDENTITY_UNAVAILABLE",
                "stage": "START",
                "session_id": None,
                "reason": (
                    "host turn identity unavailable; initialization start "
                    "suppressed"
                ),
            }
            executed_action = "none"
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _initialization_context(
                    result,
                    classified_action=classified_action,
                    attempted_action=attempted_action,
                    executed_action=executed_action,
                ),
            }
        }

    key = (
        _initialization_idempotency_key(
            payload,
            action=classified_action,
            prompt=prompt,
            turn_identity=turn_identity,
        )
        if classified_action in mutating_actions
        else ""
    )
    try:
        if forced_action == "start" and active is not None:
            service, session = active
            result = service.inspect(str(session["session_id"]), view="summary")
            executed_action = "inspect"
        elif active is None:
            if classified_action != "start":
                return None
            if project_root is not None:
                try:
                    service = init_service(project_root, project_root=project_root)
                    target = project_root
                except Exception:
                    service = init_service(cwd, project_root=cwd)
                    target = cwd
            else:
                service = init_service(cwd, project_root=cwd)
                target = cwd
            requested_mode, creative_seed = _initialization_start_request(prompt)
            result = service.start(
                project_root=target,
                mode=requested_mode,
                seed=creative_seed,
                idempotency_key=key,
                host_session_id=host_session_id,
                host_turn_id=turn_identity,
            )
        else:
            service, session = active
            session_id = str(session["session_id"])
            revision = int(session["session_revision"])
            status = str(session.get("status") or "").upper()
            if classified_action == "wait":
                result = service.inspect(
                    session_id,
                    view="proposal" if status == "PROPOSAL_FROZEN" else "summary",
                )
                executed_action = "inspect"
            elif status in {
                "PROPOSAL_FROZEN",
                "STALE_SOURCE",
                "STALE_CANON",
            } and classified_action not in {
                "inspect",
                "cancel",
            }:
                result = service.inspect(
                    session_id,
                    view="proposal" if status == "PROPOSAL_FROZEN" else "summary",
                )
                executed_action = "inspect"
            elif classified_action == "advance":
                result = service.advance(
                    session_id,
                    expected_session_revision=revision,
                    idempotency_key=key,
                )
            elif classified_action == "answer":
                question_view = service.inspect(session_id, view="questions")
                questions = question_view.get("questions") or []
                if questions:
                    result = service.answer(
                        session_id,
                        {
                            str(questions[0]["question_id"]): prompt,
                        },
                        expected_session_revision=revision,
                        idempotency_key=key,
                    )
                else:
                    result = service.advance(
                        session_id,
                        expected_session_revision=revision,
                        idempotency_key=key,
                    )
                    executed_action = "advance"
            elif classified_action == "inspect":
                result = service.inspect(session_id, view="summary")
            elif classified_action == "propose":
                result = service.propose(
                    session_id,
                    expected_session_revision=revision,
                    idempotency_key=key,
                )
            elif classified_action == "cancel":
                result = service.cancel(
                    session_id,
                    expected_session_revision=revision,
                    idempotency_key=key,
                    reason=prompt,
                )
            else:
                return None
    except Exception as exc:
        executed_action = "error"
        result = {
            "status": "ERROR",
            "stage": active_session.get("stage") if active_session else "START",
            "session_id": (
                active_session.get("session_id") if active_session else None
            ),
            "reason": str(exc),
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _initialization_context(
                result,
                classified_action=classified_action,
                attempted_action=attempted_action,
                executed_action=executed_action,
            ),
        }
    }


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_seed_references(
    payload: Mapping[str, Any],
    grill_decision: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for source in (payload, grill_decision or {}):
        for key in (
            "event_seed_references",
            "eventSeedReferences",
            "event_seeds",
        ):
            value = source.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates:
            break
    references: list[dict[str, Any]] = []
    for value in candidates:
        if not isinstance(value, Mapping):
            continue
        seed_id = str(value.get("event_seed_id") or "").strip()
        revision = value.get("event_seed_revision")
        if (
            seed_id
            and type(revision) is int
            and revision >= 1
        ):
            references.append(
                {
                    "event_seed_id": seed_id,
                    "event_seed_revision": revision,
                }
            )
    return references


def _event_experience_context(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "blocked")
    lines = [
        "[PLOT_RAG_EVENT_EXPERIENCE]",
        f"status: {status}",
        f"reason: {result.get('reason') or ''}",
        "remote_called: false",
        f"receipt_created: {str(bool(result.get('receipt_created'))).lower()}",
    ]
    question = result.get("question")
    if isinstance(question, Mapping):
        options = [
            item
            for item in question.get("options") or []
            if isinstance(item, Mapping)
        ]
        lines.extend(
            [
                "phase: event_experience",
                "blocking_state: AWAITING_EVENT_EXPERIENCE",
                "本轮由事件体验单问阶段独占：不得创建剧情 receipt，不得执行远端检索，不得推演、规划或生成剧情。",
                f"Question: {question.get('question') or ''}",
            ]
        )
        for index, option in enumerate(options):
            label = chr(ord("A") + index)
            lines.append(
                f"{label}. {option.get('label') or option.get('option_id') or ''}"
            )
        lines.extend(
            [
                (
                    "Recommended answer: "
                    f"{question.get('recommended_option_id') or ''}"
                ),
                (
                    "Reason: "
                    f"{question.get('recommendation_rationale') or ''}"
                ),
                "只有明确选项、选项文字、“按推荐答案”或“你来定”才消费本问题；“继续 / 下一步”只重复。",
                "[/PLOT_RAG_EVENT_EXPERIENCE]",
            ]
        )
        return "\n".join(lines)
    manifest = result.get("manifest")
    if isinstance(manifest, Mapping):
        lines.extend(
            [
                "phase: event_experience",
                (
                    "event_seed_manifest_hash: "
                    f"{manifest.get('event_seed_manifest_hash') or ''}"
                ),
                (
                    "event_experience_control_revision: "
                    f"{manifest.get('control_revision')}"
                ),
                "locked_manifest:",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                "所有 EventSeed 的体验合同已锁定；剧情设计必须实现该体验轨迹，且不得命中 anti_experiences。",
            ]
        )
    else:
        lines.extend(
            [
                "blocking_state: AWAITING_EVENT_EXPERIENCE",
                "本轮停在事件体验阶段：不得创建剧情 receipt，不得执行远端检索，不得推演、规划或生成剧情。",
                "先由事件体验编排器为每个 EventSeed 生成并锁定 EventExperienceContract，再以同一 seed manifest 重试本轮。",
            ]
        )
    lines.append("[/PLOT_RAG_EVENT_EXPERIENCE]")
    return "\n".join(lines)


def _prepare_event_experience_gate(
    project_root: Path,
    *,
    config: Mapping[str, Any],
    payload: Mapping[str, Any],
    grill_decision: Mapping[str, Any] | None,
    execution_prompt: str,
    host_session_id: str,
    turn_id: str,
) -> dict[str, Any]:
    experience_config = dict(config.get("event_experience") or {})
    if not bool(experience_config.get("enabled", True)):
        return {"status": "disabled", "required": False}
    if not bool(
        experience_config.get("required_before_event_design", True)
    ):
        return {"status": "shadow", "required": False}

    try:
        control_runtime = _load_event_experience_runtime()
        orchestrator = _load_event_experience_orchestrator()
        service = control_runtime.EventExperienceService.for_project(
            project_root
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": f"event_experience_runtime_failed: {exc}",
            "required": True,
            "receipt_created": False,
        }

    decision = dict(grill_decision or {})
    intent_contract = dict(decision.get("contract") or {})
    expected_intent_id = str(decision.get("grill_session_id") or "")
    expected_intent_revision = decision.get("session_revision")
    expected_intent_hash = control_runtime.canonical_hash(
        intent_contract
    )
    references = _event_seed_references(payload, grill_decision)
    if not references:
        try:
            event_artifact_context = _infer_artifact_context(
                execution_prompt
            )
            event_artifact_context["artifact_revision"] = int(
                event_artifact_context.get("artifact_revision") or 1
            )
            prepared = orchestrator.ensure_locked_manifest(
                project_root,
                prompt=execution_prompt,
                artifact_context=event_artifact_context,
                intent_contract={
                    "contract": intent_contract,
                    "status": "EXECUTING",
                    "grill_session_id": expected_intent_id,
                    "session_revision": expected_intent_revision,
                    "intent_contract_hash": expected_intent_hash,
                },
                session_identity=host_session_id or "__anonymous__",
                turn_identity=(
                    turn_id
                    or _sha256_json(
                        {
                            "session_id": host_session_id,
                            "prompt": execution_prompt,
                        }
                    )[:24]
                ),
                question_ttl_seconds=int(
                    experience_config.get(
                        "session_ttl_seconds",
                        21_600,
                    )
                ),
            )
        except Exception as exc:
            return {
                "status": "blocked",
                "reason": f"event_experience_orchestration_failed: {exc}",
                "required": True,
                "receipt_created": False,
            }
        action = str(prepared.get("action") or "")
        references = list(prepared.get("seed_references") or [])
        if action != "locked":
            return {
                "status": "ask" if action == "ask" else "blocked",
                "reason": str(
                    prepared.get("reason")
                    or "event_experience_not_locked"
                ),
                "required": True,
                "receipt_created": False,
                "event_seed_references": references,
                "question": prepared.get("question"),
                "runtime_result": dict(prepared),
                "intent_contract_hash": expected_intent_hash,
                "intent_contract_id": expected_intent_id,
                "intent_contract_revision": expected_intent_revision,
            }
        manifest = dict(prepared.get("manifest") or {})
        return {
            "status": "locked",
            "reason": str(
                prepared.get("reason")
                or "locked_manifest_valid"
            ),
            "required": True,
            "receipt_created": False,
            "manifest": manifest,
            "binding": dict(prepared.get("binding") or {}),
            "event_seed_references": references,
            "intent_contract_hash": expected_intent_hash,
            "intent_contract_id": expected_intent_id,
            "intent_contract_revision": expected_intent_revision,
        }

    try:
        manifest = service.locked_manifest(references)
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": (
                str(getattr(exc, "code", "") or "")
                or str(exc)
            ),
            "details": dict(getattr(exc, "details", {}) or {}),
            "required": True,
            "receipt_created": False,
            "event_seed_references": references,
        }
    if (
        expected_intent_id
        and (
            str(manifest.get("source_intent_contract_id") or "")
            != expected_intent_id
            or type(expected_intent_revision) is not int
            or int(
                manifest.get("source_intent_contract_revision") or -1
            )
            != expected_intent_revision
            or str(
                manifest.get("source_intent_contract_hash") or ""
            )
            != expected_intent_hash
        )
    ):
        return {
            "status": "blocked",
            "reason": "EVENT_EXPERIENCE_MANIFEST_INTENT_MISMATCH",
            "required": True,
            "receipt_created": False,
            "event_seed_references": references,
        }
    return {
        "status": "locked",
        "reason": "locked_manifest_valid",
        "required": True,
        "receipt_created": False,
        "manifest": manifest,
        "event_seed_references": references,
        "intent_contract_hash": expected_intent_hash,
        "intent_contract_id": expected_intent_id,
        "intent_contract_revision": expected_intent_revision,
    }


def _event_experience_prompt(
    execution_prompt: str,
    gate_result: Mapping[str, Any],
) -> str:
    manifest = gate_result.get("manifest")
    if not isinstance(manifest, Mapping):
        return execution_prompt
    return (
        execution_prompt.rstrip()
        + "\n\n[LOCKED_EVENT_EXPERIENCE_MANIFEST]\n"
        + json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n[/LOCKED_EVENT_EXPERIENCE_MANIFEST]"
    )


def _pending_event_experience_handoff(
    project_root: Path,
    *,
    host_session_id: str,
) -> dict[str, Any] | None:
    database = project_root / ".plot-rag" / "grill.sqlite3"
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT state_json
            FROM grill_sessions
            WHERE status='EXECUTING'
            ORDER BY updated_at DESC, grill_session_id DESC
            """
        ).fetchall()
    for row in rows:
        try:
            state = json.loads(str(row["state_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        if (
            host_session_id
            and str(state.get("host_session_id") or "")
            != host_session_id
        ):
            continue
        prepare_result = state.get("prepare_result")
        if (
            str(state.get("prepare_status") or "")
            != "awaiting_event_experience"
            or not isinstance(prepare_result, Mapping)
        ):
            continue
        return {
            "state": state,
            "event_gate_result": dict(prepare_result),
        }
    return None


def _answer_pending_event_experience(
    project_root: Path,
    *,
    pending: Mapping[str, Any],
    answer: str,
    current_turn_id: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    state = dict(pending.get("state") or {})
    previous = dict(pending.get("event_gate_result") or {})
    runtime_result = dict(previous.get("runtime_result") or {})
    seed_manifest = dict(runtime_result.get("seed_manifest") or {})
    manifest_hash = str(
        seed_manifest.get("event_seed_manifest_hash") or ""
    )
    if not manifest_hash:
        return {
            "status": "blocked",
            "reason": "EVENT_EXPERIENCE_QUESTION_BINDING_MISSING",
            "required": True,
            "receipt_created": False,
        }
    try:
        control_runtime = _load_event_experience_runtime()
        service = control_runtime.EventExperienceService.for_project(
            project_root
        )
        answered = service.answer_question(
            manifest_hash,
            answer,
            expected_control_revision=service.get_control_revision(),
            idempotency_key=(
                "hook-event-answer:"
                + _sha256_json(
                    {
                        "grill_session_id": state.get(
                            "grill_session_id"
                        ),
                        "turn_id": current_turn_id,
                        "answer": answer,
                    }
                )
            ),
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": (
                str(getattr(exc, "code", "") or "")
                or f"event_experience_answer_failed: {exc}"
            ),
            "required": True,
            "receipt_created": False,
        }
    if str(answered.get("action") or "") != "selected":
        return {
            "status": (
                "ask"
                if answered.get("action")
                in {"repeat", "awaiting_explicit_choice"}
                else "blocked"
            ),
            "reason": str(
                answered.get("reason")
                or "event_experience_answer_not_selected"
            ),
            "required": True,
            "receipt_created": False,
            "question": answered.get("question"),
            "runtime_result": {
                **runtime_result,
                "question": answered.get("question"),
                "control_revision": answered.get("control_revision"),
            },
            "intent_contract_hash": previous.get(
                "intent_contract_hash"
            ),
            "intent_contract_id": previous.get("intent_contract_id"),
            "intent_contract_revision": previous.get(
                "intent_contract_revision"
            ),
        }

    original_turn_id = str(state.get("handoff_turn_id") or "")
    execution_prompt = str(state.get("execution_prompt") or "")
    decision = {
        "action": "proceed",
        "reason": "event_experience_answered",
        "task_family": "plot",
        "grill_session_id": str(state.get("grill_session_id") or ""),
        "session_revision": int(
            previous.get("intent_contract_revision")
            or runtime_result.get("intent_contract_revision")
            or max(1, int(state.get("revision") or 1) - 1)
        ),
        "grill_prepare_revision": int(state.get("revision") or 0),
        "contract": dict(state.get("contract") or {}),
        "execution_prompt": execution_prompt,
    }
    gate = _prepare_event_experience_gate(
        project_root,
        config=config,
        payload={},
        grill_decision=decision,
        execution_prompt=execution_prompt,
        host_session_id=str(state.get("host_session_id") or ""),
        turn_id=original_turn_id,
    )
    gate["resumed_grill_decision"] = decision
    gate["resumed_execution_prompt"] = execution_prompt
    return gate


def _extraction_queue(project_root: Path):
    runtime = _load_extraction_runtime()
    return runtime.ExtractionJobQueue(project_root)


def _latest_extraction_barrier(
    project_root: Path,
    *,
    config: Mapping[str, Any],
    branch_id: str,
) -> dict[str, Any]:
    extraction = dict(
        (config.get("performance") or {}).get("extraction") or {}
    )
    if (
        str(extraction.get("mode") or "sync") != "async"
        or not bool(extraction.get("next_plot_turn_barrier", True))
    ):
        return {"code": "disabled", "blocking": False}
    database = project_root / ".plot-rag" / "state.sqlite3"
    if not database.is_file():
        return {"code": "clear", "blocking": False, "job_count": 0}
    queue = _extraction_queue(project_root)
    jobs = queue.list_jobs(branch_id=branch_id, limit=1)
    if not jobs:
        return {"code": "clear", "blocking": False, "job_count": 0}
    sequence_no = jobs[0].get("sequence_no")
    normalized_sequence = (
        int(sequence_no) if type(sequence_no) is int else None
    )
    return queue.barrier_status(
        branch_id=branch_id,
        sequence_no=normalized_sequence,
        include_prior=normalized_sequence is not None,
    )


def _session_close_path(project_root: Path) -> Path:
    return project_root / ".plot-rag" / "session-close-pending.json"


def _session_close_failclosed_dir(project_root: Path) -> Path:
    return project_root / ".plot-rag" / "session-close-failclosed"


def _session_close_process_lock(project_root: Path) -> threading.RLock:
    key = os.path.normcase(str(project_root.resolve()))
    with _SESSION_CLOSE_LOCK_GUARD:
        return _SESSION_CLOSE_PROCESS_LOCKS.setdefault(
            key,
            threading.RLock(),
        )


@contextmanager
def _session_close_lock(project_root: Path):
    """Serialize session-close read/modify/write across threads and processes."""

    process_lock = _session_close_process_lock(project_root)
    with process_lock:
        state_dir = project_root / ".plot-rag"
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = state_dir / ".session-close.lock"
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = (
                    time.monotonic()
                    + _SESSION_CLOSE_LOCK_TIMEOUT_SECONDS
                )
                last_error: OSError | None = None
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(
                            handle.fileno(),
                            msvcrt.LK_NBLCK,
                            1,
                        )
                        break
                    except OSError as exc:
                        last_error = exc
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "session-close lock acquisition timed out"
                            ) from last_error
                        time.sleep(
                            min(
                                _SESSION_CLOSE_LOCK_POLL_SECONDS,
                                remaining,
                            )
                        )
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                deadline = (
                    time.monotonic()
                    + _SESSION_CLOSE_LOCK_TIMEOUT_SECONDS
                )
                last_error = None
                while True:
                    try:
                        fcntl.flock(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except OSError as exc:
                        last_error = exc
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "session-close lock acquisition timed out"
                            ) from last_error
                        time.sleep(
                            min(
                                _SESSION_CLOSE_LOCK_POLL_SECONDS,
                                remaining,
                            )
                        )
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_session_close_entries(
    project_root: Path,
) -> list[dict[str, Any]]:
    path = _session_close_path(project_root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "session-close pending state is unreadable"
        ) from exc
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        raise RuntimeError(
            "session-close pending state has an invalid shape"
        )
    return [
        dict(entry)
        for entry in entries
        if isinstance(entry, Mapping)
        and str(entry.get("branch_id") or "").strip()
    ]


def _read_session_close_failclosed_markers(
    project_root: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    marker_dir = _session_close_failclosed_dir(project_root)
    if not marker_dir.is_dir():
        return [], []
    entries: list[dict[str, Any]] = []
    paths: list[Path] = []
    for path in sorted(marker_dir.glob("*.json"), key=lambda item: item.name):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "session-close fail-closed marker is unreadable"
            ) from exc
        entry = payload.get("entry") if isinstance(payload, Mapping) else None
        if (
            not isinstance(entry, Mapping)
            or not str(entry.get("session_id") or "").strip()
            or not str(entry.get("branch_id") or "").strip()
            or entry.get("blocking") is not True
        ):
            raise RuntimeError(
                "session-close fail-closed marker has an invalid shape"
            )
        entries.append(dict(entry))
        paths.append(path)
    return entries, paths


def _read_session_close_state(
    project_root: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    by_key = {
        (
            str(entry.get("session_id") or ""),
            str(entry.get("branch_id") or ""),
        ): dict(entry)
        for entry in _read_session_close_entries(project_root)
    }
    marker_entries, marker_paths = (
        _read_session_close_failclosed_markers(project_root)
    )
    for entry in marker_entries:
        key = (
            str(entry.get("session_id") or ""),
            str(entry.get("branch_id") or ""),
        )
        by_key[key] = dict(entry)
    return list(by_key.values()), marker_paths


def _write_session_close_entries(
    project_root: Path,
    entries: Sequence[Mapping[str, Any]],
) -> None:
    path = _session_close_path(project_root)
    normalized = sorted(
        [dict(entry) for entry in entries],
        key=lambda entry: (
            str(entry.get("session_id") or ""),
            str(entry.get("branch_id") or ""),
        ),
    )
    if not normalized:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        path.name
        + ".tmp-"
        + hashlib.sha256(
            f"{os.getpid()}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:12]
    )
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "entries": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_session_close_failclosed_markers(
    project_root: Path,
    *,
    session_id: str,
    branches: Sequence[str],
    reason: str,
) -> list[Path]:
    marker_dir = _session_close_failclosed_dir(project_root)
    marker_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for branch in branches:
        branch_id = str(branch or "main").strip() or "main"
        now = datetime.now(timezone.utc).isoformat()
        token = hashlib.sha256(
            (
                f"{session_id}:{branch_id}:{os.getpid()}:"
                f"{threading.get_ident()}:{time.time_ns()}"
            ).encode("utf-8")
        ).hexdigest()
        final_path = marker_dir / f"pending-{token}.json"
        temporary = marker_dir / f".pending-{token}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entry": {
                        "session_id": session_id,
                        "branch_id": branch_id,
                        "code": "session_close_lock_failed",
                        "blocking": True,
                        "sequence_no": None,
                        "job_id": "",
                        "proposal_id": "",
                        "reason": reason,
                        "recorded_at": now,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, final_path)
        created.append(final_path)
    return created


def _consume_session_close_failclosed_markers(
    marker_paths: Sequence[Path],
) -> None:
    marker_dirs: set[Path] = set()
    for path in marker_paths:
        marker_dirs.add(path.parent)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for marker_dir in marker_dirs:
        try:
            marker_dir.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _commit_session_close_state(
    project_root: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    marker_paths: Sequence[Path],
) -> None:
    _write_session_close_entries(project_root, entries)
    _consume_session_close_failclosed_markers(marker_paths)


def _session_close_branches(
    project_root: Path,
    *,
    session_id: str,
    payload: Mapping[str, Any],
) -> list[str]:
    explicit = str(
        payload.get("branch_id") or payload.get("branchId") or ""
    ).strip()
    branches: list[str] = [explicit] if explicit else []
    database = project_root / ".plot-rag" / "state.sqlite3"
    if database.is_file() and session_id:
        try:
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    """
                    SELECT v1_context_json
                    FROM turns
                    WHERE session_id=?
                    ORDER BY rowid DESC
                    LIMIT 100
                    """,
                    (session_id,),
                ).fetchall()
            for row in rows:
                try:
                    artifact = json.loads(str(row[0] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(artifact, Mapping):
                    continue
                branch = str(artifact.get("branch_id") or "").strip()
                if branch and branch not in branches:
                    branches.append(branch)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).casefold():
                raise
    return branches or ["main"]


def _refresh_session_close_entries_unlocked(
    project_root: Path,
    *,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    entries, marker_paths = _read_session_close_state(project_root)
    if not entries:
        return []
    retained: list[dict[str, Any]] = []
    for entry in entries:
        branch = str(entry.get("branch_id") or "main")
        try:
            barrier = _latest_extraction_barrier(
                project_root,
                config=config,
                branch_id=branch,
            )
        except Exception as exc:
            retained.append(
                {
                    **entry,
                    "code": "failed",
                    "blocking": True,
                    "reason": _safe_worker_diagnostic(exc),
                    "checked_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            )
            continue
        if bool(barrier.get("blocking")):
            retained.append(
                {
                    **entry,
                    "code": str(barrier.get("code") or "pending"),
                    "blocking": True,
                    "sequence_no": barrier.get("sequence_no"),
                    "job_id": str(
                        ((barrier.get("job") or {}).get("job_id") or "")
                        if isinstance(barrier.get("job"), Mapping)
                        else ""
                    ),
                    "proposal_id": str(
                        (
                            (barrier.get("proposal") or {}).get(
                                "proposal_id"
                            )
                            or ""
                        )
                        if isinstance(
                            barrier.get("proposal"),
                            Mapping,
                        )
                        else ""
                    ),
                    "checked_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            )
    _commit_session_close_state(
        project_root,
        retained,
        marker_paths=marker_paths,
    )
    return retained


def _refresh_session_close_entries(
    project_root: Path,
    *,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    with _session_close_lock(project_root):
        return _refresh_session_close_entries_unlocked(
            project_root,
            config=config,
        )


def _session_end_output(message: str, *, emit: bool) -> None:
    if not emit:
        return
    print(
        json.dumps(
            {
                "continue": True,
                "suppressOutput": True,
                "systemMessage": message,
            },
            ensure_ascii=False,
        )
    )


def _run_session_end_unlocked(
    payload: dict[str, Any],
    *,
    emit: bool = True,
) -> int:
    cwd = _cwd(payload)
    root, config, config_error = _find_project(cwd)
    if root is None:
        return 0
    if config_error:
        _session_end_output(
            "plot-rag-gate SessionEnd: INDEX_UNAVAILABLE; "
            + config_error,
            emit=emit,
        )
        return 0
    session_id = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or "__anonymous__"
    )
    try:
        entries, marker_paths = _read_session_close_state(root)
        branches = _session_close_branches(
            root,
            session_id=session_id,
            payload=payload,
        )
    except Exception as exc:
        _session_end_output(
            "plot-rag-gate SessionEnd: "
            "close_pending=unknown; fail_closed=true; "
            "reason="
            + _safe_worker_diagnostic(exc),
            emit=emit,
        )
        return 0
    by_key = {
        (
            str(entry.get("session_id") or ""),
            str(entry.get("branch_id") or ""),
        ): dict(entry)
        for entry in entries
    }
    blocked: list[dict[str, Any]] = []
    for branch in branches:
        try:
            barrier = _latest_extraction_barrier(
                root,
                config=config or {},
                branch_id=branch,
            )
        except Exception as exc:
            barrier = {
                "code": "failed",
                "blocking": True,
                "branch_id": branch,
                "sequence_no": None,
                "reason": _safe_worker_diagnostic(exc),
            }
        key = (session_id, branch)
        if bool(barrier.get("blocking")):
            entry = {
                "session_id": session_id,
                "branch_id": branch,
                "code": str(barrier.get("code") or "pending"),
                "blocking": True,
                "sequence_no": barrier.get("sequence_no"),
                "job_id": str(
                    ((barrier.get("job") or {}).get("job_id") or "")
                    if isinstance(barrier.get("job"), Mapping)
                    else ""
                ),
                "proposal_id": str(
                    (
                        (barrier.get("proposal") or {}).get(
                            "proposal_id"
                        )
                        or ""
                    )
                    if isinstance(barrier.get("proposal"), Mapping)
                    else ""
                ),
                "reason": str(barrier.get("reason") or ""),
                "recorded_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
            by_key[key] = entry
            blocked.append(entry)
        else:
            by_key.pop(key, None)
    try:
        _commit_session_close_state(
            root,
            list(by_key.values()),
            marker_paths=marker_paths,
        )
    except Exception as exc:
        _session_end_output(
            "plot-rag-gate SessionEnd: "
            "close_pending=unknown; fail_closed=true; "
            "reason="
            + _safe_worker_diagnostic(exc),
            emit=emit,
        )
        return 0
    _session_end_output(
        "plot-rag-gate SessionEnd: "
        + (
            "close_pending="
            + str(len(blocked))
            + "; unresolved extraction barrier persisted"
            if blocked
            else "close_clear=true; accepted/no-delta barrier clear"
        ),
        emit=emit,
    )
    return 0


def _persist_session_close_lock_failure(
    project_root: Path,
    payload: Mapping[str, Any],
    exc: BaseException,
) -> tuple[int, str]:
    session_id = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or "__anonymous__"
    )
    try:
        branches = _session_close_branches(
            project_root,
            session_id=session_id,
            payload=payload,
        )
    except Exception:
        explicit = str(
            payload.get("branch_id") or payload.get("branchId") or ""
        ).strip()
        branches = [explicit or "main"]
    reason = _safe_worker_diagnostic(exc)
    try:
        created = _write_session_close_failclosed_markers(
            project_root,
            session_id=session_id,
            branches=branches,
            reason=reason,
        )
    except Exception as marker_exc:
        return 0, _safe_worker_diagnostic(marker_exc)
    return len(created), ""


def _run_session_end(
    payload: dict[str, Any],
    *,
    emit: bool = True,
) -> int:
    cwd = _cwd(payload)
    root, _, config_error = _find_project(cwd)
    if root is None or config_error:
        return _run_session_end_unlocked(payload, emit=emit)
    try:
        with _session_close_lock(root):
            return _run_session_end_unlocked(payload, emit=emit)
    except Exception as exc:
        marker_count, marker_error = _persist_session_close_lock_failure(
            root,
            payload,
            exc,
        )
        _session_end_output(
            "plot-rag-gate SessionEnd: "
            "close_pending=unknown; fail_closed=true; reason="
            + _safe_worker_diagnostic(exc)
            + f"; durable_markers={marker_count}"
            + (
                "; marker_error=" + marker_error
                if marker_error
                else ""
            ),
            emit=emit,
        )
        return 0


def _complete_resolved_extraction_grills(
    project_root: Path,
    *,
    grill_service: Any,
    branch_id: str,
) -> int:
    """Close exact Grill handoffs whose async continuity decision is durable."""

    if grill_service is None:
        return 0
    database = project_root / ".plot-rag" / "state.sqlite3"
    if not database.is_file():
        return 0
    try:
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT turns.session_id, turns.turn_id
                FROM extraction_jobs
                JOIN turns
                  ON turns.receipt_id=extraction_jobs.receipt_id
                LEFT JOIN proposals
                  ON proposals.proposal_id=extraction_jobs.result_proposal_id
                LEFT JOIN extraction_barrier_resolutions
                  ON extraction_barrier_resolutions.job_id=extraction_jobs.job_id
                WHERE extraction_jobs.branch_id=?
                  AND (
                        extraction_barrier_resolutions.job_id IS NOT NULL
                     OR (
                            extraction_jobs.job_status='succeeded'
                        AND (
                               extraction_jobs.result_kind='no_delta'
                            OR proposals.canon_status='accepted'
                        )
                     )
                  )
                ORDER BY turns.rowid
                """,
                (str(branch_id or "main"),),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return 0
        raise
    completed = 0
    for session_id, turn_id in rows:
        if not str(turn_id or ""):
            continue
        state = grill_service.complete_execution(
            project_root=project_root,
            host_session_id=str(session_id or ""),
            handoff_turn_id=str(turn_id),
        )
        if state is not None:
            completed += 1
    return completed


def _barrier_context(result: Mapping[str, Any]) -> str:
    code = str(result.get("code") or "failed")
    job = result.get("job")
    proposal = result.get("proposal")
    job_id = str(job.get("job_id") or "") if isinstance(job, Mapping) else ""
    proposal_id = (
        str(proposal.get("proposal_id") or "")
        if isinstance(proposal, Mapping)
        else ""
    )
    guidance = {
        "queued": "上轮抽取仍在队列中；等待 worker 完成后重试。",
        "running": "上轮抽取正在运行；等待 worker 完成后重试。",
        "failed": "上轮抽取失败；先检查 job error 并 retry 或明确取消。",
        "cancelled": "上轮抽取已取消；先明确重写、丢弃或切换 branch。",
        "pending_review": "上轮 proposal 尚待明确 accept / reject。",
        "rejected": "上轮 proposal 已拒绝；先明确重写、丢弃或切换 branch。",
        "retracted": "上轮 proposal 已撤回；先明确重写、丢弃或切换 branch。",
    }.get(code, "上轮连续性屏障未清除。")
    return "\n".join(
        (
            "[PLOT_RAG_EXTRACTION_BARRIER]",
            f"status: {code}",
            f"blocking: {str(bool(result.get('blocking'))).lower()}",
            f"branch_id: {result.get('branch_id') or ''}",
            f"sequence_no: {result.get('sequence_no')}",
            f"job_id: {job_id}",
            f"proposal_id: {proposal_id}",
            guidance,
            "本轮不得创建新的剧情 receipt，也不得执行远端检索或继续剧情 handoff。",
            "[/PLOT_RAG_EXTRACTION_BARRIER]",
        )
    )


def _prepared_turn_for_stop(
    project_root: Path,
    *,
    session_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    database = project_root / ".plot-rag" / "state.sqlite3"
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list[Any] = []
        if turn_id:
            clauses.append("turn_id=?")
            params.append(turn_id)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if not clauses:
            return None
        row = connection.execute(
            f"""
            SELECT rowid AS sequence_no, turns.*
            FROM turns
            WHERE {' AND '.join(clauses)}
            ORDER BY rowid DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        lifecycle_identity = json.loads(
            str(result.get("lifecycle_identity_json") or "{}")
        )
    except json.JSONDecodeError:
        lifecycle_identity = {}
    result["lifecycle_identity"] = (
        dict(lifecycle_identity)
        if isinstance(lifecycle_identity, Mapping)
        else {}
    )
    try:
        artifact_context = json.loads(
            str(result.get("v1_context_json") or "{}")
        )
    except json.JSONDecodeError:
        artifact_context = {}
    result["artifact_context"] = (
        artifact_context if isinstance(artifact_context, dict) else {}
    )
    return result


def _extraction_model_binding(project_root: Path) -> dict[str, Any]:
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import state_rag

    runtime = state_rag._load_runtime_config(project_root)
    extract = runtime.extract
    template_source = inspect.getsource(state_rag._chat_extract)
    schema_identity = {
        "legacy": str(getattr(state_rag, "DELTA_V3_SCHEMA", "")),
        "items": str(getattr(state_rag, "DELTA_V4_SCHEMA", "")),
    }
    return {
        "extract_provider": str(extract.name or "extract"),
        "extract_base_url": str(extract.base_url),
        "extract_model": str(extract.model),
        "extract_schema_hash": _sha256_json(schema_identity),
        "extract_prompt_template_hash": hashlib.sha256(
            template_source.encode("utf-8")
        ).hexdigest(),
        "min_confidence": float(runtime.min_confidence),
        "generation_params": state_rag._extraction_generation_params(runtime),
    }


def _enqueue_extraction_job(
    project_root: Path,
    *,
    assistant_text: str,
    session_id: str,
    turn_id: str,
    require_event_experience: bool,
    execution_mode: str = "async_strict",
    authoritative_proposal_id: str = "",
) -> dict[str, Any]:
    normalized_execution_mode = str(
        execution_mode or "async_strict"
    ).strip()
    if normalized_execution_mode not in {
        "async_strict",
        "async_shadow",
    }:
        raise ValueError(
            "execution_mode must be async_strict or async_shadow"
        )
    normalized_authoritative_proposal_id = str(
        authoritative_proposal_id or ""
    ).strip()
    if (
        normalized_execution_mode == "async_shadow"
        and not normalized_authoritative_proposal_id
    ):
        raise ValueError(
            "async_shadow requires authoritative_proposal_id"
        )
    turn = _prepared_turn_for_stop(
        project_root,
        session_id=session_id,
        turn_id=turn_id,
    )
    if turn is None:
        return {"status": "skipped", "reason": "no_prepared_turn"}
    binding = dict(turn.get("lifecycle_identity") or {})
    artifact_context = dict(turn.get("artifact_context") or {})
    try:
        scripts = _scripts_dir()
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from continuity import ContinuityService
        from v1_runtime import _active_continuity_identity

        continuity_service = ContinuityService(project_root)
        current_identity = _active_continuity_identity(
            continuity_service
        )
        if (
            int(current_identity["active_canon_revision"])
            != int(turn.get("prepared_canon_revision") or 0)
            or str(current_identity["active_projection_hash"])
            != str(turn.get("active_projection_hash") or "")
        ):
            return {
                "status": "failed",
                "reason": "PREPARED_IDENTITY_STALE",
                "receipt_id": str(turn.get("receipt_id") or ""),
            }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"prepared identity validation failed: {exc}",
            "receipt_id": str(turn.get("receipt_id") or ""),
        }

    references = list(binding.get("event_seed_references") or [])
    manifest_hash = str(
        binding.get("event_seed_manifest_hash") or ""
    )
    if require_event_experience and (not references or not manifest_hash):
        return {
            "status": "failed",
            "reason": "EVENT_EXPERIENCE_BINDING_REQUIRED",
            "receipt_id": str(turn.get("receipt_id") or ""),
        }
    if references and manifest_hash:
        try:
            runtime = _load_event_experience_runtime()
            service = runtime.EventExperienceService.for_project(project_root)
            service.validate_locked_manifest(
                references,
                expected_event_seed_manifest_hash=manifest_hash,
                expected_control_revision=int(
                    binding.get("event_experience_control_revision")
                ),
            )
        except Exception as exc:
            return {
                "status": "failed",
                "reason": (
                    str(getattr(exc, "code", "") or "")
                    or f"event experience binding validation failed: {exc}"
                ),
                "receipt_id": str(turn.get("receipt_id") or ""),
            }
    job_artifact_context = dict(artifact_context)
    authoritative_artifact_revision: int | None = None
    if normalized_execution_mode == "async_shadow":
        try:
            authoritative = continuity_service.inspect_proposal(
                normalized_authoritative_proposal_id
            )
            mismatches = {
                key: {
                    "expected": artifact_context.get(key),
                    "actual": authoritative.get(key),
                }
                for key in (
                    "artifact_id",
                    "artifact_stage",
                    "branch_id",
                    "chapter_no",
                    "scene_index",
                )
                if artifact_context.get(key) != authoritative.get(key)
            }
            if mismatches:
                raise ValueError(
                    "authoritative proposal differs from prepared artifact: "
                    + json.dumps(
                        mismatches,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            if str(authoritative.get("canon_status") or "") != "proposed":
                raise ValueError(
                    "authoritative proposal is not in proposed state"
                )
            authoritative_artifact_revision = int(
                authoritative.get("artifact_revision") or 0
            )
            if authoritative_artifact_revision < 1:
                raise ValueError(
                    "authoritative proposal has invalid artifact revision"
                )
            job_artifact_context["artifact_revision"] = (
                authoritative_artifact_revision + 1
            )
        except Exception as exc:
            return {
                "status": "failed",
                "reason": (
                    "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID: "
                    + _safe_worker_diagnostic(exc)
                ),
                "receipt_id": str(turn.get("receipt_id") or ""),
            }
    job_artifact_context["_plot_rag_v15"] = {
        "extraction_execution_mode": normalized_execution_mode,
        "authoritative_proposal_id": (
            normalized_authoritative_proposal_id
        ),
        "authoritative_artifact_revision": (
            authoritative_artifact_revision
        ),
        "event_seed_references": references,
        "event_seed_manifest_hash": manifest_hash,
        "event_experience_control_revision": binding.get(
            "event_experience_control_revision"
        ),
        "intent_contract_hash": binding.get("intent_contract_hash") or "",
        "grill_session_id": binding.get("grill_session_id") or "",
        "grill_session_revision": binding.get(
            "grill_session_revision"
        ),
    }
    model = _extraction_model_binding(project_root)
    queue = _extraction_queue(project_root)
    job = queue.enqueue(
        receipt_id=str(turn.get("receipt_id") or ""),
        request_id=str(turn.get("request_id") or ""),
        prompt_hash=str(turn.get("prompt_hash") or ""),
        retrieved_context_digest=str(
            turn.get("retrieved_context_digest") or ""
        ),
        prepared_canon_revision=int(
            turn.get("prepared_canon_revision") or 0
        ),
        active_projection_hash=str(
            turn.get("active_projection_hash") or ""
        ),
        assistant_text=assistant_text,
        intent_contract_hash=str(
            binding.get("intent_contract_hash") or ""
        ),
        event_seed_manifest_hash=str(
            binding.get("event_seed_manifest_hash") or ""
        ),
        event_experience_control_revision=int(
            binding.get("event_experience_control_revision") or 0
        ),
        event_seed_references=references,
        experience_contract_hashes=list(
            binding.get("experience_contract_hashes") or []
        ),
        artifact_context=job_artifact_context,
        branch_id=str(artifact_context.get("branch_id") or "main"),
        sequence_no=int(turn["sequence_no"]),
        **model,
    )
    return {
        "status": "queued",
        "reason": "durable_extraction_job_enqueued",
        "job": job,
        "receipt_id": str(turn.get("receipt_id") or ""),
        "recorded_events": [],
        "proposal_events": [],
        "lifecycle_mode": (
            "strict_proposal_async_shadow"
            if normalized_execution_mode == "async_shadow"
            else "strict_proposal_async"
        ),
    }


def _validate_worker_experience_binding(
    project_root: Path,
    job: Mapping[str, Any],
) -> None:
    artifact_context = dict(job.get("artifact_context") or {})
    binding = dict(artifact_context.get("_plot_rag_v15") or {})
    references = list(
        job.get("event_seed_references")
        or binding.get("event_seed_references")
        or []
    )
    manifest_hash = str(
        job.get("event_seed_manifest_hash")
        or binding.get("event_seed_manifest_hash")
        or ""
    )
    if not manifest_hash:
        return
    runtime = _load_event_experience_runtime()
    service = runtime.EventExperienceService.for_project(project_root)
    service.validate_locked_manifest(
        references,
        expected_event_seed_manifest_hash=manifest_hash,
        expected_control_revision=int(
            job.get("event_experience_control_revision")
            or binding.get("event_experience_control_revision")
        ),
    )


def _experience_review_quotes(
    assistant_text: str,
) -> tuple[list[str], list[list[int]]]:
    text = str(assistant_text)
    non_space = [match.span() for match in re.finditer(r"\S+", text)]
    if not non_space:
        return [], []
    candidate_starts = [
        non_space[0][0],
        non_space[len(non_space) // 2][0],
        non_space[-1][0],
    ]
    quotes: list[str] = []
    offsets: list[list[int]] = []
    for start in candidate_starts:
        window_start = max(0, start - 40)
        while window_start < len(text) and text[window_start].isspace():
            window_start += 1
        window_end = min(len(text), window_start + 160)
        for punctuation in ("。", "！", "？", "\n"):
            boundary = text.find(punctuation, start, window_end)
            if boundary >= 0:
                window_end = boundary + 1
                break
        while window_end > window_start and text[window_end - 1].isspace():
            window_end -= 1
        quote = text[window_start:window_end]
        if quote and quote not in quotes:
            quotes.append(quote)
            offsets.append([window_start, window_end])
    return quotes, offsets


def _record_automatic_experience_reviews_unchecked(
    project_root: Path,
    *,
    assistant_text: str,
    result: Mapping[str, Any],
    turn: Mapping[str, Any] | None = None,
    job: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = str(assistant_text)
    quotes, offsets = _experience_review_quotes(source)
    if not quotes:
        return {
            "status": "skipped",
            "reason": "assistant_text_has_no_verbatim_evidence",
            "reviews": [],
            "diagnostics": [],
        }
    turn_payload = dict(turn or {})
    job_payload = dict(job or {})
    lifecycle = dict(turn_payload.get("lifecycle_identity") or {})
    artifact_context = dict(job_payload.get("artifact_context") or {})
    worker_binding = dict(artifact_context.get("_plot_rag_v15") or {})
    references = list(
        lifecycle.get("event_seed_references")
        or job_payload.get("event_seed_references")
        or worker_binding.get("event_seed_references")
        or []
    )
    manifest_hash = str(
        lifecycle.get("event_seed_manifest_hash")
        or job_payload.get("event_seed_manifest_hash")
        or worker_binding.get("event_seed_manifest_hash")
        or ""
    )
    binding_revision = (
        lifecycle.get("event_experience_control_revision")
        if lifecycle.get("event_experience_control_revision") is not None
        else job_payload.get("event_experience_control_revision")
    )
    if binding_revision is None:
        binding_revision = worker_binding.get(
            "event_experience_control_revision"
        )
    if not references or not manifest_hash or binding_revision is None:
        return {
            "status": "skipped",
            "reason": "event_experience_binding_missing",
            "reviews": [],
            "diagnostics": [],
        }
    runtime = _load_event_experience_runtime()
    service = runtime.EventExperienceService.for_project(project_root)
    try:
        manifest = service.validate_locked_manifest(
            references,
            expected_event_seed_manifest_hash=manifest_hash,
            expected_control_revision=int(binding_revision),
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "event_experience_manifest_validation_failed",
            "reviews": [],
            "diagnostics": [_safe_worker_diagnostic(exc)],
        }
    receipt_id = str(
        result.get("receipt_id")
        or turn_payload.get("receipt_id")
        or job_payload.get("receipt_id")
        or ""
    )
    proposal_id = str(
        result.get("proposal_id")
        or result.get("result_proposal_id")
        or ""
    )
    if not proposal_id:
        result_kind = str(
            result.get("result_kind")
            or (result.get("job") or {}).get("result_kind")
            or job_payload.get("result_kind")
            or ""
        )
        if result_kind == "no_delta" or str(result.get("status") or "") == "no_delta":
            proposal_id = "no-delta:" + (
                receipt_id
                or hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
            )
    if not receipt_id or not proposal_id:
        return {
            "status": "skipped",
            "reason": "proposal_or_receipt_identity_missing",
            "reviews": [],
            "diagnostics": [],
        }
    assistant_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    recorded: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for entry in manifest.get("contracts") or []:
        if not isinstance(entry, Mapping):
            continue
        contract_id = str(entry.get("contract_id") or "")
        try:
            contract = service.get_contract(contract_id)
            primary = str(contract.get("primary_emotion") or "")
            secondary = [
                str(value)
                for value in contract.get("ordered_secondary_emotions") or []
                if str(value)
            ]
            emotion_hits = [
                value
                for value in [primary, *secondary]
                if value and value in source
            ]
            anti_hits = [
                str(value)
                for value in contract.get("anti_experiences") or []
                if 1 < len(str(value)) <= 80 and str(value) in source
            ]
            if anti_hits:
                severity = "critical"
                drift = "命中 anti_experiences：" + "；".join(anti_hits)
                recommendation = "重新生成当前故事产物，移除明确违约体验。"
            elif emotion_hits:
                severity = "none"
                drift = "自动逐字审查观察到目标体验信号：" + "、".join(
                    emotion_hits
                )
                recommendation = "保留现有实现，并在人工审查时复核峰值与余味。"
            else:
                severity = "warning"
                drift = "逐字证据中未直接观察到合同的主要情绪词。"
                recommendation = "增强人物行动、冲突结果或信息转折对目标体验的承载。"
            review = service.record_review(
                {
                    "proposal_id": proposal_id,
                    "receipt_id": receipt_id,
                    "assistant_sha256": assistant_hash,
                    "contract_id": contract_id,
                    "contract_hash": str(entry.get("contract_hash") or ""),
                    "artifact_revision": int(
                        contract.get("artifact_revision")
                        or manifest.get("artifact_revision")
                        or 0
                    ),
                    "observed_entry": quotes[0],
                    "observed_peak": quotes[len(quotes) // 2],
                    "observed_exit": quotes[-1],
                    "supporting_quotes": quotes,
                    "supporting_quote_offsets": offsets,
                    "drift": drift,
                    "severity": severity,
                    "recommendation": recommendation,
                },
                expected_control_revision=service.get_control_revision(),
                idempotency_key=(
                    "automatic-experience-review:"
                    + hashlib.sha256(
                        (
                            receipt_id
                            + "\0"
                            + proposal_id
                            + "\0"
                            + contract_id
                            + "\0"
                            + assistant_hash
                        ).encode("utf-8")
                    ).hexdigest()
                ),
                assistant_text=source,
            )
            recorded.append(dict(review["review"]))
        except Exception as exc:
            diagnostics.append(
                f"{contract_id or '<missing-contract>'}: "
                + _safe_worker_diagnostic(exc)
            )
    return {
        "status": (
            "recorded"
            if recorded and not diagnostics
            else "partial"
            if recorded
            else "failed"
        ),
        "reason": (
            "automatic_experience_reviews_recorded"
            if recorded and not diagnostics
            else "automatic_experience_review_diagnostics"
        ),
        "reviews": recorded,
        "diagnostics": diagnostics,
    }


def _record_automatic_experience_reviews(
    project_root: Path,
    *,
    assistant_text: str,
    result: Mapping[str, Any],
    turn: Mapping[str, Any] | None = None,
    job: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep optional experience-review recording fail-soft end to end."""

    try:
        return _record_automatic_experience_reviews_unchecked(
            project_root,
            assistant_text=assistant_text,
            result=result,
            turn=turn,
            job=job,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "automatic_experience_review_runtime_failed",
            "reviews": [],
            "diagnostics": [_safe_worker_diagnostic(exc)],
        }


def _persist_experience_review_diagnostics(
    project_root: Path,
    *,
    receipt_id: str,
    proposal_id: str,
    diagnostics: Sequence[Any],
    source: str,
) -> None:
    sanitized = [
        _safe_worker_diagnostic(value)
        for value in diagnostics
        if str(value or "").strip()
    ]
    if not sanitized:
        return
    path = (
        project_root
        / ".plot-rag"
        / "experience-review-diagnostics.jsonl"
    )
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "receipt_id": str(receipt_id or ""),
        "proposal_id": str(proposal_id or ""),
        "source": str(source or ""),
        "diagnostics": sanitized,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        pass


def _extraction_proposal_factory(
    project_root: Path,
    job: Mapping[str, Any],
    assistant_text: str,
) -> dict[str, Any]:
    try:
        _validate_worker_experience_binding(project_root, job)
        queue = _extraction_queue(project_root)
        proposal_binding = queue.proposal_binding(job)
        artifact_context = dict(job.get("artifact_context") or {})
        worker_binding = dict(
            artifact_context.get("_plot_rag_v15") or {}
        )
        execution_mode = str(
            worker_binding.get("extraction_execution_mode")
            or "async_strict"
        )
        if execution_mode not in {"async_strict", "async_shadow"}:
            raise ValueError(
                "unknown extraction execution mode: "
                + execution_mode
            )
        shadow_only = execution_mode == "async_shadow"
        authoritative_proposal_id = str(
            worker_binding.get("authoritative_proposal_id") or ""
        )
        _, propose_turn, _ = _load_state_runtime()
        result = propose_turn(
            project_root,
            assistant_text,
            request_id=str(job.get("request_id") or ""),
            proposal_binding=proposal_binding,
            no_delta_without_proposal=True,
            shadow_only=shadow_only,
            authoritative_proposal_id=authoritative_proposal_id,
        )
    except Exception as exc:
        return {
            "validator_passed": False,
            "remote_status": "failed",
            "error": str(exc),
        }
    status = str(result.get("status") or "")
    proposal_id = str(result.get("proposal_id") or "")
    if (
        status == "no_delta"
        and str(result.get("result_kind") or "") == "no_delta"
        and not proposal_id
    ):
        return {
            "validator_passed": True,
            "result_kind": "no_delta",
            "remote_status": (
                "shadow_no_delta" if shadow_only else "no_delta"
            ),
        }
    if status != "proposed" or not proposal_id:
        return {
            "validator_passed": False,
            "remote_status": status or "failed",
            "error": str(result.get("reason") or status or "proposal failed"),
        }
    return {
        "validator_passed": True,
        "result_kind": "proposal",
        "result_proposal_id": proposal_id,
        "remote_status": (
            "shadow_"
            + str(
                (result.get("comparison") or {}).get("status")
                or "validated"
            )
            if shadow_only
            else str(
                ((result.get("remote") or {}).get("status"))
                or "validated"
            )
        ),
    }


def _run_extraction_worker(
    project_root: Path,
    *,
    worker_id: str,
    max_jobs: int = 100,
    startup_status_path: Path | None = None,
) -> int:
    try:
        queue = _extraction_queue(project_root)
        _write_worker_startup_status(
            startup_status_path,
            status="ready",
            worker_id=worker_id,
            code="WORKER_READY",
        )
        for _ in range(max(1, min(int(max_jobs), 1000))):
            captured: dict[str, Any] = {}

            def proposal_factory(
                job: Mapping[str, Any],
                text: str,
            ) -> dict[str, Any]:
                captured["job"] = dict(job)
                captured["assistant_text"] = text
                value = _extraction_proposal_factory(
                    project_root,
                    job,
                    text,
                )
                captured["work_result"] = dict(value)
                return value

            result = queue.run_once(
                worker_id=worker_id,
                proposal_factory=proposal_factory,
                lease_seconds=300,
                heartbeat_interval_seconds=60,
                recover_stale=True,
                raise_on_error=False,
            )
            if (
                str(result.get("status") or "") == "succeeded"
                and captured.get("assistant_text") is not None
                and isinstance(captured.get("job"), Mapping)
            ):
                review_result = _record_automatic_experience_reviews(
                    project_root,
                    assistant_text=str(captured["assistant_text"]),
                    result={
                        **dict(captured.get("work_result") or {}),
                        **dict(result),
                        "receipt_id": str(
                            captured["job"].get("receipt_id") or ""
                        ),
                        "result_kind": str(
                            (result.get("job") or {}).get(
                                "result_kind"
                            )
                            or (
                                captured.get("work_result") or {}
                            ).get("result_kind")
                            or ""
                        ),
                    },
                    job=dict(captured["job"]),
                )
                _persist_experience_review_diagnostics(
                    project_root,
                    receipt_id=str(
                        captured["job"].get("receipt_id") or ""
                    ),
                    proposal_id=str(result.get("proposal_id") or ""),
                    diagnostics=review_result.get("diagnostics") or [],
                    source="async_worker",
                )
            if str(result.get("status") or "") == "idle":
                break
    except Exception as exc:
        _write_worker_startup_status(
            startup_status_path,
            status="failed",
            worker_id=worker_id,
            code="WORKER_RUNTIME_FAILED",
            message=_safe_worker_diagnostic(exc),
        )
        return 1
    return 0


_WORKER_SECRET_RE = re.compile(
    r"""(?ix)
    (?:
        \bbearer\s+["']?[^\s"',;|]{8,}["']?
        |
        \b(?:api[_-]?key|authorization|password|passwd|secret|
             access[_-]?token|refresh[_-]?token|credential|
             client[_-]?secret|token|cookie|set[_-]?cookie)\b
        \s*["']?\s*[:=]\s*["']?
        (?:bearer\s+)?[^\s"',;|]{8,}["']?
        |
        \b(?:sk|sf|ak)-[A-Za-z0-9._~+/=-]{8,}
    )
    """
)
_WORKER_SECRET_ENV_NAME_RE = re.compile(
    r"""(?ix)
    (?:^|_)
    (?:
        api[_-]?key|key|token|secret|password|passwd|credential|
        authorization|cookie
    )
    (?:$|_)
    """
)
_WORKER_STARTUP_TIMEOUT_SECONDS = 0.5


def _safe_worker_diagnostic(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    environment_secrets = sorted(
        {
            str(environment_value)
            for environment_name, environment_value in os.environ.items()
            if _WORKER_SECRET_ENV_NAME_RE.search(str(environment_name))
            and len(str(environment_value)) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in environment_secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _WORKER_SECRET_RE.sub("[REDACTED]", text)
    text = re.sub(
        r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@",
        r"\1[REDACTED]@",
        text,
    )
    return text[:1000] or "worker failed without a diagnostic"


def _write_worker_startup_status(
    path: Path | None,
    *,
    status: str,
    worker_id: str,
    code: str,
    message: str = "",
) -> None:
    if path is None:
        return
    payload = {
        "schema_version": 1,
        "status": str(status),
        "worker_id": str(worker_id),
        "pid": os.getpid(),
        "code": str(code),
        "message": _safe_worker_diagnostic(message) if message else "",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            path.name
            + ".tmp-"
            + hashlib.sha256(
                f"{os.getpid()}:{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:12]
        )
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def _read_worker_startup_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _monitor_extraction_worker(
    process: subprocess.Popen[bytes],
    *,
    status_path: Path,
    worker_id: str,
) -> None:
    try:
        _, stderr = process.communicate()
    except Exception as exc:
        stderr = str(exc).encode("utf-8", errors="replace")
    if int(process.returncode or 0) == 0:
        return
    existing = _read_worker_startup_status(status_path)
    if str(existing.get("status") or "") == "failed":
        return
    diagnostic = bytes(stderr or b"").decode(
        "utf-8",
        errors="replace",
    )
    _write_worker_startup_status(
        status_path,
        status="failed",
        worker_id=worker_id,
        code="WORKER_PROCESS_EXITED",
        message=_safe_worker_diagnostic(diagnostic),
    )


def _validated_worker_startup_path(
    project_root: Path,
    value: str,
) -> Path:
    allowed = (
        project_root / ".plot-rag" / "worker-startup"
    ).resolve(strict=False)
    candidate = Path(value).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(
            "worker startup status path escapes the project runtime"
        ) from exc
    return candidate


def _spawn_extraction_worker(project_root: Path) -> bool:
    if _truthy(os.environ.get("PLOT_RAG_GATE_WORKER_DISABLED")):
        return False
    worker_id = (
        "hook-worker-"
        + hashlib.sha256(
            f"{project_root}\n{os.getpid()}".encode("utf-8")
        ).hexdigest()[:16]
    )
    status_root = project_root / ".plot-rag" / "worker-startup"
    try:
        status_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    status_path = status_root / (
        worker_id
        + "-"
        + hashlib.sha256(
            f"{time.time_ns()}:{os.getpid()}".encode("utf-8")
        ).hexdigest()[:16]
        + ".json"
    )
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--extraction-worker",
        "--project-root",
        str(project_root),
        "--worker-id",
        worker_id,
        "--startup-status",
        str(status_path),
    ]
    creationflags = (
        (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        if os.name == "nt"
        else 0
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except OSError:
        return False
    threading.Thread(
        target=_monitor_extraction_worker,
        kwargs={
            "process": process,
            "status_path": status_path,
            "worker_id": worker_id,
        },
        name=f"plot-rag-extraction-monitor-{process.pid}",
        daemon=True,
    ).start()
    deadline = time.perf_counter() + _WORKER_STARTUP_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        startup = _read_worker_startup_status(status_path)
        status = str(startup.get("status") or "")
        if status == "ready":
            try:
                status_path.unlink()
            except OSError:
                pass
            return True
        if status == "failed":
            return False
        if process.poll() is not None:
            return False
        time.sleep(0.01)
    _write_worker_startup_status(
        status_path,
        status="failed",
        worker_id=worker_id,
        code="WORKER_STARTUP_TIMEOUT",
        message="worker did not report ready before the startup deadline",
    )
    try:
        process.terminate()
    except OSError:
        pass
    return False


def _status_failed(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").lower() in {
        "failed",
        "error",
        "unavailable",
        "index_unavailable",
        "stable_id_missing",
    }:
        return True
    # A normal longform degradation should only hard-block when the required
    # Advantage generation context itself is not ready.  This keeps legacy
    # retrieval degradation observable without allowing a mandatory golden
    # finger context to be silently skipped.
    candidates: list[Any] = [
        result.get("advantage_context"),
        result.get("longform"),
        result.get("prepared_turn"),
        result.get("prepare_result"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            value = (
                candidate.get("advantage_context")
                if "advantage_context" in candidate
                else candidate
            )
            if not isinstance(value, Mapping):
                continue
            if not bool(value.get("required")):
                continue
            if str(value.get("status") or "").lower() != "ready":
                return True
    return False


def _stop_output(result: dict[str, Any], *, stop_hook_active: bool, fail_closed: bool) -> None:
    status = str(result.get("status") or "failed")
    recorded = result.get("recorded_events") or []
    if _status_failed(result) and fail_closed and not stop_hook_active:
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": (
                        "剧情状态提交失败，尚不能把本轮推演视为已记录。"
                        f" receipt={result.get('receipt_id', '')}; reason={result.get('reason', 'unknown')}"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return
    shadow_job = result.get("shadow_job")
    shadow_job_id = (
        str(shadow_job.get("job_id") or "")
        if isinstance(shadow_job, Mapping)
        else ""
    )
    shadow_extraction = result.get("shadow_extraction")
    shadow_status = (
        str(shadow_extraction.get("status") or "")
        if isinstance(shadow_extraction, Mapping)
        else ""
    )
    hook_stop = dict(
        (result.get("telemetry") or {}).get("hook_stop") or {}
    )
    message = (
        f"plot-rag-gate Stop: status={status}; "
        f"recorded_events={len(recorded)}; "
        f"proposal_events={len(result.get('proposal_events') or [])}; "
        f"proposal={result.get('proposal_id', '')}; "
        f"job={((result.get('job') or {}).get('job_id', '') if isinstance(result.get('job'), dict) else '')}; "
        f"shadow_job={shadow_job_id}; "
        f"shadow_status={shadow_status}; "
        f"execution_mode={result.get('extraction_execution_mode', '')}; "
        f"receipt={result.get('receipt_id', '')}"
    )
    if hook_stop:
        message += (
            f"; sync_ms={float(hook_stop.get('sync_ms') or 0.0):.3f}"
            f"; enqueue_ms={float(hook_stop.get('enqueue_ms') or 0.0):.3f}"
            f"; total_ms={float(hook_stop.get('total_ms') or 0.0):.3f}"
        )
    if _status_failed(result):
        message += f"; reason={result.get('reason', 'unknown')}"
    if (
        isinstance(shadow_extraction, Mapping)
        and _status_failed(dict(shadow_extraction))
    ):
        message += (
            "; shadow_reason="
            + _safe_worker_diagnostic(
                shadow_extraction.get("reason") or "unknown"
            )
        )
    print(
        json.dumps(
            {"continue": True, "suppressOutput": True, "systemMessage": message},
            ensure_ascii=False,
        )
    )


_STORY_CONTROL_TERM_PATTERNS: tuple[
    tuple[str, re.Pattern[str]],
    ...,
] = (
    (
        "internal_sentinel",
        re.compile(
            r"\[/?(?:LOCKED_EVENT_EXPERIENCE_MANIFEST|"
            r"PLOT_RAG_EVENT_EXPERIENCE|PLOT_RAG_GRILL|"
            r"EVENT_EXPERIENCE_CONTRACT)\]",
            re.IGNORECASE,
        ),
    ),
    (
        "internal_type",
        re.compile(
            r"\b(?:EventSeed|EventExperienceContract|"
            r"EventExperienceArc|ExperienceReview)\b"
        ),
    ),
    (
        "internal_identity_field",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:event_seed_id|event_seed_revision|"
            r"event_seed_manifest_hash|contract_id|contract_revision|"
            r"contract_hash|arc_id|arc_revision|arc_hash|"
            r"event_experience_control_revision|"
            r"source_intent_contract_hash|source_intent_contract_id|"
            r"supporting_quote_offsets|binding_hash)"
            r"(?![A-Za-z0-9_])\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    (
        "internal_schema",
        re.compile(
            r"plot-rag-event-experience/v\d+|"
            r"locked_event_experience|event_seed_candidates",
            re.IGNORECASE,
        ),
    ),
    (
        "internal_manifest_json",
        re.compile(
            r"[\"'](?:manifest_kind|control_revision|"
            r"source_intent_contract_revision)[\"']\s*:",
            re.IGNORECASE,
        ),
    ),
)


def _story_artifact_control_leaks(
    *,
    config: Mapping[str, Any],
    turn: Mapping[str, Any] | None,
    assistant_text: str,
) -> list[dict[str, Any]]:
    experience = dict(config.get("event_experience") or {})
    if (
        int(config.get("config_version") or config.get("version") or 1) < 3
        or experience.get("enabled") is not True
        or bool(experience.get("visible_in_story_artifacts", False))
    ):
        return []
    if not isinstance(turn, Mapping):
        return []
    prompt = str(turn.get("prompt") or "")
    original_prompt = prompt
    internal_markers = (
        "\n\n[LOCKED_INTENT_CONTRACT]",
        "\n\n[LOCKED_EVENT_EXPERIENCE_MANIFEST]",
        "\n\n[PLOT_RAG_EVENT_EXPERIENCE]",
    )
    marker_offsets = [
        original_prompt.find(marker)
        for marker in internal_markers
        if marker in original_prompt
    ]
    if marker_offsets:
        original_prompt = original_prompt[: min(marker_offsets)]
    if is_meta_plot_discussion(original_prompt):
        return []
    artifact = dict(turn.get("artifact_context") or {})
    stage = str(
        artifact.get("artifact_stage")
        or artifact.get("stage")
        or ""
    ).casefold()
    task = str(artifact.get("task") or "").casefold()
    if stage and stage not in {
        "brainstorm",
        "outline",
        "draft",
        "final",
        "published",
    }:
        return []
    if task and task not in {
        "outline",
        "scene",
        "prose",
        "revision",
    }:
        return []
    text = str(assistant_text)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for code, pattern in _STORY_CONTROL_TERM_PATTERNS:
        for match in pattern.finditer(text):
            identity = (code, match.start(), match.end())
            if identity in seen:
                continue
            seen.add(identity)
            matches.append(
                {
                    "code": code,
                    "start": match.start(),
                    "end": match.end(),
                    "text_sha256": hashlib.sha256(
                        match.group(0).encode("utf-8")
                    ).hexdigest(),
                }
            )
    return matches


def _run_stop(payload: dict[str, Any]) -> int:
    stop_started = time.perf_counter()
    cwd = _cwd(payload)
    root, config, config_error = _find_project(cwd)
    host_session_id = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    )
    turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
    try:
        active_initialization = _active_initialization(
            cwd,
            root,
            host_session_id=host_session_id,
        )
    except InitializationLookupError as exc:
        print(
            json.dumps(
                {
                    "continue": True,
                    "suppressOutput": True,
                    "systemMessage": (
                        "plot-rag-gate Stop: initialization state lookup "
                        "failed; plot extraction suppressed; "
                        f"reason={exc}"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if active_initialization is not None:
        _, session = active_initialization
        print(
            json.dumps(
                {
                    "continue": True,
                    "suppressOutput": True,
                    "systemMessage": (
                        "plot-rag-gate Stop: initialization owns this turn; "
                        "plot extraction suppressed; "
                        f"session={session.get('session_id', '')}; "
                        f"status={session.get('status', '')}"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    grill_root = root or cwd
    grill_service = None
    grill_lookup_error: str | None = None
    grill_store_exists = (
        grill_root / ".plot-rag" / "grill.sqlite3"
    ).is_file()
    try:
        _, grill_service, grill_config = _grill_service(
            grill_root,
            config,
        )
        grill_stop = (
            grill_service.should_suppress_stop(
                project_root=grill_root,
                host_session_id=host_session_id,
                turn_id=turn_id,
            )
            if bool(grill_config.get("enabled", True))
            else None
        )
    except Exception as exc:
        grill_stop = None
        grill_lookup_error = str(exc)
    if grill_lookup_error and (
        grill_store_exists
        or bool(
            config is not None
            and (config.get("grill") or {}).get("enabled", True)
        )
    ):
        print(
            json.dumps(
                {
                    "continue": True,
                    "suppressOutput": True,
                    "systemMessage": (
                        "plot-rag-gate Stop: Grill state lookup failed; "
                        "plot extraction suppressed; "
                        f"reason={grill_lookup_error}"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if grill_stop is not None:
        print(
            json.dumps(
                {
                    "continue": True,
                    "suppressOutput": True,
                    "systemMessage": (
                        "plot-rag-gate Stop: Grill owns this turn; "
                        "plot extraction suppressed; "
                        f"session={grill_stop.get('grill_session_id', '')}; "
                        f"action={grill_stop.get('action', '')}"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if root is None or (config is not None and not bool(config.get("enabled", True))):
        return 0
    if config_error:
        print(
            json.dumps(
                {
                    "continue": True,
                    "suppressOutput": True,
                    "systemMessage": f"plot-rag-gate Stop: INDEX_UNAVAILABLE; {config_error}",
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not bool((config or {}).get("state", {}).get("auto_record", True)):
        return 0
    assistant_text = _assistant_text(payload)
    if not assistant_text:
        return 0
    prepared_turn = _prepared_turn_for_stop(
        root,
        session_id=host_session_id,
        turn_id=turn_id,
    )
    control_leaks = _story_artifact_control_leaks(
        config=config or {},
        turn=prepared_turn,
        assistant_text=assistant_text,
    )
    if control_leaks:
        result = {
            "status": "failed",
            "reason": "STORY_ARTIFACT_CONTROL_TERM_LEAKAGE",
            "receipt_id": str(
                (prepared_turn or {}).get("receipt_id") or ""
            ),
            "recorded_events": [],
            "proposal_events": [],
            "control_term_violations": control_leaks,
            "telemetry": {
                "hook_stop": {
                    "sync_ms": 0.0,
                    "enqueue_ms": 0.0,
                    "total_ms": round(
                        (time.perf_counter() - stop_started) * 1000.0,
                        3,
                    ),
                }
            },
        }
        _stop_output(
            result,
            stop_hook_active=bool(
                payload.get("stop_hook_active", False)
            ),
            fail_closed=True,
        )
        return 0
    extraction_config = dict(
        ((config or {}).get("performance") or {}).get("extraction") or {}
    )
    config_version = int(
        (config or {}).get("config_version")
        or (config or {}).get("version")
        or 1
    )
    lifecycle_config = dict((config or {}).get("lifecycle") or {})
    stop_sync_ms = 0.0
    stop_enqueue_ms = 0.0
    strict_lifecycle = bool(
        config_version >= 3
        and bool(lifecycle_config.get("strict", True))
    )
    extraction_mode = str(
        extraction_config.get("mode") or "sync"
    )
    async_extraction = bool(
        extraction_mode == "async" and strict_lifecycle
    )
    async_shadow = bool(
        extraction_mode == "sync"
        and strict_lifecycle
        and bool(extraction_config.get("async_shadow", True))
    )
    experience_config = dict(
        (config or {}).get("event_experience") or {}
    )
    require_experience = bool(
        experience_config.get("enabled", True)
        and experience_config.get(
            "required_before_event_design",
            True,
        )
    )
    if async_extraction:
        try:
            enqueue_started = time.perf_counter()
            result = _enqueue_extraction_job(
                root,
                assistant_text=assistant_text,
                session_id=host_session_id,
                turn_id=turn_id,
                require_event_experience=require_experience,
                execution_mode="async_strict",
            )
            stop_enqueue_ms = round(
                (time.perf_counter() - enqueue_started) * 1000.0,
                3,
            )
            result["extraction_execution_mode"] = "async_strict"
            if result.get("status") == "queued":
                result["worker_started"] = _spawn_extraction_worker(root)
        except Exception as exc:
            result = {
                "status": "failed",
                "reason": f"extraction enqueue failed: {exc}",
                "receipt_id": "",
                "recorded_events": [],
            }
    else:
        try:
            _, commit_turn, _ = _load_state_runtime()
            sync_started = time.perf_counter()
            result = commit_turn(
                root,
                assistant_text,
                session_id=host_session_id,
                turn_id=turn_id,
            )
            stop_sync_ms = round(
                (time.perf_counter() - sync_started) * 1000.0,
                3,
            )
            result["extraction_execution_mode"] = (
                "sync_with_async_shadow"
                if async_shadow
                else "sync"
            )
        except Exception as exc:
            result = {
                "status": "failed",
                "reason": f"state commit runtime failed: {exc}",
                "receipt_id": "",
                "recorded_events": [],
            }
        if (
            str(result.get("status") or "") in {"proposed", "no_delta"}
            or str(result.get("result_kind") or "") == "no_delta"
        ):
            review_turn = _prepared_turn_for_stop(
                root,
                session_id=host_session_id,
                turn_id=turn_id,
            )
            review_result = _record_automatic_experience_reviews(
                root,
                assistant_text=assistant_text,
                result=result,
                turn=review_turn,
            )
            result["experience_review"] = review_result
            _persist_experience_review_diagnostics(
                root,
                receipt_id=str(result.get("receipt_id") or ""),
                proposal_id=str(result.get("proposal_id") or ""),
                diagnostics=review_result.get("diagnostics") or [],
                source="sync_stop",
            )
        if async_shadow:
            authoritative_proposal_id = str(
                result.get("proposal_id") or ""
            )
            if (
                str(result.get("status") or "") == "proposed"
                and authoritative_proposal_id
            ):
                shadow_enqueue_started = time.perf_counter()
                try:
                    shadow = _enqueue_extraction_job(
                        root,
                        assistant_text=assistant_text,
                        session_id=host_session_id,
                        turn_id=turn_id,
                        require_event_experience=require_experience,
                        execution_mode="async_shadow",
                        authoritative_proposal_id=(
                            authoritative_proposal_id
                        ),
                    )
                    stop_enqueue_ms = round(
                        (
                            time.perf_counter()
                            - shadow_enqueue_started
                        )
                        * 1000.0,
                        3,
                    )
                    shadow["worker_started"] = (
                        _spawn_extraction_worker(root)
                        if shadow.get("status") == "queued"
                        else False
                    )
                    result["shadow_extraction"] = shadow
                    if isinstance(shadow.get("job"), Mapping):
                        result["shadow_job"] = dict(shadow["job"])
                except Exception as exc:
                    stop_enqueue_ms = round(
                        (
                            time.perf_counter()
                            - shadow_enqueue_started
                        )
                        * 1000.0,
                        3,
                    )
                    result["shadow_extraction"] = {
                        "status": "failed",
                        "reason": (
                            "async shadow enqueue failed: "
                            + _safe_worker_diagnostic(exc)
                        ),
                        "recorded_events": [],
                        "proposal_events": [],
                    }
            else:
                result["shadow_extraction"] = {
                    "status": "skipped",
                    "reason": (
                        "authoritative sync result did not produce "
                        "a valid proposal"
                    ),
                    "recorded_events": [],
                    "proposal_events": [],
                }
    telemetry = dict(result.get("telemetry") or {})
    telemetry["hook_stop"] = {
        "sync_ms": stop_sync_ms,
        "enqueue_ms": stop_enqueue_ms,
        "total_ms": round(
            (time.perf_counter() - stop_started) * 1000.0,
            3,
        ),
    }
    result["telemetry"] = telemetry
    if result.get("status") == "skipped" and result.get("reason") == "no_prepared_turn":
        return 0
    if (
        grill_service is not None
        and not _status_failed(result)
        and not async_extraction
    ):
        try:
            grill_service.complete_execution(
                project_root=grill_root,
                host_session_id=host_session_id,
                handoff_turn_id=turn_id,
            )
        except Exception:
            pass
    _run_session_end(payload, emit=False)
    _stop_output(
        result,
        stop_hook_active=bool(payload.get("stop_hook_active", False)),
        fail_closed=bool((config or {}).get("state", {}).get("fail_closed", False)),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session-start", action="store_true")
    parser.add_argument("--session-end", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--extraction-worker", action="store_true")
    parser.add_argument("--project-root")
    parser.add_argument("--worker-id")
    parser.add_argument("--startup-status")
    args, _ = parser.parse_known_args()
    if _truthy(os.environ.get(DISABLE_ENV)):
        return 0

    if args.extraction_worker:
        project_root = Path(
            str(args.project_root or os.getcwd())
        ).expanduser().resolve()
        worker_id = str(args.worker_id or "").strip() or (
            f"hook-worker-{os.getpid()}"
        )
        startup_status_path: Path | None = None
        if str(args.startup_status or "").strip():
            try:
                startup_status_path = _validated_worker_startup_path(
                    project_root,
                    str(args.startup_status),
                )
            except ValueError:
                return 2
        return _run_extraction_worker(
            project_root,
            worker_id=worker_id,
            startup_status_path=startup_status_path,
        )

    payload = {} if args.session_start else _load_payload()
    if (
        args.session_end
        or payload.get("hook_event_name") == "SessionEnd"
    ):
        return _run_session_end(payload)
    if args.stop or payload.get("hook_event_name") == "Stop":
        return _run_stop(payload)
    cwd = _cwd(payload)
    root, config, config_error = _find_project(cwd)
    prompt = _prompt(payload)
    if args.session_start:
        if root is None:
            return 0
        if config_error:
            print(f"plot-rag-gate: INDEX_UNAVAILABLE for {root}: {config_error}")
        else:
            try:
                _, _, state_doctor = _load_state_runtime()
                health = state_doctor(root)
                state_status = health.get("status", "unknown")
            except Exception:
                state_status = "unavailable"
            extraction_note = ""
            close_note = ""
            extraction_config = dict(
                ((config or {}).get("performance") or {}).get(
                    "extraction"
                )
                or {}
            )
            if (
                str(extraction_config.get("mode") or "sync") == "async"
                and (root / ".plot-rag" / "state.sqlite3").is_file()
            ):
                try:
                    queue = _extraction_queue(root)
                    recovered = queue.recover_stale_running()
                    queued = queue.list_jobs(status="queued", limit=1000)
                    running = queue.list_jobs(status="running", limit=1000)
                    worker_started = bool(
                        queued and _spawn_extraction_worker(root)
                    )
                    extraction_note = (
                        f"; extraction_recovered={len(recovered)}"
                        f"; extraction_queued={len(queued)}"
                        f"; extraction_running={len(running)}"
                        f"; extraction_worker_started={str(worker_started).lower()}"
                    )
                except Exception as exc:
                    extraction_note = (
                        "; extraction_recovery=failed"
                        f"; extraction_reason={exc}"
                    )
            try:
                close_pending = _refresh_session_close_entries(
                    root,
                    config=config or {},
                )
                close_note = (
                    f"; session_close_pending={len(close_pending)}"
                )
            except Exception as exc:
                close_note = (
                    "; session_close_pending=unreadable"
                    f"; session_close_reason={_safe_worker_diagnostic(exc)}"
                )
            print(
                f"plot-rag-gate: enabled for {root}; "
                f"state={state_status}; Grill precedes plot prepare and initialization"
                f"{extraction_note}{close_note}."
            )
        return 0

    host_session_id = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    )
    turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
    initialization_turn_id = _effective_initialization_turn_identity(
        payload,
        prompt,
    )
    initialization_meta_bypass = is_unrelated_grill_meta_turn(payload, prompt)
    try:
        active_initialization = (
            None
            if initialization_meta_bypass
            else _active_initialization(
                cwd,
                root,
                host_session_id=host_session_id,
            )
        )
    except InitializationLookupError as exc:
        print(
            json.dumps(
                _initialization_lookup_failure_output(str(exc)),
                ensure_ascii=False,
            )
        )
        return 0
    if active_initialization is not None:
        active_host_turn_id = str(
            active_initialization[1].get("host_turn_id") or ""
        )
        same_initialization_turn = bool(
            initialization_turn_id
            and active_host_turn_id == initialization_turn_id
        )
        replayed_grill_handoff = False
        replayed_decision: dict[str, Any] | None = None
        replay_grill_config: dict[str, Any] | None = None
        if initialization_turn_id:
            try:
                _, replay_grill_service, replay_grill_config = _grill_service(
                    root or cwd,
                    config,
                )
                replayed_decision = replay_grill_service.turn_response(
                    project_root=root or cwd,
                    host_session_id=host_session_id,
                    turn_id=initialization_turn_id,
                    prompt=prompt,
                    task_family="initialization",
                    continuation=False,
                )
                if (
                    isinstance(replayed_decision, dict)
                    and str(replayed_decision.get("action") or "") == "proceed"
                    and str(replayed_decision.get("task_family") or "")
                    == "initialization"
                    and active_host_turn_id not in {"", initialization_turn_id}
                ):
                    replayed_decision = {
                        "action": "conflict",
                        "reason": "active_initialization_turn_conflict",
                        "owns_turn": True,
                        "suppress_plot_receipt": True,
                        "suppress_plot_stop_extract": True,
                        "task_family": "initialization",
                    }
                replayed_grill_handoff = bool(
                    isinstance(replayed_decision, dict)
                    and str(replayed_decision.get("action") or "") == "proceed"
                    and str(replayed_decision.get("task_family") or "")
                    == "initialization"
                )
            except Exception:
                replayed_grill_handoff = False
        if (
            isinstance(replayed_decision, dict)
            and str(replayed_decision.get("action") or "") == "conflict"
        ):
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _grill_context(
                                replayed_decision,
                                project_root=root or cwd,
                                config=(
                                    replay_grill_config
                                    or _effective_grill_config(root or cwd, config)
                                ),
                                project_config=config,
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        initialization_output = _handle_initialization_submit(
            payload,
            cwd=cwd,
            project_root=root,
            prompt=prompt,
            forced_action=(
                "start"
                if replayed_grill_handoff or same_initialization_turn
                else None
            ),
            active_initialization=active_initialization,
        )
        if initialization_output is not None:
            print(json.dumps(initialization_output, ensure_ascii=False))
            return 0

    if config is not None and not bool(config.get("enabled", True)):
        return 0

    grill_root = root or cwd
    try:
        classification_skip_phrases = list(
            _effective_grill_config(grill_root, config).get("skip_phrases") or []
        )
    except Exception:
        classification_skip_phrases = []
    initialization_requested = _initialization_start_requested(payload, prompt)
    plot_requested = resolve_plot_progression(
        payload,
        prompt,
        allow_short_continue=bool((config or {}).get("trigger_short_continue", True)),
        skip_phrases=classification_skip_phrases,
    )
    missing_config = bool(
        config_error and "missing project config" in config_error
    )
    configless_grill_allowed = bool(
        (root is None and config_error is None) or missing_config
    )
    grill_runtime_error: str | None = None
    resumed_event_gate: dict[str, Any] | None = None
    resumed_grill_decision: dict[str, Any] | None = None
    resumed_execution_prompt = ""
    try:
        _, grill_service, grill_config = _grill_service(grill_root, config)
        configured_grill_enabled = bool(
            config is not None and grill_config.get("enabled", True)
        )
        active_grill = (
            grill_service.active(
                project_root=grill_root,
                host_session_id=host_session_id,
            )
            if configured_grill_enabled or configless_grill_allowed
            else None
        )
        cached_grill = (
            grill_service.turn_response(
                project_root=grill_root,
                host_session_id=host_session_id,
                turn_id=turn_id,
            )
            if configured_grill_enabled or configless_grill_allowed
            else None
        )
        grill_enabled = bool(
            configured_grill_enabled
            or (
                configless_grill_allowed
                and (
                    initialization_requested
                    or (
                        active_grill is not None
                        and str(active_grill.get("task_family") or "")
                        == "initialization"
                    )
                    or (
                        cached_grill is not None
                        and str(cached_grill.get("task_family") or "")
                        == "initialization"
                    )
                )
            )
        )
    except Exception as exc:
        grill_service = None
        grill_config = _effective_grill_config(grill_root, config)
        grill_enabled = False
        active_grill = None
        cached_grill = None
        grill_runtime_error = str(exc)

    if (
        root is not None
        and grill_service is not None
        and not is_unrelated_grill_meta_turn(payload, prompt)
    ):
        pending_event = _pending_event_experience_handoff(
            root,
            host_session_id=host_session_id,
        )
        if pending_event is not None:
            resumed_event_gate = _answer_pending_event_experience(
                root,
                pending=pending_event,
                answer=prompt,
                current_turn_id=turn_id,
                config=config or {},
            )
            if resumed_event_gate.get("status") != "locked":
                pending_state = dict(pending_event.get("state") or {})
                try:
                    grill_service.mark_prepared(
                        project_root=grill_root,
                        host_session_id=host_session_id,
                        grill_session_id=str(
                            pending_state.get("grill_session_id") or ""
                        ),
                        expected_session_revision=int(
                            pending_state.get("revision") or 0
                        ),
                        receipt_id="",
                        prepare_status="awaiting_event_experience",
                        turn_id=str(
                            pending_state.get("handoff_turn_id") or ""
                        ),
                        prepare_result=resumed_event_gate,
                    )
                except Exception:
                    pass
                print(
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": "event_experience_not_locked",
                            "hookSpecificOutput": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": (
                                    _event_experience_context(
                                        resumed_event_gate
                                    )
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            resumed_grill_decision = dict(
                resumed_event_gate.pop(
                    "resumed_grill_decision",
                    {},
                )
            )
            resumed_execution_prompt = str(
                resumed_event_gate.pop(
                    "resumed_execution_prompt",
                    "",
                )
            )

    if active_grill is not None and is_unrelated_grill_meta_turn(payload, prompt):
        return 0

    if resumed_event_gate is not None:
        task_family = "plot"
    elif active_grill is not None:
        task_family = str(active_grill.get("task_family") or "plot")
    elif cached_grill is not None:
        task_family = str(cached_grill.get("task_family") or "plot")
    elif initialization_requested:
        task_family = "initialization"
    elif plot_requested:
        task_family = "plot"
    else:
        return 0
    if task_family == "initialization" and not host_session_id:
        return 0
    workflow_turn_id = (
        initialization_turn_id
        if task_family == "initialization"
        else turn_id
    )

    if config_error and not (
        task_family == "initialization" and missing_config
    ):
        request_id = hashlib.sha256(
            f"{host_session_id}\n{turn_id}\n{prompt}".encode("utf-8")
        ).hexdigest()[:12]
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": _context(
                            grill_root,
                            request_id,
                            config_error,
                            None,
                        ),
                    }
                },
                ensure_ascii=False,
            )
        )
        return 0
    if root is None and task_family == "plot":
        return 0
    if grill_runtime_error and (
        (config is not None and bool((config.get("grill") or {}).get("enabled", True)))
        or task_family == "initialization"
    ):
        grill_decision = {
            "action": "conflict",
            "reason": f"grill_runtime_failed: {grill_runtime_error}",
            "task_family": task_family,
            "suppress_plot_receipt": True,
            "suppress_plot_stop_extract": True,
        }
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": _grill_context(
                            grill_decision,
                            project_root=grill_root,
                            config=grill_config,
                            project_config=config,
                        ),
                    }
                },
                ensure_ascii=False,
            )
        )
        return 0

    if (
        task_family == "plot"
        and resumed_event_gate is None
        and root is not None
        and config_error is None
    ):
        pre_grill_prompt = str(
            (
                (cached_grill or {}).get("execution_prompt")
                if isinstance(cached_grill, Mapping)
                else ""
            )
            or prompt
        )
        pre_grill_artifact = _infer_artifact_context(pre_grill_prompt)
        pre_grill_branch = str(
            pre_grill_artifact.get("branch_id") or "main"
        )
        close_pending: dict[str, Any] | None = None
        try:
            close_entries = _refresh_session_close_entries(
                root,
                config=config or {},
            )
            close_pending = next(
                (
                    entry
                    for entry in close_entries
                    if str(entry.get("branch_id") or "")
                    == pre_grill_branch
                ),
                None,
            )
        except Exception as exc:
            close_pending = {
                "code": "failed",
                "blocking": True,
                "branch_id": pre_grill_branch,
                "reason": (
                    "session_close_pending_unreadable: "
                    + _safe_worker_diagnostic(exc)
                ),
            }
        try:
            pre_grill_barrier = _latest_extraction_barrier(
                root,
                config=config or {},
                branch_id=pre_grill_branch,
            )
        except Exception as exc:
            pre_grill_barrier = {
                "code": "failed",
                "blocking": True,
                "branch_id": pre_grill_branch,
                "sequence_no": None,
                "reason": f"extraction_barrier_failed: {exc}",
            }
        if close_pending is not None:
            pre_grill_barrier = {
                **dict(pre_grill_barrier),
                **dict(close_pending),
                "blocking": True,
                "session_close_pending": True,
            }
        if str(pre_grill_barrier.get("code") or "") != "disabled":
            try:
                _complete_resolved_extraction_grills(
                    root,
                    grill_service=grill_service,
                    branch_id=pre_grill_branch,
                )
            except Exception:
                # Closing a previously resolved Grill handoff is best-effort
                # control-plane cleanup.  It must never turn a healthy
                # continuity barrier lookup into a new story block.
                pass
        if bool(pre_grill_barrier.get("blocking")):
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": "extraction_barrier_blocking",
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _barrier_context(
                                pre_grill_barrier
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return 0

    intent_context = ""
    execution_prompt = prompt
    grill_decision: dict[str, Any] | None = None
    if resumed_event_gate is not None:
        grill_decision = resumed_grill_decision
        execution_prompt = resumed_execution_prompt
        intent_context = _grill_context(
            grill_decision or {},
            project_root=grill_root,
            config=grill_config,
            project_config=config,
        )
    elif grill_enabled and grill_service is not None:
        try:
            grill_decision = grill_service.process(
                project_root=grill_root,
                prompt=prompt,
                task_family=task_family,
                host_session_id=host_session_id,
                turn_id=workflow_turn_id,
                required_fields=list(
                    grill_config.get("required_fields") or []
                ),
                max_questions=int(grill_config.get("max_questions") or 6),
                ttl_seconds=int(
                    grill_config.get("session_ttl_seconds") or 21600
                ),
                skip_phrases=list(grill_config.get("skip_phrases") or []),
                cancel_phrases=list(
                    grill_config.get("cancel_phrases") or []
                ),
                continuation=(
                    task_family == "plot"
                    and classify_task_family(
                        prompt,
                        skip_phrases=classification_skip_phrases,
                    )
                    == "continuation"
                ),
            )
        except Exception as exc:
            grill_decision = {
                "action": "conflict",
                "reason": f"grill_runtime_failed: {exc}",
                "task_family": task_family,
                "suppress_plot_receipt": True,
                "suppress_plot_stop_extract": True,
            }
        intent_context = _grill_context(
            grill_decision,
            project_root=grill_root,
            config=grill_config,
            project_config=config,
        )
        if str(grill_decision.get("action") or "") != "proceed":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": intent_context,
                        }
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        execution_prompt = str(
            grill_decision.get("execution_prompt") or prompt
        )

    if task_family == "initialization":
        try:
            initialization_output = _handle_initialization_submit(
                payload,
                cwd=cwd,
                project_root=root,
                prompt=execution_prompt,
                forced_action="start" if grill_enabled else None,
            )
        except InitializationLookupError as exc:
            initialization_output = _initialization_lookup_failure_output(
                str(exc)
            )
        if initialization_output is None:
            initialization_output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "[PLOT_RAG_INITIALIZATION]\n"
                        "status: ERROR\n"
                        "reason: initialization handoff was not claimed\n"
                        "[/PLOT_RAG_INITIALIZATION]"
                    ),
                }
            }
        if intent_context:
            _prepend_additional_context(initialization_output, intent_context)
        if grill_service is not None and grill_decision is not None:
            if _initialization_handoff_succeeded(initialization_output):
                try:
                    grill_service.complete_execution(
                        project_root=grill_root,
                        host_session_id=host_session_id,
                        grill_session_id=str(
                            grill_decision.get("grill_session_id") or ""
                        ),
                    )
                except Exception:
                    pass
            else:
                specific = initialization_output.get("hookSpecificOutput")
                initialization_context = (
                    str(specific.get("additionalContext") or "")
                    if isinstance(specific, dict)
                    else ""
                )
                failure_matches = re.findall(
                    r"(?m)^reason:\s*(.+)$",
                    initialization_context,
                )
                failure_reason = (
                    str(failure_matches[-1]).strip()
                    if failure_matches
                    else "initialization handoff was not completed"
                )
                try:
                    failed_decision = grill_service.fail_handoff(
                        project_root=grill_root,
                        host_session_id=host_session_id,
                        grill_session_id=str(
                            grill_decision.get("grill_session_id") or ""
                        ),
                        turn_id=turn_id,
                        reason=failure_reason,
                    )
                except Exception as exc:
                    failed_decision = {
                        "action": "conflict",
                        "reason": (
                            "handoff_persistence_failed: "
                            f"{failure_reason}; persistence_error={exc}"
                        ),
                        "task_family": "initialization",
                        "grill_session_id": str(
                            grill_decision.get("grill_session_id") or ""
                        ),
                        "suppress_plot_receipt": True,
                        "suppress_plot_stop_extract": True,
                    }
                _prepend_additional_context(
                    initialization_output,
                    _grill_context(
                        failed_decision,
                        project_root=grill_root,
                        config=grill_config,
                        project_config=config,
                    ),
                )
        print(json.dumps(initialization_output, ensure_ascii=False))
        return 0

    plot_root = root or cwd
    effective_config_error = config_error
    if root is None and effective_config_error is None:
        effective_config_error = (
            f"missing project config: {cwd / '.plot-rag' / 'config.json'}"
        )
    artifact_context = _infer_artifact_context(execution_prompt)
    event_context = ""
    if root is not None and effective_config_error is None:
        try:
            barrier = _latest_extraction_barrier(
                root,
                config=config or {},
                branch_id=str(
                    artifact_context.get("branch_id") or "main"
                ),
            )
        except Exception as exc:
            barrier = {
                "code": "failed",
                "blocking": True,
                "branch_id": str(
                    artifact_context.get("branch_id") or "main"
                ),
                "sequence_no": None,
                "reason": f"extraction_barrier_failed: {exc}",
            }
        if bool(barrier.get("blocking")):
            output = {
                "decision": "block",
                "reason": "extraction_barrier_blocking",
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _barrier_context(barrier),
                },
            }
            if intent_context:
                _prepend_additional_context(output, intent_context)
            print(json.dumps(output, ensure_ascii=False))
            return 0

    event_gate_result: dict[str, Any] = {
        "status": "disabled",
        "required": False,
    }
    if resumed_event_gate is not None:
        event_gate_result = dict(resumed_event_gate)
    elif (
        root is not None
        and effective_config_error is None
        and grill_enabled
        and grill_decision is not None
        and str(grill_decision.get("action") or "") == "proceed"
    ):
        event_gate_result = _prepare_event_experience_gate(
            root,
            config=config or {},
            payload=payload,
            grill_decision=grill_decision,
            execution_prompt=execution_prompt,
            host_session_id=host_session_id,
            turn_id=turn_id,
        )
    if event_gate_result.get("status") not in {
        "disabled",
        "shadow",
    }:
        event_context = _event_experience_context(event_gate_result)
    if (
        bool(event_gate_result.get("required"))
        and event_gate_result.get("status") != "locked"
    ):
        if (
            event_gate_result.get("status") == "ask"
            and grill_service is not None
            and grill_decision is not None
        ):
            try:
                persisted = grill_service.mark_prepared(
                    project_root=grill_root,
                    host_session_id=host_session_id,
                    grill_session_id=str(
                        grill_decision.get("grill_session_id") or ""
                    ),
                    expected_session_revision=int(
                        grill_decision.get(
                            "grill_prepare_revision",
                            grill_decision.get("session_revision"),
                        )
                    ),
                    receipt_id="",
                    prepare_status="awaiting_event_experience",
                    turn_id=turn_id,
                    prepare_result=event_gate_result,
                )
                if persisted is None:
                    raise RuntimeError(
                        "Grill handoff session was not found"
                    )
            except Exception as exc:
                output = {
                    "decision": "block",
                    "reason": (
                        "event_experience_handoff_persistence_failed"
                    ),
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": (
                            "[PLOT_RAG_EVENT_EXPERIENCE]\n"
                            "status: blocked\n"
                            "reason: "
                            f"event_experience_handoff_persistence_failed: {exc}\n"
                            "remote_called: false\n"
                            "receipt_created: false\n"
                            "[/PLOT_RAG_EVENT_EXPERIENCE]"
                        ),
                    },
                }
                if intent_context:
                    _prepend_additional_context(output, intent_context)
                print(json.dumps(output, ensure_ascii=False))
                return 0
        output = {
            "decision": "block",
            "reason": "event_experience_not_locked",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": event_context,
            },
        }
        if intent_context:
            _prepend_additional_context(output, intent_context)
        print(json.dumps(output, ensure_ascii=False))
        return 0

    lifecycle_identity: dict[str, Any] | None = None
    if event_gate_result.get("status") == "locked":
        manifest = dict(event_gate_result.get("manifest") or {})
        contracts = [
            dict(item)
            for item in manifest.get("contracts") or []
            if isinstance(item, Mapping)
        ]
        seed_references = sorted(
            [
                {
                    "event_seed_id": str(
                        item.get("event_seed_id") or ""
                    ),
                    "event_seed_revision": int(
                        item.get("event_seed_revision") or 0
                    ),
                }
                for item in (
                    event_gate_result.get("event_seed_references") or []
                )
                if isinstance(item, Mapping)
            ],
            key=lambda item: (
                item["event_seed_id"],
                item["event_seed_revision"],
            ),
        )
        lifecycle_identity = {
            "intent_contract_hash": str(
                event_gate_result.get("intent_contract_hash") or ""
            ),
            "event_seed_manifest_hash": str(
                manifest.get("event_seed_manifest_hash") or ""
            ),
            "experience_contract_hashes": sorted(
                {
                    str(item.get("contract_hash") or "")
                    for item in contracts
                    if item.get("contract_hash")
                }
            ),
            "event_experience_control_revision": int(
                manifest.get("control_revision") or 0
            ),
            "event_seed_references": seed_references,
        }
    execution_prompt = _event_experience_prompt(
        execution_prompt,
        event_gate_result,
    )
    request_id = hashlib.sha256(
        f"{host_session_id}\n{turn_id}\n{execution_prompt}".encode("utf-8")
    ).hexdigest()[:12]
    state_result: dict[str, Any] | None = None
    if grill_service is not None and grill_decision is not None:
        try:
            handoff_state = grill_service.session_state(
                project_root=grill_root,
                host_session_id=host_session_id,
                grill_session_id=str(
                    grill_decision.get("grill_session_id") or ""
                ),
            )
        except Exception:
            handoff_state = None
        if (
            isinstance(handoff_state, dict)
            and str(handoff_state.get("handoff_turn_id") or "") == turn_id
            and isinstance(handoff_state.get("prepare_result"), dict)
        ):
            state_result = dict(handoff_state["prepare_result"])
    if (
        state_result is None
        and root is not None
        and effective_config_error is None
        and bool((config or {}).get("state", {}).get("auto_retrieve", True))
    ):
        try:
            prepare_turn, _, _ = _load_state_runtime()
            state_result = prepare_turn(
                plot_root,
                execution_prompt,
                request_id=request_id,
                session_id=host_session_id,
                turn_id=turn_id,
                artifact_stage=str(
                    artifact_context.get("artifact_stage") or "brainstorm"
                ),
                branch_id=str(
                    artifact_context.get("branch_id") or "main"
                ),
                chapter_no=artifact_context.get("chapter_no"),
                scene_index=artifact_context.get("scene_index"),
                artifact_id=str(
                    artifact_context.get("artifact_id") or ""
                ),
                task=str(artifact_context.get("task") or "prose"),
                lifecycle_identity=lifecycle_identity,
            )
        except Exception as exc:
            state_result = {
                "status": "failed",
                "reason": f"state prepare runtime failed: {exc}",
                "request_id": request_id,
                "receipt_id": "",
                "context": "",
            }
    if (
        state_result is not None
        and lifecycle_identity is not None
        and not _status_failed(state_result)
    ):
        persisted_identity = state_result.get("lifecycle_identity")
        if not isinstance(persisted_identity, Mapping):
            state_result = {
                **state_result,
                "status": "failed",
                "reason": "Prepare did not persist lifecycle identity",
            }
        elif dict(persisted_identity) != lifecycle_identity:
            state_result = {
                **state_result,
                "status": "failed",
                "reason": "Prepare lifecycle identity mismatch",
            }
    if (
        grill_service is not None
        and grill_enabled
        and grill_decision is not None
    ):
        handoff_persist_error: str | None = None
        try:
            persisted_handoff = grill_service.mark_prepared(
                project_root=grill_root,
                host_session_id=host_session_id,
                grill_session_id=str(
                    grill_decision.get("grill_session_id") or ""
                ),
                expected_session_revision=int(
                    grill_decision.get(
                        "grill_prepare_revision",
                        grill_decision.get("session_revision"),
                    )
                ),
                receipt_id=str(
                    (state_result or {}).get("receipt_id")
                    or (state_result or {}).get("receipt")
                    or ""
                ),
                prepare_status=str(
                    (state_result or {}).get("status")
                    or (
                        "failed"
                        if effective_config_error
                        else "not_requested"
                    )
                ),
                turn_id=turn_id,
                prepare_result=state_result,
            )
            if persisted_handoff is None:
                raise RuntimeError("Grill handoff session was not found")
        except Exception as exc:
            handoff_persist_error = str(exc)
        if handoff_persist_error:
            try:
                failed_decision = grill_service.fail_handoff(
                    project_root=grill_root,
                    host_session_id=host_session_id,
                    grill_session_id=str(
                        grill_decision.get("grill_session_id") or ""
                    ),
                    turn_id=turn_id,
                    reason=handoff_persist_error,
                )
            except Exception:
                failed_decision = None
            conflict = failed_decision or {
                "action": "conflict",
                "reason": (
                    "handoff_persistence_failed: "
                    f"{handoff_persist_error}"
                ),
                "task_family": "plot",
                "grill_session_id": str(
                    grill_decision.get("grill_session_id") or ""
                ),
                "suppress_plot_receipt": True,
                "suppress_plot_stop_extract": True,
            }
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _grill_context(
                                conflict,
                                project_root=grill_root,
                                config=grill_config,
                                project_config=config,
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
            return 0
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _context(
                plot_root,
                request_id,
                effective_config_error,
                state_result,
            ),
        }
    }
    if event_context:
        _prepend_additional_context(output, event_context)
    if intent_context:
        _prepend_additional_context(output, intent_context)
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
