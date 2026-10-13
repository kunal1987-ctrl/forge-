"""Comprehensive test suite for FORGE Advanced AI Orchestration Pillars:
1. Memory Optimization & Rolling Summarization (summarizer.py)
2. Error Classification Router (error_classifier.py)
3. Multi-Agent Specialization & Strict Contracts (schemas.py, nodes.py)
4. Collaborative Governance & Risk Scoring (impact_analyzer.py)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from services.api.forge.execution.impact_analyzer import (
    ImpactAnalysisResult,
    ImpactAnalyzer,
    RiskScore,
)
from services.api.forge.orchestration.error_classifier import (
    ErrorCategory,
    ErrorClassifier,
)
from services.api.forge.orchestration.graph import verify_router
from services.api.forge.orchestration.nodes import (
    implementer_node,
    investigator_node,
    prune_state_node,
    reviewer_node,
)
from services.api.forge.orchestration.schemas import (
    ImplementationPlan,
)
from services.api.forge.orchestration.state import AgentState
from services.api.forge.orchestration.summarizer import (
    ArtifactSink,
    RollingSummarizer,
    RollingSummary,
)


# ===========================================================================
# PILLAR 1: MEMORY OPTIMIZATION & ROLLING SUMMARIZATION
# ===========================================================================


def test_artifact_sink_offloading():
    """Verify bulky pytest output and duplicate RAG chunks offload to ArtifactSink."""
    task_id = "task-mem-1"
    huge_output = (
        "PASSED test_1\n" * 200 + "FAILED test_fail\nE   assert 1 == 2\n" + "TRACEBACK\n" * 100
    )
    test_results = {"exit_code": 1, "raw_output": huge_output, "stdout": "...", "stderr": "Error"}
    rag_chunks = [f"### [PRIMARY TARGET]: file_{i}.py::sym_{i}\ncode_{i}" for i in range(15)]

    offloaded = ArtifactSink.offload_test_and_rag_artifacts(
        task_id=task_id,
        test_results=test_results,
        context_chunks=rag_chunks,
        max_inline_chars=1000,
    )

    # State must only contain lightweight references
    art_id = offloaded["artifact_id"]
    assert art_id
    assert "test_output_artifact_id" in offloaded["artifact_refs"]
    assert "rag_chunks_artifact_id" in offloaded["artifact_refs"]
    assert len(offloaded["test_results"]["raw_output"]) < len(huge_output)
    assert "[OFFLOADED to artifact:" in offloaded["test_results"]["raw_output"]

    # Raw artifact must be retrievable from table
    entry = ArtifactSink.get_artifact(art_id)
    assert entry is not None
    assert entry.artifact_type == "pytest_raw_output"
    assert huge_output in str(entry.content)


@pytest.mark.asyncio
async def test_rolling_summarizer_condensation_on_retries():
    """Verify RollingSummarizer activates when retry >= 2, emits RemoveMessages and RollingSummary."""
    messages = [
        SystemMessage(content="System prompt", id="sys-1"),
        HumanMessage(content="User prompt", id="human-1"),
        AIMessage(content="Attempt 1 patch", id="ai-1"),
        AIMessage(content="Attempt 2 patch", id="ai-2"),
    ]
    repair_trajectory = [
        {
            "attempt": 1,
            "failed_file": "services/calc.py",
            "failure_type": "ImportError",
            "summary": "Missing pkg",
        },
        {
            "attempt": 2,
            "failed_file": "services/calc.py",
            "failure_type": "TypeError",
            "summary": "Bad type",
        },
    ]

    assert RollingSummarizer.should_summarize(messages, retry_count=2) is True

    # Fast LLM mock returning structured json
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content='{"summary_text": "Attempt 1 failed due to missing import; Attempt 2 fixed import but had type error.", "failed_attempts_count": 2}'
        )
    )

    with patch("services.api.forge.orchestration.summarizer.get_llm", return_value=mock_llm):
        summary_obj, remove_cmds, new_msg = await RollingSummarizer.summarize_history(
            messages=messages,
            retry_count=2,
            repair_trajectory=repair_trajectory,
        )

    assert isinstance(summary_obj, RollingSummary)
    assert summary_obj.failed_attempts_count == 2
    assert "missing import" in summary_obj.summary_text
    # Obsolete messages must receive RemoveMessage commands
    assert len(remove_cmds) == 4
    assert any(isinstance(cmd, RemoveMessage) and cmd.id == "ai-1" for cmd in remove_cmds)
    assert "[ROLLING REPAIR SUMMARY" in str(new_msg.content)


# ===========================================================================
# PILLAR 2: ERROR CLASSIFICATION ENGINE & TARGETED REPAIR
# ===========================================================================


def test_error_classifier_categories():
    """Verify ErrorClassifier correctly identifies all 4 strict categories."""
    # 1. SYNTAX_ERROR
    syntax_raw = "File 'services/api.py', line 12\n    def foo(\n           ^\nSyntaxError: unexpected EOF while parsing"
    res1 = ErrorClassifier.classify(syntax_raw)
    assert res1.category == ErrorCategory.SYNTAX_ERROR
    assert "services/api.py" in res1.failing_file

    # 2. IMPORT_ERROR
    import_raw = "ImportError: cannot import name 'calculate_discount' from 'services.pricing'"
    res2 = ErrorClassifier.classify(import_raw)
    assert res2.category == ErrorCategory.IMPORT_ERROR
    assert "calculate_discount" in res2.offending_symbol

    # 3. ASSERTION_FAILURE
    assert_raw = "FAILED tests/test_calc.py::test_div\nE   assert 10 / 2 == 6\nE   AssertionError: assert 5.0 == 6"
    res3 = ErrorClassifier.classify(assert_raw)
    assert res3.category == ErrorCategory.ASSERTION_FAILURE
    assert "assert 5.0 == 6" in res3.mismatch_snippet

    # 4. TIMEOUT
    timeout_raw = "Command 'pytest' timed out after 30 seconds"
    res4 = ErrorClassifier.classify(timeout_raw)
    assert res4.category == ErrorCategory.TIMEOUT


def test_verify_router_sets_error_classification():
    """Verify verify_router inspects failure output and sets state['error_classification']."""
    state: AgentState = {
        "test_results": {
            "exit_code": 1,
            "passed_count": 0,
            "raw_output": "ModuleNotFoundError: No module named 'scipy'",
        },
        "retry_count": 1,
    }
    dest = verify_router(state)
    assert dest == "prune_state_node"
    assert state.get("error_classification") == "IMPORT_ERROR"
    assert state.get("error_details", {}).get("category") == "IMPORT_ERROR"


@pytest.mark.asyncio
async def test_implementer_node_targeted_repair_prompting(tmp_path):
    """Verify implementer_node dynamically injects targeted guidance for IMPORT_ERROR and ASSERTION_FAILURE."""
    worktree = tmp_path / "worktree_imp"
    worktree.mkdir()
    (worktree / "main.py").write_text("def run(): return 1\n", encoding="utf-8")

    state_import: AgentState = {
        "task_id": "task-imp-1",
        "worktree_path": str(worktree),
        "prompt": "Fix module import issue",
        "error_classification": "IMPORT_ERROR",
        "error_details": {"mismatch_snippet": "No module named 'helper'"},
        "status": "implementing",
        "mode": "fast",
    }

    captured_system_prompt = ""

    async def mock_invoke(messages):
        nonlocal captured_system_prompt
        for m in messages:
            if isinstance(m, SystemMessage):
                captured_system_prompt = m.content
        return AIMessage(
            content="### File: main.py\n<<<<<<< SEARCH\ndef run(): return 1\n=======\ndef run(): return 2\n>>>>>>> REPLACE"
        )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=mock_invoke)

    with (
        patch("services.api.forge.orchestration.nodes.get_llm", return_value=mock_llm),
        patch("services.api.core.config.settings.OPENAI_API_KEY", "mock-key"),
    ):
        _ = await implementer_node(state_import)

    assert "CRITICAL REPAIR STRATEGY (IMPORT_ERROR)" in captured_system_prompt
    assert "Graph-RAG dependency search pass" in captured_system_prompt


# ===========================================================================
# PILLAR 3: MULTI-AGENT SPECIALIZATION & STRICT CONTRACTS
# ===========================================================================


def test_implementation_plan_strict_contract_validation():
    """Verify ImplementationPlan rejects empty target_files, empty steps, or missing test strategy."""
    with pytest.raises(ValueError):
        ImplementationPlan(target_files=[], logic_steps=["step 1"], test_strategy="pytest")

    with pytest.raises(ValueError):
        ImplementationPlan(target_files=["file.py"], logic_steps=[], test_strategy="pytest")

    with pytest.raises(ValueError):
        ImplementationPlan(target_files=["file.py"], logic_steps=["step 1"], test_strategy="")

    # Valid plan
    plan = ImplementationPlan(
        target_files=["services/calc.py"],
        logic_steps=["Fix division"],
        test_strategy="Run unit tests",
    )
    assert plan.target_files == ["services/calc.py"]


@pytest.mark.asyncio
async def test_investigator_contract_generation(tmp_path):
    """Verify investigator_node generates and validates InvestigatorContract handover."""
    state: AgentState = {
        "task_id": "task-inv-1",
        "prompt": "Find multiply function",
        "plan": {
            "target_files": ["services/math.py"],
            "logic_steps": ["Inspect math.py"],
            "test_strategy": "pytest",
        },
        "investigation_step": 0,
        "max_investigation_steps": 1,
        "mode": "fast",
    }

    res = await investigator_node(state)
    assert "investigator_contract" in res
    contract = res["investigator_contract"]
    assert contract["target_files"] == ["services/math.py"]
    assert "is_contract_fulfilled" in contract


# ===========================================================================
# PILLAR 4: COLLABORATIVE GOVERNANCE & RISK SCORING
# ===========================================================================


def test_impact_analyzer_risk_scoring():
    """Verify ImpactAnalyzer computes lines changed, files modified, public APIs, and RiskScore."""
    # Low Risk Diff: Small changes, 1 file, no public API deletion
    low_diff = """
### File: services/utils.py
<<<<<<< SEARCH
    return x + 1
=======
    return x + 2
>>>>>>> REPLACE
"""
    res_low = ImpactAnalyzer.analyze(low_diff)
    assert res_low.risk_score == RiskScore.LOW
    assert "services/utils.py" in res_low.files_modified
    assert res_low.total_lines_changed == 2

    # High Risk Diff: modifying >2 public API signatures or >150 lines
    high_diff = """
--- a/services/api.py
+++ b/services/api.py
@@ -1,15 +1,15 @@
-def public_endpoint_one(a, b):
+def public_endpoint_one(a, b, c=1):
-def public_endpoint_two(x):
+def public_endpoint_two(x, y=2):
-class PublicEngine:
+class PublicEngine(BaseEngine):
"""
    res_high = ImpactAnalyzer.analyze(high_diff)
    assert res_high.risk_score in (RiskScore.MEDIUM, RiskScore.HIGH)
    assert len(res_high.public_api_signatures_altered) >= 2


def test_impact_analyzer_review_payload_generation():
    """Verify generate_review_payload produces rich frontend preview structure."""
    impact = ImpactAnalysisResult(
        lines_added=10,
        lines_removed=5,
        total_lines_changed=15,
        files_modified=["services/api.py"],
        public_api_signatures_altered=["def handle()"],
        risk_score=RiskScore.LOW,
        risk_reasons=["Surgical, contained modification."],
    )
    test_results = {"exit_code": 0, "passed_count": 6, "failed_count": 0}

    payload = ImpactAnalyzer.generate_review_payload(
        diff="patch diff",
        prompt="Fix calculation",
        impact=impact,
        repro_test_code="def test_repro(): pass",
        repro_test_path="tests/_forge_repro_test.py",
        test_results=test_results,
        verification_log="6 passed in 0.2s",
    )

    assert "explanation" in payload
    assert payload["test_modification_confirmed"] is False
    assert payload["repro_test_added"] is True
    assert payload["risk_breakdown"]["risk_score"] == "LOW"
    assert "verification_proof_logs" in payload
    assert "6 passed" in payload["verification_proof_logs"]


@pytest.mark.asyncio
async def test_reviewer_node_interrupt_payload_contains_governance():
    """Verify reviewer_node attaches risk_score and review_payload to interrupt and state."""
    state: AgentState = {
        "task_id": "task-rev-1",
        "generated_diff": "### File: foo.py\n<<<<<<< SEARCH\nfoo=1\n=======\nfoo=2\n>>>>>>> REPLACE",
        "test_results": {"exit_code": 0, "passed_count": 4},
        "repro_test_code": "def test_repro(): pass",
        "prompt": "Fix foo value",
        "mode": "fast",
    }

    mock_interrupt = MagicMock(return_value={"decision": "approve", "feedback": "LGTM"})
    with patch("services.api.forge.orchestration.nodes.interrupt", mock_interrupt):
        res = await reviewer_node(state)

    assert mock_interrupt.called
    interrupt_args = mock_interrupt.call_args[0][0]
    assert "risk_score" in interrupt_args
    assert "review_payload" in interrupt_args
    assert "impact_analysis" in interrupt_args
    assert interrupt_args["review_payload"]["repro_test_added"] is True

    # State output verification
    assert res["status"] == "approved"
    assert "impact_analysis" in res
    assert "review_payload" in res
    assert res["impact_analysis"]["risk_score"] == "LOW"


# ===========================================================================
# ADDITIONAL UNIT TESTS: ROLLING SUMMARIZER & ERROR ROUTER ISOLATION
# ===========================================================================


def test_rolling_summarizer_summarize_messages_mocked_llm():
    """Verify summarize_messages invokes mocked ChatOpenAI, returning summary and RemoveMessages for older turns."""
    from services.api.forge.orchestration.summarizer import RollingSummarizer
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [
        HumanMessage(content="Turn 1: Fix bug", id="msg-1"),
        AIMessage(content="Turn 2: Tried patch 1", id="msg-2"),
        HumanMessage(content="Turn 3: Test failed with ImportError", id="msg-3"),
        AIMessage(content="Turn 4: Tried patch 2", id="msg-4"),
    ]

    mock_response = AIMessage(content="Summary: Previous attempts failed due to missing module.")
    mock_chat = MagicMock()
    mock_chat.invoke.return_value = mock_response

    result = RollingSummarizer.summarize_messages(
        messages,
        existing_summary="Initial attempt failed.",
        llm=mock_chat,
    )

    assert result["summary"] == "Summary: Previous attempts failed due to missing module."
    remove_cmds = result["messages"]
    assert len(remove_cmds) == 2
    assert remove_cmds[0].id == "msg-1"
    assert remove_cmds[1].id == "msg-2"


@pytest.mark.asyncio
async def test_prune_state_node_with_mocked_summarizer():
    """Verify prune_state_node invokes RollingSummarizer and updates state when summarization is needed."""
    from services.api.forge.orchestration.summarizer import RollingSummarizer

    messages = [
        HumanMessage(content="msg " + str(i), id=f"id-{i}")
        for i in range(15)  # len > 12 triggers should_summarize
    ]

    mock_result = {
        "summary": "Rolling summary of 15 execution turns.",
        "messages": [RemoveMessage(id=f"id-{i}") for i in range(13)],
    }

    state: AgentState = {
        "task_id": "task-prune-1",
        "messages": messages,
        "summary": "Old summary",
        "retry_count": 1,
        "test_results": {"exit_code": 1, "raw_output": "AssertionError: expected 1 got 2"},
    }

    with patch.object(RollingSummarizer, "summarize_messages", return_value=mock_result):
        res = await prune_state_node(state)

    assert res["retry_count"] == 2
    assert res["summary"] == "Rolling summary of 15 execution turns."
    assert "messages" in res


def test_error_router_node_routing_categories():
    """Verify error_router_node directs repair loop to correct specialized nodes based on failure category."""
    from services.api.forge.orchestration.nodes import error_router_node

    # 1. IMPORT_ERROR -> graph_rag_dependency_search
    state_import = {
        "test_results": {"raw_output": "ModuleNotFoundError: No module named 'scipy'"},
    }
    assert error_router_node(state_import) == "graph_rag_dependency_search"
    assert state_import["error_classification"] == ErrorCategory.IMPORT_ERROR

    # 2. SYNTAX_ERROR -> implementer_syntax_repair
    state_syntax = {
        "test_results": {"raw_output": "SyntaxError: invalid syntax line 42"},
    }
    assert error_router_node(state_syntax) == "implementer_syntax_repair"
    assert state_syntax["error_classification"] == ErrorCategory.SYNTAX_ERROR

    # 3. ASSERTION_FAILURE -> standard_repair
    state_assert = {
        "test_results": {"raw_output": "AssertionError: 1 != 2"},
    }
    assert error_router_node(state_assert) == "standard_repair"
    assert state_assert["error_classification"] == ErrorCategory.ASSERTION_FAILURE
