"""摘要模块的离线测试：不访问 DeepSeek，不写生产数据库。"""

from __future__ import annotations

import json

import pytest

from pinn_radar import summarize
from pinn_radar.models import PaperRecord


def _paper() -> PaperRecord:
    return PaperRecord(
        arxiv_id="2610.00001",
        version=1,
        title="Physics-Informed Neural Networks for Parameter Identification",
        abstract=(
            "We propose a physics-informed neural network method for parameter "
            "identification from sparse observations. Experiments on the Burgers "
            "equation demonstrate the effectiveness of the proposed method."
        ),
        authors=["A. Author"],
        categories=["cs.LG"],
        primary_category="cs.LG",
        published_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
        comment="",
        journal_ref="",
        doi="",
        abs_url="https://arxiv.org/abs/2610.00001",
        pdf_url=None,
        has_abstract=True,
        source_queries=["test"],
        fetched_at="2026-10-31T00:00:00+00:00",
    )


def _payload() -> dict:
    return {
        "core_contribution_zh": "提出一种用于稀疏观测参数辨识的物理信息神经网络方法。",
        "core_contribution_evidence_en": (
            "We propose a physics-informed neural network method for parameter "
            "identification from sparse observations."
        ),
        "methods": [
            {
                "name_zh": "物理信息神经网络",
                "term_en": "physics-informed neural network",
                "evidence_en": (
                    "We propose a physics-informed neural network method for parameter "
                    "identification from sparse observations."
                ),
            }
        ],
        "datasets": [
            {
                "name_zh": "Burgers 方程",
                "term_en": "Burgers equation",
                "evidence_en": (
                    "Experiments on the Burgers equation demonstrate the effectiveness "
                    "of the proposed method."
                ),
            }
        ],
        "code": {"status": "not_found", "urls": [], "evidence_en": ""},
    }


def _json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_extract_json_accepts_code_fence_and_noise() -> None:
    data = summarize._extract_json("```json\n" + _json_text(_payload()) + "\n```")
    assert data["core_contribution_zh"]


def test_validate_payload_rejects_ungrounded_claim() -> None:
    payload = summarize.SummaryPayload.model_validate(_payload())
    payload.methods[0].evidence_en = "This sentence does not exist in the paper."
    errors = summarize._validate_payload(payload, _paper())
    assert errors
    assert any("evidence_en" in error for error in errors)


def test_code_without_evidence_is_not_invented() -> None:
    paper = _paper()
    code = summarize.CodeOutput(status="not_found", urls=[], evidence_en="")
    info = summarize._build_code_info(code, paper, check_links=False)
    assert info["status"] == "not_found"
    assert info["urls"] == []
    assert "未开源" not in info["note_zh"]


def test_code_url_without_reachability_is_claimed() -> None:
    paper = _paper()
    paper.abstract += " Code is available at https://github.com/example/repo."
    code = summarize.CodeOutput(
        status="link_provided",
        urls=["https://github.com/example/repo"],
        evidence_en="Code is available at https://github.com/example/repo.",
    )
    info = summarize._build_code_info(code, paper, check_links=False)
    assert info["status"] == "claimed"
    assert info["urls"] == ["https://github.com/example/repo"]


def test_invalid_json_is_retried_then_succeeds() -> None:
    class FakeClient:
        model = "fake-model"

        def __init__(self) -> None:
            self.outputs = ["这不是 JSON", _json_text(_payload())]

        def complete(self, messages):
            return self.outputs.pop(0), 10, 20

    result = summarize.summarize_one(
        _paper(), FakeClient(), check_code_links=False
    )
    assert result.status == "success"
    assert result.attempts == 2
    assert result.summary["code"]["status"] == "not_found"


def test_all_invalid_json_returns_failed_without_raising() -> None:
    class FakeClient:
        model = "fake-model"

        def complete(self, messages):
            return "not json", 1, 1

    result = summarize.summarize_one(
        _paper(), FakeClient(), check_code_links=False
    )
    assert result.status == "failed"
    assert result.summary is None
    assert result.error
    assert result.title_en
    assert result.abstract_en


def test_missing_key_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(summarize.SummaryConfigError):
        summarize.DeepSeekClient()
