"""V4.1：完全离线的报告范围、标量和过期事件回归。"""

from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from src.ai_analysis import (_safe_cell, build_explanation_packet, explain_result,
                             GroundedExplanation, AIAnalysisError)
from src.agent_report import report_packet, grounded_report, readable_report_view, local_summary, local_facts
from src.agent_runner import AgentRunner, AgentState, AgentError
from src.sql_analysis import QueryResult
from src.llm_provider import RequestBudget


def result(values=(1,), truncated=False):
    return QueryResult(pd.DataFrame({"n": list(values)}, dtype=object), truncated)


class ReportSafetyTests(unittest.TestCase):
    def test_all_steps_represented(self):
        packet = report_packet((result(), result((2,))))
        self.assertFalse(packet.partial)

    def test_empty_step_is_explicitly_omitted(self):
        packet = report_packet((result(), result(())))
        self.assertTrue(packet.partial)
        self.assertIn("步骤2", "".join(packet.notes))
        self.assertIn("0行", "".join(packet.notes))
        self.assertIn("未包含", "".join(packet.notes))
        report = grounded_report(packet, GroundedExplanation([], [], []))
        self.assertIn("步骤2", "".join(readable_report_view(report).notes))

    def test_multiple_empty_steps(self):
        packet = report_packet((result(()), result(), result(())))
        self.assertTrue(packet.partial)
        for step in (1, 3):
            self.assertIn(f"步骤{step}", "".join(packet.notes))

    def test_empty_and_truncated(self):
        packet = report_packet((result(range(30), True), result(())))
        self.assertTrue(packet.partial)
        self.assertEqual(packet.sent_rows, 20)
        self.assertIn("步骤2", "".join(packet.notes))

    def test_all_empty_still_has_no_fabricated_cells(self):
        with self.assertRaises(AIAnalysisError):
            report_packet((result(()), result(())))

    def test_scalar_types_and_exact_decimal(self):
        for value in (1, 1.5, True, "text", None, "", 0, np.int64(12), np.float64(2.5)):
            with self.subTest(kind=type(value).__name__):
                encoded, partial = _safe_cell(value)
                self.assertEqual(encoded, value)
                self.assertFalse(partial)
                json.dumps(encoded, allow_nan=False)
        value = Decimal("1.2345678901234567890123456789")
        encoded, partial = _safe_cell(value)
        self.assertEqual(encoded, {"type": "decimal", "value": str(value)})
        self.assertFalse(partial)

    def test_non_scalar_marked_without_content(self):
        for value in (["sensitive", 2], np.array([1, 2]), {"sensitive": 1}):
            with self.subTest(kind=type(value).__name__):
                packet = build_explanation_packet(result((value,)))
                self.assertTrue(packet.partial)
                self.assertNotIn("sensitive", packet.json_text)
                self.assertIn("non_scalar", packet.json_text)
                self.assertTrue(packet.notes)

    def test_nonfinite_null_zero_empty_are_distinct(self):
        packet = build_explanation_packet(result((None, float("nan"), float("inf"), -float("inf"), 0, "")))
        cells = [row[0] for row in json.loads(packet.json_text)["rows"]]
        self.assertIsNone(cells[0])
        self.assertEqual(len({json.dumps(cell) for cell in cells}), 6)
        self.assertTrue(packet.partial)
        self.assertIn("NaN", "".join(local_summary((result((float("nan"),)),)).facts))

    def test_decimal_packet_validates_and_extrema_remain_numeric(self):
        packet = build_explanation_packet(result((Decimal("1.0000000000000000001"), Decimal("1.0000000000000000002"))))
        provider = Mock()
        provider.complete_json.return_value = json.dumps({"facts": [{"kind": "column_max", "row": 1, "column": "n"}], "inferences": [], "suggestions": []})
        explanation = explain_result(packet, provider, confirmed=True)
        self.assertIn("1.0000000000000000002", explanation.facts[0])

    def test_non_scalar_notes_survive_report_and_no_secret_stringification(self):
        packet = report_packet((result((["private-value", 2],)),))
        report = grounded_report(packet, GroundedExplanation([], [], []))
        self.assertTrue(report.partial)
        self.assertNotIn("private-value", str(report))
        self.assertTrue(any("安全替换" in note for note in report.notes))

    def test_unavailable_and_decimal_forged_shapes_blocked_before_provider(self):
        from src.ai_analysis import ExplanationPacket
        for cell in ({"type": "decimal", "value": "Infinity"},
                     {"type": "decimal", "value": "invalid"},
                     {"type": "unavailable", "reason": "private-value"},
                     {"type": "decimal", "value": "1", "extra": "private-value"}):
            with self.subTest(cell=cell):
                provider = Mock()
                text = json.dumps({"columns": ["n"], "rows": [[cell]], "partial": True})
                with self.assertRaises(AIAnalysisError):
                    explain_result(ExplanationPacket(text, 1, True), provider, confirmed=True)
                provider.complete_json.assert_not_called()

    def test_wide_or_byte_limited_packet_scope(self):
        packet = report_packet((result(("长" * 200,) * 20), result((2,))))
        self.assertTrue(packet.partial)
        self.assertLessEqual(len(packet.json_text.encode("utf-8")), 8192)
        self.assertIn("步骤2", "".join(packet.notes))

    def test_numeric_numpy_and_decimal_extremes(self):
        for value in (np.int32(0), np.uint64(2**63 + 1), np.float32(1.5), np.bool_(False)):
            with self.subTest(kind=type(value).__name__):
                encoded, partial = _safe_cell(value)
                self.assertEqual(encoded, value)
                self.assertFalse(partial)
                json.dumps(encoded, allow_nan=False)
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1." + "1" * 201)):
            with self.subTest(value=str(value)[:20]):
                encoded, partial = _safe_cell(value)
                self.assertTrue(partial)
                self.assertEqual(encoded["type"], "unavailable")

    def test_float_column_missing_sentinel_and_object_nan_policy(self):
        packet = build_explanation_packet(QueryResult(pd.DataFrame({"n": [1.5, None]}), False))
        self.assertIsNone(json.loads(packet.json_text)["rows"][1][0])
        self.assertFalse(packet.partial)
        self.assertTrue(build_explanation_packet(result((float("nan"),))).partial)

    def test_preview_serialization_error_is_sanitized(self):
        with patch("src.ai_analysis._safe_cell", side_effect=ValueError("private-cell-body")):
            with self.assertRaises(AIAnalysisError) as raised:
                build_explanation_packet(result())
        self.assertNotIn("private-cell-body", str(raised.exception))

    def test_raw_dictionary_is_not_a_trusted_numeric_tag(self):
        value = {"type": "decimal", "value": "123"}
        self.assertTrue(local_summary((result((value,)),)).partial)
        self.assertTrue(build_explanation_packet(result((value,))).partial)

    def test_null_count_does_not_count_explicit_nonfinite_values(self):
        facts = local_facts((result((None, float("nan"), float("inf"), 0, "")),))
        self.assertEqual(facts[0]["null_cells"], 1)

    def test_cancel_helper_does_not_hide_unexpected_bugs(self):
        from src.agent_ui import _cancel_task
        runner = Mock()
        runner.cancel.side_effect = RuntimeError("programming-error")
        with self.assertRaises(RuntimeError):
            _cancel_task(runner, "d")


class StalePlanTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock(last_metrics=None)
        self.provider.complete_json.return_value = '{"steps":[{"title":"计数","question":"多少条"}]}'
        self.runner = AgentRunner(pd.DataFrame({"n": [1]}), "d", "计数", self.provider, RequestBudget())
        self.runner.plan(dataset_id="d", command_id="plan", confirmed=True)

    def test_normal_rejection(self):
        self.runner.approve_plan(dataset_id="d", command_id="reject", confirmed=False)
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)

    def test_repeated_cancel_is_local_and_idempotent(self):
        calls = self.provider.complete_json.call_count
        for _ in range(2):
            self.runner.cancel(dataset_id="d")
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)
        self.assertEqual(self.provider.complete_json.call_count, calls)

    def test_real_approval_then_stale_rejection_does_not_cancel(self):
        self.runner.approve_plan(dataset_id="d", command_id="approve", confirmed=True)
        with self.assertRaises(AgentError):
            self.runner.approve_plan(dataset_id="d", command_id="reject", confirmed=False)
        self.assertEqual(self.runner.snapshot().state, AgentState.PLANNED)

    def test_terminal_stale_rejection_with_wrong_dataset_preserves_terminal(self):
        self.runner._state = AgentState.COMPLETED
        with self.assertRaises(AgentError):
            self.runner.approve_plan(dataset_id="wrong", command_id="reject", confirmed=False)
        self.assertEqual(self.runner.snapshot().state, AgentState.COMPLETED)

    def test_stale_rejections_preserve_state_and_side_effects(self):
        for state, approval in ((AgentState.PLANNED, None), (AgentState.COMPLETED, None),
                                (AgentState.FAILED, None), (AgentState.CANCELLED, None),
                                (AgentState.AWAITING_APPROVAL, "sql")):
            with self.subTest(state=state):
                self.runner._state, self.runner._approval = state, approval
                before = self.provider.complete_json.call_count
                with patch("src.agent_tools.execute_proposal") as execute:
                    with self.assertRaises(AgentError):
                        self.runner.approve_plan(dataset_id="d", command_id="stale", confirmed=False)
                    execute.assert_not_called()
                self.assertEqual(self.runner.snapshot().state, state)
                self.assertEqual(self.provider.complete_json.call_count, before)

