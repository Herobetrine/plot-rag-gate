"""Deterministic, non-canonical intent grilling for creative execution hooks.

The grill store is deliberately independent from continuity and initialization
canon.  It records only the user's task purpose, one-question-at-a-time
clarification progress, and hook idempotency responses.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:  # Package import, for example ``scripts.grill_gate``.
    from .sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )
except ImportError:  # Direct script/runtime import with ``scripts`` on sys.path.
    from sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )


INTENT_SCHEMA_VERSION = "plot-rag-intent/v1"
GRILL_DATABASE_SCHEMA_VERSION = 1
GRILL_DATABASE_TABLES = frozenset(
    {"grill_meta", "grill_sessions", "grill_turn_responses"}
)

FIELD_ORDER = (
    "problem_to_solve",
    "expected_deliverable",
    "reader_experience",
    "protagonist_drive_conflict",
    "scope_endpoint",
    "success_criteria",
    "hard_constraints",
    "model_autonomy",
)

FIELD_LABELS = {
    "problem_to_solve": "本轮要解决的问题",
    "expected_deliverable": "预期交付物",
    "reader_experience": "剧情作用或读者体验",
    "protagonist_drive_conflict": "主角欲望与当前冲突",
    "scope_endpoint": "推演范围和终点",
    "success_criteria": "成功标准",
    "hard_constraints": "硬约束、禁区与保留项",
    "model_autonomy": "允许模型自行决定的空间",
}

DEFAULT_REQUIRED_FIELDS = FIELD_ORDER
DEFAULT_SKIP_PHRASES = (
    "跳过grill",
    "跳过 grill",
    "跳过盘问",
    "跳过目的确认",
    "不需要目的确认",
    "按现有要求直接执行",
    "直接执行不要追问",
    "直接执行，不要追问",
)
DEFAULT_CANCEL_PHRASES = (
    "取消本轮grill",
    "取消本轮 grill",
    "结束本轮盘问",
    "停止本轮盘问",
    "放弃本轮任务",
)
REPEAT_PHRASES = (
    "继续",
    "继续吧",
    "继续推进",
    "继续写",
    "开始",
    "开始吧",
    "下一步",
    "接着来",
    "接着写",
    "往下",
    "往下写",
    "写下一章",
    "照此执行",
    "按这个来",
    "按计划推进",
    "一口气推进到底",
    "一口气推进到底，最后再审查",
    "继续推进到底",
    "继续推进到底，最后再审查",
)
INSPECT_PHRASES = (
    "为什么问这个",
    "为什么要问",
    "还剩几题",
    "还有几题",
    "查看grill状态",
    "查看 grill 状态",
    "当前grill状态",
    "当前 grill 状态",
)

ACTIVE_STATUS = "AWAITING_ANSWER"
EXECUTING_STATUS = "EXECUTING"
COMPLETED_STATUS = "COMPLETED"
CANCELLED_STATUS = "CANCELLED"
HANDOFF_FAILED_STATUS = "HANDOFF_FAILED"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS grill_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grill_sessions(
    grill_session_id TEXT PRIMARY KEY,
    host_session_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    task_family TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS grill_sessions_lookup
ON grill_sessions(host_session_id, project_root, status, updated_at);

CREATE TABLE IF NOT EXISTS grill_turn_responses(
    host_session_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(host_session_id, project_root, turn_id)
);
"""

_GENERIC_PROMPTS = {
    "剧情推演",
    "推演剧情",
    "继续剧情",
    "继续推进剧情",
    "写剧情",
    "写正文",
    "写下一章",
    "规划剧情",
    "初始化作品",
    "初始化一部作品",
    "整理现有作品",
    "继续",
    "开始",
    "开始吧",
}
_DELIVERABLE_PATTERNS = (
    (r"卷纲", "可直接执行的卷纲"),
    (r"章纲", "可直接执行的章纲"),
    (r"事件链", "带因果、选择和状态变化的事件链"),
    (r"场景", "可直接落笔的场景方案"),
    (r"正文|续写|写下一章|第[一二三四五六七八九十百千0-9]+章", "可发布或可继续审修的章节正文"),
    (r"初始化|创建新作|建立作品", "InitializationBundle 与标准作品骨架"),
    (r"整理现有|导入现有|标准结构|结构化格式", "只读来源清单、冲突缺口与标准化逐文件 diff"),
    (r"推演|推进|规划|设计", "可供用户裁决的剧情推进 proposal"),
)
_EXPERIENCE_TERMS = (
    "爽",
    "悬疑",
    "悬念",
    "压迫",
    "紧张",
    "恐怖",
    "治愈",
    "热血",
    "燃",
    "反转",
    "惊喜",
    "震撼",
    "悲壮",
    "轻松",
    "幽默",
    "代入",
    "期待",
    "兑现",
    "爽点",
    "钩子",
)
_SCOPE_PATTERNS = (
    r"第[一二三四五六七八九十百千0-9]+章",
    r"下一章",
    r"本章",
    r"这一章",
    r"第一卷|第二卷|第三卷|本卷|下一卷",
    r"这一幕|下一幕|这一场|下一场|本场",
    r"直到[^，。；;\n]+",
    r"以[^，。；;\n]+为终点",
    r"推到[^，。；;\n]+",
)
_SUCCESS_MARKERS = (
    "成功标准",
    "完成标准",
    "完成后",
    "确保",
    "必须发生",
    "必须完成",
    "最终要",
    "结果是",
    "拿到",
    "逃出",
    "击败",
    "揭示",
    "兑现",
    "不可逆",
    "为终点",
)
_CONSTRAINT_MARKERS = (
    "不要",
    "不得",
    "禁止",
    "不能",
    "不新增",
    "不修改",
    "不改变",
    "保持",
    "保留",
    "沿用",
    "只允许",
    "必须遵守",
    "限知",
)
_AUTONOMY_MARKERS = (
    "你来定",
    "自行决定",
    "自由发挥",
    "其余你决定",
    "细节由你",
    "可以自行",
    "不要自行",
    "严格按",
)
_DRIVE_MARKERS = (
    "主角",
    "主人公",
    "想要",
    "试图",
    "必须",
    "目标",
    "阻力",
    "冲突",
    "对手",
    "敌人",
    "遭遇",
    "面对",
    "对抗",
    "代价",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_revision(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeError(f"{field} must be a non-negative integer")
    return value


def _advance_revision(state: dict[str, Any]) -> int:
    revision = _require_revision(
        state.get("revision"),
        "Grill session state revision",
    ) + 1
    state["revision"] = revision
    return revision


def _compact(value: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:'\"“”‘’]+", "", str(value or "")).casefold()


def _entry(value: str = "", source: str = "unknown") -> dict[str, str]:
    return {"value": str(value or "").strip(), "source": source}


def _first_match(patterns: Sequence[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


def _contains_phrase(prompt: str, phrases: Sequence[str]) -> bool:
    compact = _compact(prompt)
    return any(_compact(phrase) in compact for phrase in phrases if str(phrase).strip())


def mask_terms_in_phrases(
    prompt: str,
    phrases: Sequence[str],
    terms: Sequence[str],
) -> str:
    """Mask selected terms only inside configured control-phrase matches."""

    text = str(prompt or "")
    separators = r"[\s，。！？!?、；;：:'\"“”‘’]*"
    for phrase in phrases:
        compact = _compact(phrase)
        if not compact:
            continue
        pattern = separators.join(re.escape(character) for character in compact)
        text = re.sub(
            pattern,
            lambda match: _mask_compact_terms(
                match.group(0),
                terms,
                separators=separators,
            ),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _mask_compact_terms(
    text: str,
    terms: Sequence[str],
    *,
    separators: str,
) -> str:
    masked = text
    for term in terms:
        compact = _compact(term)
        if not compact:
            continue
        pattern = separators.join(re.escape(character) for character in compact)
        masked = re.sub(pattern, "", masked, flags=re.IGNORECASE)
    return masked


def _specific_problem(prompt: str) -> str:
    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not text or _compact(text) in {_compact(item) for item in _GENERIC_PROMPTS}:
        return ""
    stripped = re.sub(
        r"^(?:请|帮我|现在|直接|马上|立即|开始|继续|接着|使用这个插件|"
        r"用这个插件|剧情推演|推演剧情)[，,:：\s]*",
        "",
        text,
    ).strip()
    if len(_compact(stripped)) < 10:
        return ""
    return stripped


def infer_contract(prompt: str, task_family: str) -> dict[str, Any]:
    """Extract only explicit or safely structural intent signals from a prompt."""

    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    fields = {field: _entry() for field in FIELD_ORDER}

    problem = _specific_problem(text)
    if problem:
        fields["problem_to_solve"] = _entry(problem, "prompt")

    for pattern, value in _DELIVERABLE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            fields["expected_deliverable"] = _entry(value, "prompt")
            break
    if task_family == "initialization" and not fields["expected_deliverable"]["value"]:
        fields["expected_deliverable"] = _entry(
            "InitializationBundle 与标准作品骨架",
            "workflow_default",
        )

    experience_hits = [term for term in _EXPERIENCE_TERMS if term in text]
    if experience_hits:
        fields["reader_experience"] = _entry(
            "、".join(dict.fromkeys(experience_hits)),
            "prompt",
        )

    if any(marker in text for marker in _DRIVE_MARKERS):
        fields["protagonist_drive_conflict"] = _entry(text, "prompt")

    scope = _first_match(_SCOPE_PATTERNS, text)
    if scope:
        fields["scope_endpoint"] = _entry(scope, "prompt")

    if any(marker in text for marker in _SUCCESS_MARKERS):
        fields["success_criteria"] = _entry(text, "prompt")

    if any(marker in text for marker in _CONSTRAINT_MARKERS):
        fields["hard_constraints"] = _entry(text, "prompt")

    if any(marker in text for marker in _AUTONOMY_MARKERS):
        fields["model_autonomy"] = _entry(text, "prompt")

    return {
        "schema_version": INTENT_SCHEMA_VERSION,
        "task_family": task_family,
        "fields": fields,
    }


def _default_value(field: str, task_family: str, contract: Mapping[str, Any]) -> str:
    deliverable = (
        ((contract.get("fields") or {}).get("expected_deliverable") or {}).get("value")
        or "本轮创作产物"
    )
    defaults = {
        "problem_to_solve": (
            "从用户原始任务中锁定一个会造成可观察状态变化的核心创作问题"
        ),
        "expected_deliverable": (
            "InitializationBundle 与标准作品骨架"
            if task_family == "initialization"
            else "可供用户裁决并能直接进入下一创作步骤的剧情 proposal"
        ),
        "reader_experience": "形成明确期待、有效阻力、阶段兑现和新的后续问题",
        "protagonist_drive_conflict": "以当前主角的具体欲望、现实阻力和失败代价组织行动",
        "scope_endpoint": "只推进到本轮核心冲突产生一次不可逆状态变化",
        "success_criteria": f"{deliverable}具备清晰因果、可执行节点和可核验状态变化",
        "hard_constraints": "不改写 accepted 事实，不凭空新增核心设定，不越过用户明确保留项",
        "model_autonomy": "模型可决定场景实现与次级冲突；核心设定、人物底线和终点服从合同",
    }
    return defaults[field]


def _recommended_answer(
    field: str,
    task_family: str,
    contract: Mapping[str, Any],
) -> str:
    problem = (
        ((contract.get("fields") or {}).get("problem_to_solve") or {}).get("value")
        or "当前任务"
    )
    recommendations = {
        "problem_to_solve": (
            f"本轮围绕“{problem}”形成一个由人物主动选择推动、"
            "并造成一次不可逆状态变化的解决方案。"
        ),
        "expected_deliverable": (
            ((contract.get("fields") or {}).get("expected_deliverable") or {}).get(
                "value"
            )
            or _default_value("expected_deliverable", task_family, contract)
        ),
        "reader_experience": (
            "以持续升级的期待与阻力为主体验，以阶段兑现和章末新问题为辅助体验。"
        ),
        "protagonist_drive_conflict": (
            "主角必须为一个眼前且具体的目标主动行动；对立方采取同样合理的阻止行动，"
            "失败会损失资源、关系、身份或退路中的至少一项。"
        ),
        "scope_endpoint": (
            "从当前有效状态开始，只推进到核心冲突造成一次不可逆变化，"
            "并停在新的后续问题出现处。"
        ),
        "success_criteria": (
            f"“{problem}”得到可执行解法；每个转折都有前因、人物选择、"
            "预期外结果和可记录的状态变化。"
        ),
        "hard_constraints": (
            "锁定 accepted 事实、角色底线、既定视角和用户保留项；"
            "新的核心设定与正典变化只作为 proposal。"
        ),
        "model_autonomy": (
            "模型可决定场景实现、次级阻力和表达细节；"
            "核心设定、人物底线、终点与正典变化由用户裁决。"
        ),
    }
    if task_family == "initialization" and field == "scope_endpoint":
        return (
            "plot_ready：先完成能支撑人物行动、冲突升级和第一阶段连载的最小可运行世界。"
        )
    return recommendations[field]


def _recommendation_rationale(field: str, task_family: str) -> str:
    rationales = {
        "problem_to_solve": "先锁定上游问题，后续检索、范围和成功标准才不会互相冲突。",
        "expected_deliverable": "单一可验收产物最容易控制范围，也便于下一步直接使用。",
        "reader_experience": "统一的体验目标能约束节奏、转折和信息释放。",
        "protagonist_drive_conflict": "欲望、阻力和代价能让剧情由人物选择推进。",
        "scope_endpoint": "以不可逆变化为终点，可以形成闭环又保留连载钩子。",
        "success_criteria": "可观察标准能让完成后的审查不依赖主观感觉。",
        "hard_constraints": "先锁定不能动的部分，可避免 RAG 缺口被误补成新正典。",
        "model_autonomy": "划清裁决边界后，模型既有发挥空间，也不会替用户做核心决定。",
    }
    if task_family == "initialization" and field == "scope_endpoint":
        return "plot_ready 能用最少交互建立可推演作品，避免先填满世界百科。"
    return rationales[field]


def _question(field: str, task_family: str) -> str:
    questions = {
        "problem_to_solve": (
            "这次你真正想解决的创作问题是什么？请用“谁要达成什么、"
            "最大阻力是什么、结束时什么必须改变”回答。"
        ),
        "expected_deliverable": "你希望本轮最终交付哪一种可直接验收的产物？",
        "reader_experience": "这一轮最想让读者获得什么主体验，辅助体验又是什么？",
        "protagonist_drive_conflict": "主角此刻最想得到什么，谁或什么在阻止，失败会失去什么？",
        "scope_endpoint": "本轮从哪里开始、推进到哪个明确终点就停止？",
        "success_criteria": "完成后用哪几条可观察标准判断这轮真的成功？",
        "hard_constraints": "哪些正典、人物底线、视角、设定或情节绝对不能动？",
        "model_autonomy": "哪些决定可以交给模型，哪些必须由你亲自裁决？",
    }
    if task_family == "initialization" and field == "scope_endpoint":
        return (
            "这次初始化要达到哪个目标档位：plot_ready、world_bible、"
            "normalize_only 还是 continuity_ready？"
        )
    return questions[field]


def _missing_fields(
    contract: Mapping[str, Any],
    required_fields: Sequence[str],
) -> list[str]:
    fields = contract.get("fields") or {}
    return [
        field
        for field in required_fields
        if not str((fields.get(field) or {}).get("value") or "").strip()
    ]


def _quick_path_ready(
    contract: Mapping[str, Any],
    required_fields: Sequence[str],
) -> bool:
    fields = contract.get("fields") or {}
    present = {
        field
        for field in required_fields
        if str((fields.get(field) or {}).get("value") or "").strip()
    }
    core = {
        "problem_to_solve",
        "expected_deliverable",
        "scope_endpoint",
    }
    supporting = {
        "reader_experience",
        "protagonist_drive_conflict",
        "success_criteria",
        "hard_constraints",
    }
    return core.issubset(present) and len(present & supporting) >= 2


def _fill_defaults(
    contract: dict[str, Any],
    task_family: str,
    fields: Sequence[str],
    *,
    source: str,
) -> None:
    for field in fields:
        current = contract["fields"][field]
        if not str(current.get("value") or "").strip():
            contract["fields"][field] = _entry(
                _default_value(field, task_family, contract),
                source,
            )


def _execution_prompt(state: Mapping[str, Any]) -> str:
    contract = state["contract"]
    summary = {
        field: {
            "label": FIELD_LABELS[field],
            "value": contract["fields"][field]["value"],
            "source": contract["fields"][field]["source"],
        }
        for field in FIELD_ORDER
    }
    return (
        f"{state['original_prompt']}\n\n"
        "[LOCKED_INTENT_CONTRACT]\n"
        f"{json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
        "[/LOCKED_INTENT_CONTRACT]"
    )


class GrillGateService:
    """Persist and advance a single-question intent interview."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)

    @property
    def exists(self) -> bool:
        return self.database_path.is_file()

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_sqlite_component_schema(
                    connection,
                    component="Grill intent state",
                    meta_table="grill_meta",
                    version_key="schema_version",
                    supported_version=GRILL_DATABASE_SCHEMA_VERSION,
                    owned_tables=GRILL_DATABASE_TABLES,
                    allowed_tables=GRILL_DATABASE_TABLES,
                )
            except SQLiteComponentSchemaError as exc:
                if exc.code == "SQLITE_COMPONENT_SCHEMA_UNSUPPORTED":
                    raise RuntimeError(
                        "unsupported Grill database schema version: "
                        f"{exc}"
                    ) from exc
                raise
            try:
                execute_sqlite_script_in_transaction(
                    connection,
                    SCHEMA_SQL,
                )
                connection.execute(
                    """
                    INSERT INTO grill_meta(key, value)
                    VALUES('schema_version', ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (str(GRILL_DATABASE_SCHEMA_VERSION),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        self._initialize()
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection | None]:
        if not self.exists:
            yield None
            return
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _host_key(host_session_id: str) -> str:
        return str(host_session_id or "__anonymous__")

    @staticmethod
    def _project_key(project_root: Path | str) -> str:
        return str(Path(project_root).expanduser().resolve(strict=False))

    @staticmethod
    def _request_hash(
        *,
        project_root: str,
        prompt: str,
        task_family: str,
        continuation: bool,
    ) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "project_root": project_root,
                    "prompt": prompt,
                    "task_family": task_family,
                    "continuation": continuation,
                }
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _decode_state(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            value = json.loads(str(row["state_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored Grill session state is invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("stored Grill session state must be an object")
        state_revision = _require_revision(
            value.get("revision"),
            "stored Grill session state revision",
        )
        database_revision = _require_revision(
            row["revision"],
            "stored Grill session database revision",
        )
        if state_revision != database_revision:
            raise RuntimeError(
                "stored Grill session revision does not match database revision"
            )
        return value

    @staticmethod
    def _find_active_row(
        connection: sqlite3.Connection,
        host_key: str,
        project_key: str,
        now: datetime,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT revision, state_json, expires_at
            FROM grill_sessions
            WHERE host_session_id=? AND project_root=? AND status=?
            ORDER BY updated_at DESC, grill_session_id DESC
            """,
            (host_key, project_key, ACTIVE_STATUS),
        ).fetchall()
        for row in rows:
            try:
                if _parse_utc(str(row["expires_at"])) >= now:
                    return row
            except ValueError:
                continue
        return None

    @staticmethod
    def _latest_reusable_row(
        connection: sqlite3.Connection,
        host_key: str,
        project_key: str,
        task_family: str,
        now: datetime,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT revision, state_json, expires_at
            FROM grill_sessions
            WHERE host_session_id=? AND project_root=? AND task_family=?
              AND status=?
            ORDER BY updated_at DESC, grill_session_id DESC
            """,
            (
                host_key,
                project_key,
                task_family,
                COMPLETED_STATUS,
            ),
        ).fetchall()
        for row in rows:
            try:
                if _parse_utc(str(row["expires_at"])) >= now:
                    return row
            except ValueError:
                continue
        return None

    @staticmethod
    def _save_state(
        connection: sqlite3.Connection,
        state: dict[str, Any],
        *,
        create: bool,
    ) -> None:
        now = utc_now()
        state["updated_at"] = now
        revision = _require_revision(
            state.get("revision"),
            "Grill session state revision",
        )
        encoded = _canonical_json(state)
        if create:
            connection.execute(
                """
                INSERT INTO grill_sessions(
                    grill_session_id, host_session_id, project_root,
                    task_family, status, revision, state_json,
                    created_at, updated_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["grill_session_id"],
                    state["host_session_id"],
                    state["project_root"],
                    state["task_family"],
                    state["status"],
                    revision,
                    encoded,
                    state["created_at"],
                    now,
                    state["expires_at"],
                ),
            )
            return
        connection.execute(
            """
            UPDATE grill_sessions
            SET status=?, revision=?, state_json=?, updated_at=?, expires_at=?
            WHERE grill_session_id=?
            """,
            (
                state["status"],
                revision,
                encoded,
                now,
                state["expires_at"],
                state["grill_session_id"],
            ),
        )

    @staticmethod
    def _new_state(
        *,
        host_key: str,
        project_key: str,
        task_family: str,
        prompt: str,
        contract: dict[str, Any],
        required_fields: Sequence[str],
        max_questions: int,
        ttl_seconds: int,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        created = utc_now()
        digest = hashlib.sha256(
            _canonical_json(
                {
                    "host": host_key,
                    "project": project_key,
                    "family": task_family,
                    "prompt": prompt,
                    "created": created,
                }
            ).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "schema_version": INTENT_SCHEMA_VERSION,
            "grill_session_id": f"grill-{digest}",
            "parent_grill_session_id": parent_session_id,
            "host_session_id": host_key,
            "project_root": project_key,
            "task_family": task_family,
            "status": ACTIVE_STATUS,
            "revision": 1,
            "original_prompt": str(prompt or "").strip(),
            "contract": contract,
            "required_fields": list(required_fields),
            "missing_fields": _missing_fields(contract, required_fields),
            "current_field": None,
            "current_recommendation": None,
            "current_recommendation_rationale": None,
            "question_index": 0,
            "question_limit": int(max_questions),
            "planned_question_total": 0,
            "execution_prompt": None,
            "created_at": created,
            "updated_at": created,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            )
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        }

    @staticmethod
    def _ask_response(state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        missing = list(state["missing_fields"])
        field = missing[0]
        state["current_field"] = field
        state["question_index"] = int(state["question_index"]) + 1
        if not state["planned_question_total"]:
            state["planned_question_total"] = min(
                len(missing),
                int(state["question_limit"]),
            )
        recommendation = _recommended_answer(
            field,
            str(state["task_family"]),
            state["contract"],
        )
        state["current_recommendation"] = recommendation
        state["current_recommendation_rationale"] = _recommendation_rationale(
            field,
            str(state["task_family"]),
        )
        return {
            "action": "ask",
            "reason": reason,
            "owns_turn": True,
            "suppress_plot_receipt": True,
            "suppress_plot_stop_extract": True,
            "grill_session_id": state["grill_session_id"],
            "session_revision": _require_revision(
                state.get("revision"),
                "Grill session state revision",
            ),
            "task_family": state["task_family"],
            "question": {
                "field": field,
                "label": FIELD_LABELS[field],
                "index": state["question_index"],
                "total": max(
                    int(state["question_index"]),
                    int(state["planned_question_total"]),
                ),
                "text": _question(field, str(state["task_family"])),
                "recommended_answer": recommendation,
                "recommendation_rationale": state[
                    "current_recommendation_rationale"
                ],
            },
            "contract": deepcopy(state["contract"]),
        }

    @staticmethod
    def _repeat_response(
        state: dict[str, Any],
        *,
        reason: str,
        inspect_only: bool = False,
    ) -> dict[str, Any]:
        field = str(state["current_field"])
        return {
            "action": "inspect" if inspect_only else "ask",
            "reason": reason,
            "owns_turn": True,
            "suppress_plot_receipt": True,
            "suppress_plot_stop_extract": True,
            "grill_session_id": state["grill_session_id"],
            "session_revision": _require_revision(
                state.get("revision"),
                "Grill session state revision",
            ),
            "task_family": state["task_family"],
            "remaining_questions": len(state.get("missing_fields") or []),
            "question": {
                "field": field,
                "label": FIELD_LABELS[field],
                "index": int(state["question_index"]),
                "total": max(
                    int(state["question_index"]),
                    int(state["planned_question_total"]),
                ),
                "text": _question(field, str(state["task_family"])),
                "recommended_answer": str(
                    state.get("current_recommendation") or ""
                ),
                "recommendation_rationale": str(
                    state.get("current_recommendation_rationale") or ""
                ),
            },
            "contract": deepcopy(state["contract"]),
        }

    @staticmethod
    def _proceed_response(state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        state["status"] = EXECUTING_STATUS
        state["current_field"] = None
        state["current_recommendation"] = None
        state["current_recommendation_rationale"] = None
        state["missing_fields"] = []
        state["execution_prompt"] = _execution_prompt(state)
        return {
            "action": "proceed",
            "reason": reason,
            "owns_turn": False,
            "suppress_plot_receipt": False,
            "suppress_plot_stop_extract": False,
            "grill_session_id": state["grill_session_id"],
            "session_revision": _require_revision(
                state.get("revision"),
                "Grill session state revision",
            ),
            "task_family": state["task_family"],
            "execution_prompt": state["execution_prompt"],
            "contract": deepcopy(state["contract"]),
        }

    @staticmethod
    def _cancel_response(state: dict[str, Any]) -> dict[str, Any]:
        state["status"] = CANCELLED_STATUS
        state["current_field"] = None
        state["current_recommendation"] = None
        state["current_recommendation_rationale"] = None
        return {
            "action": "cancel",
            "reason": "user_cancelled",
            "owns_turn": True,
            "suppress_plot_receipt": True,
            "suppress_plot_stop_extract": True,
            "grill_session_id": state["grill_session_id"],
            "session_revision": _require_revision(
                state.get("revision"),
                "Grill session state revision",
            ),
            "task_family": state["task_family"],
            "contract": deepcopy(state["contract"]),
        }

    @staticmethod
    def _record_turn_response(
        connection: sqlite3.Connection,
        *,
        host_key: str,
        project_key: str,
        turn_id: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        if not turn_id:
            return
        connection.execute(
            """
            INSERT INTO grill_turn_responses(
                host_session_id, project_root, turn_id,
                request_hash, response_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                host_key,
                project_key,
                turn_id,
                request_hash,
                _canonical_json(response),
                utc_now(),
            ),
        )

    def process(
        self,
        *,
        project_root: Path | str,
        prompt: str,
        task_family: str,
        host_session_id: str,
        turn_id: str,
        required_fields: Sequence[str] = DEFAULT_REQUIRED_FIELDS,
        max_questions: int = 6,
        ttl_seconds: int = 21600,
        skip_phrases: Sequence[str] = DEFAULT_SKIP_PHRASES,
        cancel_phrases: Sequence[str] = DEFAULT_CANCEL_PHRASES,
        continuation: bool = False,
    ) -> dict[str, Any]:
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        request_hash = self._request_hash(
            project_root=project_key,
            prompt=prompt,
            task_family=task_family,
            continuation=continuation,
        )
        now = datetime.now(timezone.utc)

        with self._write_connection() as connection:
            if turn_id:
                cached = connection.execute(
                    """
                    SELECT request_hash, response_json
                    FROM grill_turn_responses
                    WHERE host_session_id=? AND project_root=? AND turn_id=?
                    """,
                    (host_key, project_key, turn_id),
                ).fetchone()
                if cached is not None:
                    if str(cached["request_hash"]) == request_hash:
                        value = json.loads(str(cached["response_json"]))
                        if isinstance(value, dict):
                            return value
                    return {
                        "action": "conflict",
                        "reason": "turn_id_request_conflict",
                        "owns_turn": True,
                        "suppress_plot_receipt": True,
                        "suppress_plot_stop_extract": True,
                        "task_family": task_family,
                    }

            active = self._decode_state(
                self._find_active_row(connection, host_key, project_key, now)
            )
            if active is not None:
                if _compact(prompt) in {_compact(value) for value in REPEAT_PHRASES}:
                    response = self._repeat_response(
                        active,
                        reason="repeat_current_question",
                    )
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response

                if _contains_phrase(prompt, INSPECT_PHRASES):
                    response = self._repeat_response(
                        active,
                        reason="inspect_active_grill",
                        inspect_only=True,
                    )
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response

                if _contains_phrase(prompt, cancel_phrases):
                    _advance_revision(active)
                    response = self._cancel_response(active)
                    self._save_state(connection, active, create=False)
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response

                if _contains_phrase(prompt, skip_phrases):
                    _fill_defaults(
                        active["contract"],
                        str(active["task_family"]),
                        active["missing_fields"],
                        source="grill_skip_default",
                    )
                    _advance_revision(active)
                    response = self._proceed_response(active, reason="explicit_skip")
                    self._save_state(connection, active, create=False)
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response

                current_field = str(active.get("current_field") or "")
                if current_field not in FIELD_ORDER:
                    active["missing_fields"] = _missing_fields(
                        active["contract"],
                        active["required_fields"],
                    )
                    current_field = active["missing_fields"][0]

                answer = str(prompt or "").strip()
                delegated = _compact(answer) in {
                    "按推荐答案",
                    "就按推荐答案",
                    "采用推荐答案",
                    "用推荐答案",
                    "你来定",
                }
                if delegated:
                    answer = str(active.get("current_recommendation") or "").strip()
                if not answer:
                    response = self._repeat_response(
                        active,
                        reason="empty_answer_repeats_question",
                    )
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response
                active["contract"]["fields"][current_field] = _entry(
                    answer,
                    "recommended_delegation" if delegated else "user_answer",
                )
                active["missing_fields"] = _missing_fields(
                    active["contract"],
                    active["required_fields"],
                )
                _advance_revision(active)

                if (
                    active["missing_fields"]
                    and int(active["question_index"]) < int(active["question_limit"])
                ):
                    response = self._ask_response(active, reason="next_dependency")
                else:
                    _fill_defaults(
                        active["contract"],
                        str(active["task_family"]),
                        active["missing_fields"],
                        source="question_limit_default",
                    )
                    response = self._proceed_response(
                        active,
                        reason=(
                            "contract_complete"
                            if not active["missing_fields"]
                            else "question_limit_reached"
                        ),
                    )
                self._save_state(connection, active, create=False)
                self._record_turn_response(
                    connection,
                    host_key=host_key,
                    project_key=project_key,
                    turn_id=turn_id,
                    request_hash=request_hash,
                    response=response,
                )
                return response

            if continuation:
                reusable = self._decode_state(
                    self._latest_reusable_row(
                        connection,
                        host_key,
                        project_key,
                        task_family,
                        now,
                    )
                )
                if reusable is not None:
                    inherited = self._new_state(
                        host_key=host_key,
                        project_key=project_key,
                        task_family=task_family,
                        prompt=prompt,
                        contract=deepcopy(reusable["contract"]),
                        required_fields=required_fields,
                        max_questions=max_questions,
                        ttl_seconds=ttl_seconds,
                        parent_session_id=str(reusable["grill_session_id"]),
                    )
                    response = self._proceed_response(
                        inherited,
                        reason="inherited_locked_contract",
                    )
                    self._save_state(connection, inherited, create=True)
                    self._record_turn_response(
                        connection,
                        host_key=host_key,
                        project_key=project_key,
                        turn_id=turn_id,
                        request_hash=request_hash,
                        response=response,
                    )
                    return response

            contract = infer_contract(prompt, task_family)
            state = self._new_state(
                host_key=host_key,
                project_key=project_key,
                task_family=task_family,
                prompt=prompt,
                contract=contract,
                required_fields=required_fields,
                max_questions=max_questions,
                ttl_seconds=ttl_seconds,
            )
            if _contains_phrase(prompt, skip_phrases):
                _fill_defaults(
                    state["contract"],
                    task_family,
                    state["missing_fields"],
                    source="grill_skip_default",
                )
                response = self._proceed_response(state, reason="explicit_skip")
            elif _quick_path_ready(state["contract"], required_fields):
                _fill_defaults(
                    state["contract"],
                    task_family,
                    state["missing_fields"],
                    source="quick_path_default",
                )
                response = self._proceed_response(state, reason="intent_already_clear")
            else:
                response = self._ask_response(state, reason="intent_incomplete")
            self._save_state(connection, state, create=True)
            self._record_turn_response(
                connection,
                host_key=host_key,
                project_key=project_key,
                turn_id=turn_id,
                request_hash=request_hash,
                response=response,
            )
            return response

    def active(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
    ) -> dict[str, Any] | None:
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._read_connection() as connection:
            if connection is None:
                return None
            return self._decode_state(
                self._find_active_row(
                    connection,
                    host_key,
                    project_key,
                    datetime.now(timezone.utc),
                )
            )

    def turn_response(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        turn_id: str,
        prompt: str | None = None,
        task_family: str | None = None,
        continuation: bool = False,
    ) -> dict[str, Any] | None:
        if not self.exists or not turn_id:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._read_connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                """
                SELECT request_hash, response_json
                FROM grill_turn_responses
                WHERE host_session_id=? AND project_root=? AND turn_id=?
                """,
                (host_key, project_key, turn_id),
            ).fetchone()
            if row is None:
                return None
            if prompt is not None and task_family is not None:
                request_hash = self._request_hash(
                    project_root=project_key,
                    prompt=prompt,
                    task_family=task_family,
                    continuation=continuation,
                )
                if str(row["request_hash"]) != request_hash:
                    return {
                        "action": "conflict",
                        "reason": "turn_id_request_conflict",
                        "owns_turn": True,
                        "suppress_plot_receipt": True,
                        "suppress_plot_stop_extract": True,
                        "task_family": task_family,
                    }
            value = json.loads(str(row["response_json"]))
            return value if isinstance(value, dict) else None

    def should_suppress_stop(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        """Return the current turn decision when Grill owns the assistant output."""

        if not self.exists:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._read_connection() as connection:
            if connection is None:
                return None
            if turn_id:
                row = connection.execute(
                    """
                    SELECT response_json
                    FROM grill_turn_responses
                    WHERE host_session_id=? AND project_root=? AND turn_id=?
                    """,
                    (host_key, project_key, turn_id),
                ).fetchone()
                if row is not None:
                    value = json.loads(str(row["response_json"]))
                    if (
                        isinstance(value, dict)
                        and bool(value.get("suppress_plot_stop_extract"))
                    ):
                        return value
            active = self._decode_state(
                self._find_active_row(
                    connection,
                    host_key,
                    project_key,
                    datetime.now(timezone.utc),
                )
            )
            if active is None:
                return None
            return {
                "action": "ask",
                "reason": "active_grill",
                "grill_session_id": active.get("grill_session_id"),
                "suppress_plot_stop_extract": True,
            }

    def session_state(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        grill_session_id: str,
    ) -> dict[str, Any] | None:
        if not self.exists:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._read_connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                """
                SELECT revision, state_json
                FROM grill_sessions
                WHERE grill_session_id=? AND host_session_id=? AND project_root=?
                """,
                (grill_session_id, host_key, project_key),
            ).fetchone()
            return self._decode_state(row)

    def complete_execution(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        grill_session_id: str = "",
        handoff_turn_id: str = "",
    ) -> dict[str, Any] | None:
        if not self.exists:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._write_connection() as connection:
            if grill_session_id:
                row = connection.execute(
                    """
                    SELECT revision, state_json
                    FROM grill_sessions
                    WHERE grill_session_id=? AND host_session_id=? AND project_root=?
                      AND status IN (?, ?)
                    """,
                    (
                        grill_session_id,
                        host_key,
                        project_key,
                        EXECUTING_STATUS,
                        COMPLETED_STATUS,
                    ),
                ).fetchone()
                state = self._decode_state(row)
            else:
                rows = connection.execute(
                    """
                    SELECT revision, state_json
                    FROM grill_sessions
                    WHERE host_session_id=? AND project_root=? AND status=?
                    ORDER BY updated_at DESC, grill_session_id DESC
                    """,
                    (host_key, project_key, EXECUTING_STATUS),
                ).fetchall()
                state = next(
                    (
                        decoded
                        for decoded in (self._decode_state(row) for row in rows)
                        if decoded is not None
                        and (
                            not handoff_turn_id
                            or str(decoded.get("handoff_turn_id") or "")
                            == str(handoff_turn_id)
                        )
                    ),
                    None,
                )
            if state is None:
                return None
            if (
                handoff_turn_id
                and str(state.get("handoff_turn_id") or "")
                not in {"", str(handoff_turn_id)}
            ):
                raise RuntimeError("Grill handoff turn does not match execution")
            if str(state.get("status") or "") == COMPLETED_STATUS:
                return state
            state["status"] = COMPLETED_STATUS
            _advance_revision(state)
            self._save_state(connection, state, create=False)
            return state

    def mark_prepared(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        grill_session_id: str,
        expected_session_revision: int,
        receipt_id: str,
        prepare_status: str,
        turn_id: str,
        prepare_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Attach one deterministic plot handoff result to its exact contract."""

        if (
            type(expected_session_revision) is not int
            or expected_session_revision < 0
        ):
            raise RuntimeError(
                "expected_session_revision must be a non-negative integer"
            )
        if not self.exists:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT revision, state_json
                FROM grill_sessions
                WHERE grill_session_id=? AND host_session_id=? AND project_root=?
                  AND status IN (?, ?)
                """,
                (
                    grill_session_id,
                    host_key,
                    project_key,
                    EXECUTING_STATUS,
                    COMPLETED_STATUS,
                ),
            ).fetchone()
            state = self._decode_state(row)
            if state is None:
                return None
            normalized_result = (
                deepcopy(dict(prepare_result))
                if isinstance(prepare_result, Mapping)
                else None
            )
            if (
                str(state.get("prepared_receipt_id") or "")
                == str(receipt_id or "")
                and str(state.get("prepare_status") or "")
                == str(prepare_status or "")
                and str(state.get("handoff_turn_id") or "")
                == str(turn_id or "")
                and state.get("prepare_result") == normalized_result
            ):
                return state
            if state["revision"] != expected_session_revision:
                raise RuntimeError(
                    "Grill session revision changed before prepare handoff"
                )
            if str(state.get("status") or "") != EXECUTING_STATUS:
                raise RuntimeError("completed Grill handoff is immutable")
            state["prepared_receipt_id"] = str(receipt_id or "")
            state["prepare_status"] = str(prepare_status or "")
            state["handoff_turn_id"] = str(turn_id or "")
            state["prepare_result"] = normalized_result
            _advance_revision(state)
            self._save_state(connection, state, create=False)
            return state

    def fail_handoff(
        self,
        *,
        project_root: Path | str,
        host_session_id: str,
        grill_session_id: str,
        turn_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """Persist a Stop-suppressing terminal result for an unsafe handoff."""

        if not self.exists:
            return None
        project_key = self._project_key(project_root)
        host_key = self._host_key(host_session_id)
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT revision, state_json
                FROM grill_sessions
                WHERE grill_session_id=? AND host_session_id=? AND project_root=?
                """,
                (grill_session_id, host_key, project_key),
            ).fetchone()
            state = self._decode_state(row)
            if state is None:
                return None
            existing_status = str(state.get("status") or "")
            existing_turn_id = str(state.get("handoff_turn_id") or "")
            existing_reason = str(state.get("handoff_error") or "")
            if existing_status == HANDOFF_FAILED_STATUS:
                if (
                    existing_turn_id != str(turn_id or "")
                    or existing_reason != str(reason or "unknown")
                ):
                    raise RuntimeError("failed Grill handoff is immutable")
                return {
                    "action": "conflict",
                    "reason": f"handoff_persistence_failed: {existing_reason}",
                    "owns_turn": True,
                    "suppress_plot_receipt": True,
                    "suppress_plot_stop_extract": True,
                    "grill_session_id": grill_session_id,
                    "session_revision": _require_revision(
                        state.get("revision"),
                        "Grill session state revision",
                    ),
                    "task_family": state["task_family"],
                }
            if existing_status != EXECUTING_STATUS:
                raise RuntimeError(
                    "only an executing Grill session can fail its handoff"
                )
            state["status"] = HANDOFF_FAILED_STATUS
            state["handoff_turn_id"] = str(turn_id or "")
            state["handoff_error"] = str(reason or "unknown")
            _advance_revision(state)
            self._save_state(connection, state, create=False)
            response = {
                "action": "conflict",
                "reason": f"handoff_persistence_failed: {reason}",
                "owns_turn": True,
                "suppress_plot_receipt": True,
                "suppress_plot_stop_extract": True,
                "grill_session_id": grill_session_id,
                "session_revision": _require_revision(
                    state.get("revision"),
                    "Grill session state revision",
                ),
                "task_family": state["task_family"],
            }
            if turn_id:
                connection.execute(
                    """
                    UPDATE grill_turn_responses
                    SET response_json=?
                    WHERE host_session_id=? AND project_root=? AND turn_id=?
                    """,
                    (
                        _canonical_json(response),
                        host_key,
                        project_key,
                        turn_id,
                    ),
                )
            return response
