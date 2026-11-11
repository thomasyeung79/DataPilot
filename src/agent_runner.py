"""Alpha 服务层状态机：显式授权、有限调用、幂等命令和本地审计。

不创建真实 Provider，不读取环境变量，不连接 Streamlit。调用方必须注入未包裹的
Provider 及与原有 AI 功能共享的 RequestBudget；只有明确命令会产生副作用。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import copy
import hashlib
import threading
import time
from types import MappingProxyType
import uuid

from src.ai_analysis import (AIAnalysisError, GENERATION_PROMPT, build_generation_payload,
                             encode_json, generate_sql,
                             validate_proposal_sql)
from src.agent_planner import parse_plan, planning_messages
from src.agent_report import grounded_report, local_facts, report_packet
from src import agent_tools
from src.llm_provider import BudgetedProvider, CallMetrics, LLMError, RequestBudget, _http_error
from src.sql_analysis import SQLAnalysisError, safe_ast_metadata


class AgentError(ValueError):
    """可以展示的固定中文状态或授权错误，不包含第三方异常正文。"""

    def __init__(self, message, *, category="AGENT_STATE_INVALID"):
        super().__init__(message)
        self.category = category


def _safe_model_error(error):
    """只复用固定 HTTP 提示或已知错误类别，绝不拼接任意异常正文。"""
    if type(error.status_code) is int and 100 <= error.status_code <= 599:
        if error.status_code != 200:
            return str(_http_error(error.status_code)) + " 任务已停止。"
    categories = {
        "CONNECT_TIMEOUT": "连接模型服务超时", "READ_TIMEOUT": "读取模型响应超时",
        "NETWORK_TIMEOUT": "模型网络请求超时", "REQUEST_TIMEOUT": "模型服务请求超时",
        "NETWORK_ERROR": "无法连接模型服务", "EMPTY_RESPONSE": "模型返回空白内容",
        "PROVIDER_ERROR": "模型服务返回错误响应",
        "TASK_REQUEST_LIMIT": "任务模型请求次数达到六次上限",
        "SESSION_REQUEST_LIMIT": "共享会话模型请求预算已耗尽",
        "MODEL_JSON_INVALID": "模型内容不是有效JSON", "RESPONSE_FORMAT_INVALID": "模型响应封装格式异常",
    }
    if isinstance(error.category, str) and error.category in categories:
        return f"{error.category}：{categories[error.category]}，任务已停止。"
    return "模型服务调用失败，任务已停止。"


class AgentState(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    STEP_COMPLETED = "STEP_COMPLETED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_NORMAL_TRANSITIONS = {
    AgentState.CREATED: {AgentState.PLANNED},
    AgentState.PLANNED: {AgentState.AWAITING_APPROVAL},
    AgentState.AWAITING_APPROVAL: {AgentState.PLANNED, AgentState.RUNNING},
    AgentState.RUNNING: {AgentState.STEP_COMPLETED},
    AgentState.STEP_COMPLETED: {AgentState.AWAITING_APPROVAL, AgentState.COMPLETED},
    AgentState.COMPLETED: set(), AgentState.FAILED: set(), AgentState.CANCELLED: set(),
}
TRANSITIONS = MappingProxyType({
    state: frozenset(targets | ({AgentState.FAILED} if state not in
                               {AgentState.FAILED, AgentState.CANCELLED} else set())
                    | ({AgentState.CANCELLED} if state != AgentState.CANCELLED else set()))
    for state, targets in _NORMAL_TRANSITIONS.items()
})


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    kind: str
    state: AgentState
    step: int | None
    fingerprint: str | None = None
    timestamp: str = ""
    stage: str | None = None
    category: str | None = None
    sql_submitted: bool | None = None
    model_requests_consumed: int | None = None
    ast_node: str | None = None
    ast_reason: str | None = None


@dataclass(frozen=True)
class FailureDetail:
    step: int | None
    stage: str
    category: str
    sql_submitted: bool
    model_requests_consumed: int
    ast_node: str | None = None
    ast_reason: str | None = None


@dataclass(frozen=True)
class ModelCall:
    elapsed_seconds: float
    usage: dict[str, int] | None
    success: bool


@dataclass(frozen=True)
class AgentSnapshot:
    task_id: str
    state: AgentState
    goal: str
    steps: tuple
    approval: str | None
    proposal: object
    proposal_id: str | None
    results: tuple
    sql_history: tuple[str, ...]
    facts: tuple
    report: object
    failure: str | None
    calls: tuple[ModelCall, ...]
    session_requests: int
    session_limit: int
    audit: tuple[AuditEvent, ...]
    failure_detail: FailureDetail | None = None


class AgentRunner:
    MAX_CALLS = 6

    def __init__(self, frame, dataset_id: str, goal: str, provider, session_budget: RequestBudget):
        if not isinstance(dataset_id, str) or not dataset_id:
            raise AgentError("必须提供当前数据集标识。")
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 1200:
            raise AgentError("分析目标须为一至1200字符。")
        if isinstance(provider, BudgetedProvider):
            raise AgentError("请注入原始 Provider 和共享预算，避免重复计数。")
        self._frame = frame.copy(deep=True)
        self._dataset_id = dataset_id
        self._goal = goal.strip()
        self._provider, self._budget = provider, session_budget
        self._id = uuid.uuid4().hex
        self._state = AgentState.CREATED
        self._steps, self._results, self._calls, self._audit = [], [], [], []
        self._sql_history = []
        self._model = _TaskProvider(self)
        self._approval = self._proposal = self._proposal_id = self._report = self._failure = None
        self._plan_approved = False
        self._approved_plan_digest = None
        self._failure_detail = None
        self._operation_step = None
        self._phase = "任务创建"
        self._sql_submitted = False
        self._commands = {}
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._event("task_created")

    def _event(self, kind, fingerprint=None, detail=None):
        self._audit.append(AuditEvent(len(self._audit) + 1, kind, self._state,
                                     self._operation_step,
                                     fingerprint, datetime.now(timezone.utc).isoformat(),
                                     detail.stage if detail else self._phase,
                                     detail.category if detail else None,
                                     detail.sql_submitted if detail else None,
                                     detail.model_requests_consumed if detail else None,
                                     detail.ast_node if detail else None,
                                     detail.ast_reason if detail else None))

    def _transition(self, state):
        if state not in TRANSITIONS[self._state]:
            raise AgentError("不允许此状态变化。", category="AGENT_STATE_TRANSITION")
        self._state = state
        self._event("state_changed")

    def _stop(self, state, message):
        self._check(state in {AgentState.FAILED, AgentState.CANCELLED}, "无效的终止状态。")
        self._transition(state)
        self._failure, self._approval = message, None
        self._event("task_stopped")

    def snapshot(self):
        """返回深拷贝；页面不能通过修改快照篡改 SQL、结果或审批。"""
        with self._lock:
            return copy.deepcopy(AgentSnapshot(
                self._id, self._state, self._goal, tuple(self._steps), self._approval,
                self._proposal, self._proposal_id, tuple(self._results), tuple(self._sql_history),
                local_facts(tuple(self._results)), self._report, self._failure,
                tuple(self._calls), self._budget.used, self._budget.limit, tuple(self._audit),
                self._failure_detail))

    def _check(self, condition, message="当前状态不允许此操作。"):
        if not condition:
            raise AgentError(message)

    def _dataset(self, dataset_id):
        if dataset_id != self._dataset_id:
            self._cancel.set()
            if self._state != AgentState.CANCELLED:
                self._stop(AgentState.CANCELLED, "数据集已变化，旧任务已失效。")
            raise AgentError("数据集已变化，请创建新任务。")

    def _active(self):
        if self._cancel.is_set():
            if self._state != AgentState.CANCELLED:
                self._stop(AgentState.CANCELLED, "任务已取消。")
            raise AgentError("任务已取消。")

    def _command(self, name, command_id, dataset_id, arguments, action):
        """同一命令标识只能绑定同一内容；重复提交不再次请求或执行。"""
        with self._lock:
            self._dataset(dataset_id)
            self._active()
            self._check(isinstance(command_id, str) and 1 <= len(command_id) <= 128,
                        "必须提供一至128字符的幂等命令标识。")
            signature = hashlib.sha256(encode_json([name, arguments]).encode()).hexdigest()
            if command_id in self._commands:
                previous, error = self._commands[command_id]
                self._check(previous == signature, "命令标识已被其他操作使用。")
                if error:
                    raise AgentError(error)
                return self.snapshot()
            self._operation_step = len(self._results) + 1 if name in {"generate_step", "execute_step"} else None
            self._phase = {"plan": "规划生成", "generate_step": "SQL生成",
                           "execute_step": "SQL执行", "approve_plan": "计划审批",
                           "summarize": "结果报告"}.get(name, "状态校验")
            self._sql_submitted = False
            calls_before = len(self._calls)
            self._event("command_started", signature)
            # 仅状态错误和缺少确认不改变任务；模型、解析或查询失败则终止。
            try:
                action()
                self._active()
            except AgentError as exc:
                if self._state not in {AgentState.FAILED, AgentState.CANCELLED}:
                    self._failure_detail = FailureDetail(self._operation_step, "状态校验",
                                                         exc.category, self._sql_submitted,
                                                         len(self._calls) - calls_before)
                    self._event("command_rejected", signature, self._failure_detail)
                    if exc.category in {"AGENT_STATE_TRANSITION", "APPROVAL_INVALIDATED"}:
                        message = ("审批绑定的 SQL 或计划已变化，请新建任务并重新审核。"
                                   if exc.category == "APPROVAL_INVALIDATED"
                                   else "Agent 状态转换失败，任务已停止。")
                        self._stop(AgentState.FAILED, message)
                raise
            except Exception as exc:
                if self._cancel.is_set():
                    self._active()
                message = (("本地 SQL 查询超时，任务已停止。" if "超时" in str(exc)
                            else "本地 SQL 查询失败或资源受限，任务已停止。")
                           if isinstance(exc, SQLAnalysisError)
                           else "模型或分析输出未通过校验，任务已停止。"
                           if isinstance(exc, AIAnalysisError)
                           else _safe_model_error(exc)
                           if isinstance(exc, LLMError)
                           else "分析任务发生异常，已安全停止。")
                self._failure_detail = self._diagnose(exc, len(self._calls) - calls_before)
                self._stop(AgentState.FAILED, message)
                self._event("command_failed", signature, self._failure_detail)
                self._commands[command_id] = (signature, message)
                raise AgentError(message) from None
            self._commands[command_id] = (signature, None)
            self._failure_detail = None
            self._event(name, signature)
            return self.snapshot()

    def _diagnose(self, error, consumed):
        """固定类别，不根据模型正文推断原因，也不把异常文字写进审计。"""
        category, stage = "ANALYSIS_VALIDATION_FAILED", self._phase
        known = {
            "PLANNING_JSON_INVALID": "规划JSON解析", "PLANNING_STEPS_INVALID": "规划步骤校验",
            "PLAN_STEP_COUNT_MISMATCH": "规划步骤校验", "PLANNING_CONSTRAINT_INVALID": "规划约束校验",
            "PLAN_NON_SQL_STEP": "规划步骤校验",
            "SQL_GENERATION_FORMAT": "SQL生成格式校验", "SQL_AST_REJECTED": "SQL安全校验",
            "SQL_PARSE_ERROR": "SQL安全校验", "SQL_TABLE_ERROR": "SQL表名校验",
            "SQL_FIELD_ERROR": "SQL字段校验", "SQL_CLARIFICATION_REQUIRED": "SQL生成澄清",
        }
        code = getattr(error, "category", None)
        if isinstance(code, str) and code in known:
            category, stage = code, known[code]
        elif isinstance(error, LLMError) and code == "MODEL_JSON_INVALID":
            category, stage = (("PLANNING_JSON_INVALID", "规划JSON解析") if self._phase == "规划生成"
                               else ("SQL_GENERATION_FORMAT", "SQL生成格式校验") if self._phase == "SQL生成"
                               else ("REPORT_FORMAT_INVALID", "结果报告校验"))
        elif isinstance(error, LLMError):
            category = "MODEL_REQUEST_FAILED"
            allowed = {"CONNECT_TIMEOUT", "READ_TIMEOUT", "NETWORK_TIMEOUT", "REQUEST_TIMEOUT",
                       "NETWORK_ERROR", "EMPTY_RESPONSE", "PROVIDER_ERROR", "TASK_REQUEST_LIMIT",
                       "SESSION_REQUEST_LIMIT", "RESPONSE_FORMAT_INVALID"}
            if type(error.status_code) is int and 100 <= error.status_code <= 599 and error.status_code != 200:
                category = _http_error(error.status_code).category
            elif isinstance(code, str) and code in allowed:
                category = code
            if category in {"TASK_REQUEST_LIMIT", "SESSION_REQUEST_LIMIT"}:
                stage = "模型请求预算校验"
        elif isinstance(error, SQLAnalysisError):
            category = "SQL_EXECUTION_TIMEOUT" if "超时" in str(error) else "SQL_EXECUTION_FAILED"
            stage = "SQL执行"
        elif code == "JSON_INVALID":
            category = "SQL_GENERATION_FORMAT" if self._phase == "SQL生成" else "REPORT_FORMAT_INVALID"
            stage = "SQL生成格式校验" if self._phase == "SQL生成" else "结果报告校验"
        elif not isinstance(error, AIAnalysisError):
            category = "UNEXPECTED_ERROR"
        node, reason = safe_ast_metadata(getattr(error, "ast_node", None), getattr(error, "ast_reason", None))
        return FailureDetail(self._operation_step, stage, category, self._sql_submitted, consumed, node, reason)

    def _complete_json(self, messages):
        """仅供受控服务内部调用；同时占用任务及原有会话预算。"""
        self._active()
        if len(self._calls) >= self.MAX_CALLS:
            raise LLMError("任务模型请求次数达到六次上限。", category="TASK_REQUEST_LIMIT")
        try:
            self._budget.claim()
        except LLMError:
            raise LLMError("共享会话模型请求预算已耗尽。", category="SESSION_REQUEST_LIMIT") from None
        started, success = time.monotonic(), False
        previous_metrics = getattr(self._provider, "last_metrics", None)
        try:
            text = self._provider.complete_json(messages)
            self._active()
            success = True
            return text
        finally:
            metrics = getattr(self._provider, "last_metrics", None)
            usage = (copy.deepcopy(metrics.usage) if isinstance(metrics, CallMetrics)
                     and (success or metrics is not previous_metrics) else None)
            self._calls.append(ModelCall(time.monotonic() - started, usage, success))
            self._event("model_call")

    def preview_plan(self, dataset_id):
        with self._lock:
            self._dataset(dataset_id)
            self._active()
            return planning_messages(self._frame, self._goal)

    def plan(self, *, dataset_id, command_id, confirmed=False):
        def action():
            self._check(confirmed is True, "规划前必须确认外部发送。")
            self._check(self._state == AgentState.CREATED)
            self._steps = list(parse_plan(self._complete_json(self.preview_plan(dataset_id)), self._goal))
            self._transition(AgentState.PLANNED)
            self._approval = "plan"
            self._transition(AgentState.AWAITING_APPROVAL)
        return self._command("plan", command_id, dataset_id, [confirmed], action)

    def approve_plan(self, *, dataset_id, command_id, confirmed=False):
        def action():
            self._check(self._state == AgentState.AWAITING_APPROVAL and self._approval == "plan")
            self._plan_approved, self._approval = True, None
            self._approved_plan_digest = self._plan_digest()
            self._transition(AgentState.PLANNED)
        # 过期拒绝不能取消已批准、已完成或失败的任务；校验与取消持同一把锁。
        if confirmed is not True:
            with self._lock:
                self._check(self._state == AgentState.AWAITING_APPROVAL and self._approval == "plan")
                return self.cancel(dataset_id=dataset_id)
        return self._command("approve_plan", command_id, dataset_id, [confirmed], action)

    def generate_step(self, *, dataset_id, command_id, confirmed=False):
        def action():
            self._check(confirmed is True, "生成 SQL 前必须确认外部发送。")
            self._check(self._plan_approved and self._state in
                        {AgentState.PLANNED, AgentState.STEP_COMPLETED})
            self._check_approved_plan()
            self._check(len(self._results) < len(self._steps))
            question = self._step_question()
            proposal = generate_sql(self._frame, question, self._model, confirmed=True)
            if proposal.sql is None:
                raise AIAnalysisError("业务口径需要澄清，请修改目标后新建任务。",
                                      category="SQL_CLARIFICATION_REQUIRED")
            self._proposal = proposal
            self._proposal_id = self._proposal_fingerprint()
            self._approval = "sql"
            self._transition(AgentState.AWAITING_APPROVAL)
        return self._command("generate_step", command_id, dataset_id, [confirmed], action)

    def execute_step(self, *, dataset_id, command_id, proposal_id, confirmed=False):
        def action():
            self._check(confirmed is True, "每条 SQL 必须由用户单独确认。")
            self._check(self._state == AgentState.AWAITING_APPROVAL and self._approval == "sql")
            self._check(proposal_id == self._proposal_id, "SQL 审批已失效，请重新审核。")
            self._check_approved_plan()
            if self._proposal_id != self._proposal_fingerprint():
                raise AgentError("候选 SQL 已变化，旧审批不能使用。", category="APPROVAL_INVALIDATED")
            self._phase = "SQL安全校验"
            validate_proposal_sql(self._proposal.sql, self._frame)
            self._transition(AgentState.RUNNING)
            self._phase = "SQL执行"
            self._sql_submitted = True
            result = agent_tools.run_safe_sql(self._frame, self._proposal, confirmed=True)
            self._active()
            self._results.append(result)
            self._sql_history.append(self._proposal.sql)
            self._approval = self._proposal = self._proposal_id = None
            self._transition(AgentState.STEP_COMPLETED)
            if len(self._results) == len(self._steps):
                self._transition(AgentState.COMPLETED)
        return self._command("execute_step", command_id, dataset_id,
                             [proposal_id, confirmed], action)

    def cancel(self, *, dataset_id):
        # 请求可在耗时调用期间设置；正在执行的 SQL 仍由原执行器超时回收。
        self._cancel.set()
        with self._lock:
            self._dataset(dataset_id)
            if self._state != AgentState.CANCELLED:
                self._stop(AgentState.CANCELLED, "任务已由用户取消。")
            return self.snapshot()

    def preview_report(self, *, dataset_id):
        with self._lock:
            self._dataset(dataset_id)
            self._active()
            self._check(self._state == AgentState.COMPLETED)
            return report_packet(tuple(self._results))

    def summarize(self, *, dataset_id, command_id, preview_json, confirmed=False):
        def action():
            self._check(confirmed is True, "解释结果前必须单独确认外部发送。")
            self._check(self._state == AgentState.COMPLETED and self._report is None)
            packet = self.preview_report(dataset_id=dataset_id)
            self._check(preview_json == packet.json_text, "预览已变化，请重新审核发送内容。")
            explanation = agent_tools.summarize_results(packet, self._model, confirmed=True)
            self._report = grounded_report(packet, explanation)
        return self._command("summarize", command_id, dataset_id,
                             [preview_json, confirmed], action)

    def _step_question(self):
        step = self._steps[len(self._results)]
        return f"用户目标：{self._goal}\n已批准的当前步骤：{step.title}\n分析问题：{step.question}"

    def _plan_digest(self):
        content = [self._goal, self._dataset_id, [[step.title, step.question] for step in self._steps]]
        return hashlib.sha256(encode_json(content).encode("utf-8")).hexdigest()

    def _check_approved_plan(self):
        if not self._plan_approved or self._approved_plan_digest != self._plan_digest():
            raise AgentError("完整计划已变化，旧审批不能使用。", category="APPROVAL_INVALIDATED")

    def _proposal_fingerprint(self):
        content = [self._id, len(self._results), self._plan_digest(), self._proposal.sql]
        return hashlib.sha256(encode_json(content).encode("utf-8")).hexdigest()

    def preview_step(self, *, dataset_id):
        with self._lock:
            self._dataset(dataset_id)
            self._active()
            self._check(self._plan_approved and self._state in
                        {AgentState.PLANNED, AgentState.STEP_COMPLETED})
            payload = build_generation_payload(self._frame, self._step_question())
            return [{"role": "system", "content": GENERATION_PROMPT},
                    {"role": "user", "content": encode_json(payload)}]

    def inspect_current_schema(self, *, dataset_id):
        with self._lock:
            self._dataset(dataset_id)
            self._active()
            return agent_tools.inspect_schema(self._frame)


class _TaskProvider:
    """仅提供给内部生成/解释函数，不暴露自动工具或 SQL 执行能力。"""

    def __init__(self, runner):
        self._runner = runner

    def complete_json(self, messages):
        return self._runner._complete_json(messages)
