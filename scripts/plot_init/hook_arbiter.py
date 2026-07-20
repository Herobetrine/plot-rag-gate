"""Helpers for giving an active initialization workflow priority over plot hooks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


SHORT_CONTINUE = frozenset(
    {
        "继续",
        "继续吧",
        "接着来",
        "接着",
        "下一步",
        "往下",
        "就这样",
        "按这个来",
    }
)

TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED", "SUPERSEDED"})
INPUT_STATUSES = frozenset({"ACTIVE", "NEEDS_INPUT", "PAUSED_REMOTE"})
WAITING_STATUSES = frozenset(
    {"READY_TO_PROPOSE", "PROPOSAL_FROZEN", "STALE_SOURCE", "STALE_CANON"}
)

_EXPLICIT_START = (
    "开始初始化作品",
    "开始作品初始化",
    "初始化一部作品",
    "初始化一部",
    "从零初始化",
    "整理现有作品",
    "整理成标准结构",
    "导入现有小说",
    "建立作品世界",
    "创建新作",
)
_INITIALIZATION_REFERENCE_SUBJECTS = (
    "触发词",
    "关键词",
    "口令",
    "这个词",
    "这个短语",
    "这句话",
)
_INITIALIZATION_RUNTIME_SUBJECTS = (
    "插件",
    "hook",
    "钩子",
    "门禁",
)
_INITIALIZATION_DISCUSSION_CUES = (
    "提到",
    "引用",
    "会触发",
    "触发条件",
    "触发规则",
    "会接管",
    "会做什么",
    "做什么流程",
    "什么流程",
    "执行流程",
    "工作流程",
    "为什么",
    "如何",
    "怎么",
    "是否",
    "合理",
    "什么意思",
    "含义",
    "解释",
    "说明",
    "检查",
    "审查",
)
_INITIALIZATION_DEFINITION_CUES = (
    "含义",
    "是什么意思",
    "什么意思",
    "定义",
)
_META_DIRECT_MARKERS = (
    "全量审查",
    "项目审查",
    "代码审查",
    "审查插件",
    "review",
    "audit",
    "接管普通消息",
    "接管无关消息",
    "普通消息接管",
    "无关消息接管",
)
_META_STRONG_SUBJECTS = (
    "插件",
    "hook",
    "git",
    "mcp",
    "cli",
    "schema",
    "运行面",
    "github actions",
    "ci门禁",
)
_META_AMBIGUOUS_SUBJECTS = (
    "门禁",
    "钩子",
    "仓库",
)
_META_COMPOUNDS = (
    "初始化框架",
    "初始化流程",
    "升级计划",
    "改造计划",
    "协议设计",
    "数据模型",
    "安装缓存",
    "运行缓存",
    "插件缓存",
    "发布流程",
)
_META_WEAK_SUBJECTS = (
    "代码",
    "脚本",
    "测试",
    "文档",
    "版本",
    "缓存",
    "工作流",
    "workflow",
    "实现",
)
_META_ACTIONS = (
    "实现",
    "重构",
    "修复",
    "修改",
    "更新",
    "升级",
    "改造",
    "审查",
    "检查",
    "校验",
    "维护",
    "运行",
    "执行",
    "补齐",
    "新增",
    "删除",
    "合并",
    "提交",
    "推送",
    "部署",
    "发布",
    "排查",
    "诊断",
)
_STORY_CONTEXT_MARKERS = (
    "主角",
    "角色",
    "人物",
    "反派",
    "守卫",
    "宗门",
    "法器",
    "功法",
    "古籍",
    "符文",
    "法术",
    "魔法",
    "修仙",
    "真元",
    "灵气",
    "境界",
    "能力",
    "技能",
    "道具",
    "世界",
    "剧情",
    "情节",
    "故事",
    "章节",
    "正文",
    "网文",
    "悬疑",
    "背叛",
    "放行",
)
_LOCAL_SEGMENT_RE = re.compile(r"[\r\n。！？!?；;，,、]+")
_ACTION_TO_SUBJECT_GAP_RE = re.compile(
    r"(?:这|这个|这份|该|当前|现有|相关|整个|本地|默认|"
    r"一下|一遍|重新|彻底|全面|直接|其|上述|刚才的|新的|旧的)*"
)
_SUBJECT_TO_ACTION_GAP_RE = re.compile(
    r"(?:的|需要|需|要|待|进行|作|继续|必须|应|应该|应当|"
    r"得|正在|再|重新|彻底|全面|一下|一遍|相关|问题|缺陷|"
    r"代码|脚本|逻辑|流程|状态|测试|文档|版本|缓存)*"
)
_CANCEL_MARKERS = ("取消初始化", "终止初始化", "停止初始化", "先暂停初始化")
_INSPECT_MARKERS = (
    "查看初始化",
    "初始化状态",
    "查看冲突",
    "还有什么缺口",
    "查看缺口",
    "查看来源",
    "查看提案",
)
_PROPOSE_MARKERS = ("冻结提案", "生成初始化提案", "提交初始化提案", "就按这个提案")
_ANSWER_MARKERS = (
    "选择",
    "选第",
    "就按",
    "你来定",
    "改成",
    "设为",
    "答案是",
)


def is_initialization_storage_path(path: Path | str) -> bool:
    """Return True for paths that authority discovery and plot RAG must exclude."""

    normalized = str(path).replace("\\", "/").casefold()
    parts = [part for part in normalized.split("/") if part]
    if ".plot-rag-init" in parts:
        return True
    if ".plot-rag" in parts:
        index = parts.index(".plot-rag")
        tail = parts[index + 1 :]
        if "init-sessions" in tail or "init.sqlite3" in tail:
            return True
        if any(part.startswith("init-") for part in tail):
            return True
    return normalized.endswith("/init.sqlite3")


def _plain(prompt: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:]+", "", str(prompt or "")).casefold()


def _ascii_marker_pattern(marker: str) -> re.Pattern[str]:
    escaped = re.escape(str(marker).casefold()).replace(r"\ ", r"\s+")
    return re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def _marker_spans(text: str, marker: str) -> list[tuple[int, int]]:
    folded = str(text or "").casefold()
    if marker.isascii() and any(character.isalpha() for character in marker):
        return [match.span() for match in _ascii_marker_pattern(marker).finditer(folded)]
    needle = str(marker).casefold()
    spans: list[tuple[int, int]] = []
    start = 0
    while needle:
        index = folded.find(needle, start)
        if index < 0:
            break
        spans.append((index, index + len(needle)))
        start = index + 1
    return spans


def _contains_marker(text: str, marker: str) -> bool:
    return bool(_marker_spans(text, marker))


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(_contains_marker(text, marker) for marker in markers)


def _is_explicit_initialization_start(prompt: str) -> bool:
    compact = _plain(prompt)
    return any(_plain(marker) in compact for marker in _EXPLICIT_START)


def _has_quoted_explicit_start(prompt: str) -> bool:
    text = str(prompt or "")
    quote_pairs = {
        '"': '"',
        "'": "'",
        "“": "”",
        "‘": "’",
        "「": "」",
        "『": "』",
        "`": "`",
    }
    for marker in _EXPLICIT_START:
        for start, end in _marker_spans(text, marker):
            left = text[:start].rstrip()
            right = text[end:].lstrip()
            if left and right and quote_pairs.get(left[-1]) == right[0]:
                return True
    return False


def _is_explicit_initialization_discussion(prompt: str) -> bool:
    """Return True when an explicit start phrase is only being discussed."""

    text = str(prompt or "").strip()
    if not text or not _is_explicit_initialization_start(text):
        return False
    for segment in _LOCAL_SEGMENT_RE.split(text):
        segment = segment.strip()
        if not segment or not _is_explicit_initialization_start(segment):
            continue
        if _contains_any(segment, _INITIALIZATION_DEFINITION_CUES):
            return True
        if not _contains_any(segment, _INITIALIZATION_DISCUSSION_CUES):
            continue
        if (
            _has_quoted_explicit_start(segment)
            or _contains_any(segment, _INITIALIZATION_RUNTIME_SUBJECTS)
            or _contains_any(segment, _INITIALIZATION_REFERENCE_SUBJECTS)
        ):
            return True
    return False


def has_initialization_story_context(prompt: str) -> bool:
    """Return True for concrete fictional-world language in the current turn."""

    return _contains_any(str(prompt or ""), _STORY_CONTEXT_MARKERS)


def _direct_meta_marker(segment: str) -> bool:
    for marker in _META_DIRECT_MARKERS:
        for start, end in _marker_spans(segment, marker):
            if marker == "代码审查" and segment[end : end + 1] in {"员", "师"}:
                continue
            return True
    return False


def _subject_action_pair(
    segment: str,
    subjects: tuple[str, ...],
) -> bool:
    subject_spans = [
        span
        for subject in subjects
        for span in _marker_spans(segment, subject)
    ]
    action_spans = [
        span
        for action in _META_ACTIONS
        for span in _marker_spans(segment, action)
    ]
    for subject_start, subject_end in subject_spans:
        for action_start, action_end in action_spans:
            if action_end <= subject_start:
                gap = re.sub(r"\s+", "", segment[action_end:subject_start])
                if len(gap) <= 12 and _ACTION_TO_SUBJECT_GAP_RE.fullmatch(gap):
                    return True
            elif subject_end <= action_start:
                gap = re.sub(r"\s+", "", segment[subject_end:action_start])
                if len(gap) <= 12 and _SUBJECT_TO_ACTION_GAP_RE.fullmatch(gap):
                    return True
    return False


def is_initialization_meta_prompt(prompt: str) -> bool:
    """Return True when a turn is about the plugin/workflow rather than the story."""

    text = str(prompt or "").strip()
    if not text:
        return False
    explicit_start = _is_explicit_initialization_start(text)
    if explicit_start and _is_explicit_initialization_discussion(text):
        return True
    for segment in _LOCAL_SEGMENT_RE.split(text):
        segment = segment.strip()
        if not segment:
            continue
        if explicit_start and _is_explicit_initialization_start(segment):
            continue
        story_context = has_initialization_story_context(segment)
        if _direct_meta_marker(segment):
            return True
        if _subject_action_pair(segment, _META_STRONG_SUBJECTS):
            return True
        if story_context:
            continue
        if _contains_any(segment, _META_COMPOUNDS):
            return True
        if _subject_action_pair(segment, _META_AMBIGUOUS_SUBJECTS):
            return True
        if _subject_action_pair(segment, _META_WEAK_SUBJECTS):
            return True
    return False


def resolve_initialization_intent(
    prompt: str,
    *,
    active_session: dict[str, Any] | None = None,
) -> str:
    """Classify a prompt without creating a session or plot receipt."""

    text = str(prompt or "").strip()
    compact = _plain(text)
    session_active = bool(
        active_session
        and str(active_session.get("status") or "") not in TERMINAL_STATUSES
    )
    status = str((active_session or {}).get("status") or "").upper()
    if any(_plain(marker) in compact for marker in _CANCEL_MARKERS):
        return "cancel" if session_active else "none"
    if any(_plain(marker) in compact for marker in _INSPECT_MARKERS):
        return "inspect" if session_active else "none"
    if any(_plain(marker) in compact for marker in _PROPOSE_MARKERS):
        return "propose" if session_active else "none"
    if is_initialization_meta_prompt(text):
        return "none"
    if not session_active and _is_explicit_initialization_start(text):
        return "start"
    if session_active:
        if status in WAITING_STATUSES:
            return "wait" if text else "none"
        if status not in INPUT_STATUSES:
            return "wait" if text else "none"
        if compact in {_plain(value) for value in SHORT_CONTINUE}:
            return "advance"
        if any(_plain(marker) in compact for marker in _ANSWER_MARKERS):
            return "answer"
        if text:
            return "answer"
        return "none"
    return "none"


def arbitrate_initialization_hook(
    payload: dict[str, Any],
    *,
    active_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a single-workflow decision for UserPromptSubmit or Stop."""

    event_name = str(payload.get("hook_event_name") or "")
    session_active = bool(
        active_session
        and str(active_session.get("status") or "") not in TERMINAL_STATUSES
    )
    if event_name == "Stop":
        return {
            "workflow": "initialization" if session_active else "unclaimed",
            "action": "suppress_stop_extract" if session_active else "none",
            "session_id": (
                str(active_session.get("session_id")) if active_session else None
            ),
            "suppress_plot_receipt": session_active,
            "suppress_plot_stop_extract": session_active,
        }
    prompt = str(payload.get("prompt") or "")
    intent = resolve_initialization_intent(prompt, active_session=active_session)
    claimed = intent != "none"
    return {
        "workflow": "initialization" if claimed else "unclaimed",
        "action": intent,
        "session_id": (
            str(active_session.get("session_id")) if active_session else None
        ),
        "suppress_plot_receipt": claimed or session_active,
        "suppress_plot_stop_extract": claimed or session_active,
    }
