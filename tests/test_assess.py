"""SPEC §10.1 / §15: assessment validation, retries, storage, and the Phase 2 acceptance check."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pewa import assess, signals, snapshot
from pewa.llm import FALLBACK_BETA, LLM, LLMError
from pewa.models import Assessment, LLMCall, Run

WEEK6 = "2026-02-13"  # end of week 6; Epic A is due 2026-02-20


def reply(rating="red", ids=("evt_x",), **extra) -> str:
    risks = [{"summary": "PR stuck in review", "evidence_ids": list(ids)}] if ids else []
    return json.dumps({"rating": rating, "confidence": 0.8, "rationale": "Because.", "predicted_slip_days": None,
                       "key_risks": risks, "suggested_actions": ["Escalate"], **extra})


@pytest.fixture
def week6(atlas: Session):
    state = snapshot.build(atlas, "atlas", snapshot.parse_as_of(WEEK6))
    found = signals.detect(state)
    return state, found


def packet_ids(state, found, key="ATL-1") -> list[str]:
    return sorted(assess.build_packet(state, state.epics[key], found[key]).evidence_ids)


class RecordingLLM(LLM):
    def __init__(self, *responses: str):
        super().__init__("test-model", dry_run=True, scripted=responses)
        self.calls: list[list[dict]] = []

    def complete(self, purpose, system, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        return super().complete(purpose, system, messages, **kw)


# ----- validate() -----


def test_invalid_json_is_rejected():
    with pytest.raises(assess.InvalidAssessment, match="not valid JSON"):
        assess.validate("The epic looks risky.", {"evt_a"})


def test_schema_violations_are_rejected():
    with pytest.raises(assess.InvalidAssessment, match="rating"):
        assess.validate(reply(rating="purple", ids=("evt_a",)), {"evt_a"})
    with pytest.raises(assess.InvalidAssessment, match="confidence"):
        assess.validate(reply(ids=("evt_a",), confidence=1.7), {"evt_a"})


def test_code_fences_are_tolerated():
    output, _ = assess.validate("```json\n" + reply(ids=("evt_a",)) + "\n```", {"evt_a"})
    assert output.rating == "red"


def test_hallucinated_ids_are_dropped():
    output, dropped = assess.validate(reply(ids=("evt_a", "evt_made_up")), {"evt_a"})
    assert output.key_risks[0].evidence_ids == ["evt_a"]
    assert dropped == ["evt_made_up"]


def test_red_without_valid_citation_is_rejected():
    with pytest.raises(assess.InvalidAssessment, match="must cite"):
        assess.validate(reply(ids=()), {"evt_a"})
    with pytest.raises(assess.InvalidAssessment, match="evt_made_up"):
        assess.validate(reply(rating="amber", ids=("evt_made_up",)), {"evt_a"})


def test_green_needs_no_citation():
    output, _ = assess.validate(reply(rating="green", ids=()), set())
    assert output.rating == "green" and output.key_risks == []


# ----- assess_epic(): retry once, then unknown -----


def test_retry_once_with_validation_error(week6):
    state, found = week6
    valid_id = packet_ids(state, found)[0]
    llm = RecordingLLM("not json at all", reply(ids=(valid_id,)))
    result = assess.assess_epic(llm, state, state.epics["ATL-1"], found["ATL-1"])
    assert result.rating == "red" and result.attempts == 2 and result.error is None
    retry = llm.calls[1]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "not valid JSON" in retry[-1]["content"]


def test_second_failure_stores_unknown(week6):
    state, found = week6
    llm = RecordingLLM(reply(ids=("evt_fake1",)), reply(ids=("evt_fake2",)))
    result = assess.assess_epic(llm, state, state.epics["ATL-1"], found["ATL-1"])
    assert result.rating == "unknown" and result.attempts == 2
    assert "evt_fake2" in result.error


def test_cited_evidence_is_resolved_and_bogus_ids_recorded(week6):
    state, found = week6
    valid_id = packet_ids(state, found)[0]
    llm = RecordingLLM(reply(ids=(valid_id, "evt_bogus")))
    result = assess.assess_epic(llm, state, state.epics["ATL-1"], found["ATL-1"])
    assert [e.id for e in result.evidence] == [valid_id]
    assert result.evidence[0].url and result.dropped_evidence_ids == ["evt_bogus"]


def test_llm_failure_stores_unknown(week6):
    state, found = week6
    llm = LLM("test-model", dry_run=True)  # no scripted and no canned -> LLMError

    def fail(*args, **kwargs):
        raise LLMError("model declined the request (category: cyber)")

    llm.complete = fail  # type: ignore[method-assign]
    result = assess.assess_epic(llm, state, state.epics["ATL-1"], found["ATL-1"])
    assert result.rating == "unknown" and "declined" in result.error


def test_done_epic_skips_the_model(week6):
    state, _ = week6
    llm = RecordingLLM()
    result = assess.assess_epic(llm, state, state.epics["ATL-4"], [])
    assert result.rating == "green" and result.skipped == "done" and llm.calls == []


def test_packet_is_bounded_and_epic_scoped(week6):
    state, found = week6
    packet = assess.build_packet(state, state.epics["ATL-1"], found["ATL-1"])
    linked = {e.id: e.epic_key for e in state.events}
    assert 0 < len(packet.evidence) <= assess.MAX_EVIDENCE
    assert all(linked[e.id] == "ATL-1" for e in packet.evidence)
    signal_ids = {i for s in found["ATL-1"] for i in s.evidence}
    assert signal_ids <= packet.evidence_ids  # signal evidence is always included
    _, user = assess.render_prompt(packet)
    assert "ATL-1" in user and all(e.id in user for e in packet.evidence)


# ----- storage and the LLM wrapper -----


def test_run_stores_run_assessments_and_llm_calls(atlas: Session):
    result = assess.run(atlas, "atlas", snapshot.parse_as_of(WEEK6), LLM(dry_run=True, session=atlas), label="t")
    run = atlas.get(Run, result.run_id)
    assert run.model == "dry-run" and run.label == "t" and run.mode == "live"
    rows = atlas.scalars(select(Assessment).where(Assessment.run_id == run.id)).all()
    assert {r.epic_key for r in rows} == {"ATL-1", "ATL-2", "ATL-3", "ATL-4", "ATL-5"}
    epic_a = next(r for r in rows if r.epic_key == "ATL-1")
    assert epic_a.evidence and epic_a.signals and epic_a.detail["key_risks"]
    calls = atlas.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.dry_run))
    assert calls == 4  # one per epic that is not done


def fake_client(stop_reason="end_turn", text=None, category=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text or reply(rating="green", ids=()))],
            stop_reason=stop_reason,
            stop_details=SimpleNamespace(category=category),
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create), beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return client, calls


def test_api_call_uses_refusal_fallbacks_and_logs_cost(session: Session):
    llm = LLM("claude-sonnet-5-5", session=session)
    llm._client, calls = fake_client()
    text = llm.complete("assess", "sys", [{"role": "user", "content": "hi"}])
    assert json.loads(text)["rating"] == "green"
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == [FALLBACK_BETA]
    assert "temperature" not in calls[0]
    row = session.scalars(select(LLMCall)).one()
    assert (row.input_tokens, row.output_tokens, row.dry_run) == (1000, 200, False)
    assert row.cost_usd == pytest.approx(1000 * 2 / 1e6 + 200 * 10 / 1e6)


def test_models_without_fallback_support_use_plain_create(session: Session):
    llm = LLM("claude-haiku-5-5", session=session)
    llm._client, calls = fake_client()
    llm.complete("assess", "sys", [{"role": "user", "content": "hi"}])
    assert "fallbacks" not in calls[0]


def test_refusal_raises_and_is_logged(session: Session):
    llm = LLM("claude-sonnet-5-5", session=session)
    llm._client, _ = fake_client(stop_reason="refusal", category="cyber")
    with pytest.raises(LLMError, match="cyber"):
        llm.complete("assess", "sys", [{"role": "user", "content": "hi"}])
    assert "declined" in session.scalars(select(LLMCall)).one().error


# ----- Phase 2 acceptance -----


@pytest.mark.parametrize("as_of", ["2026-02-09", WEEK6])
def test_acceptance_week6_dry_run(atlas: Session, as_of):
    """On Atlas at week 6, Epic A is amber or red with >= 1 valid citation; D and E are green."""
    state = snapshot.build(atlas, "atlas", snapshot.parse_as_of(as_of))
    result = assess.run(atlas, "atlas", state.as_of, LLM(dry_run=True, session=atlas))
    by_epic = {a.epic_key: a for a in result.assessments}
    linked = {e.id: e.epic_key for e in state.events}

    epic_a = by_epic["ATL-1"]
    assert epic_a.rating in ("amber", "red")
    assert epic_a.evidence and all(linked.get(e.id) == "ATL-1" for e in epic_a.evidence)
    assert by_epic["ATL-4"].rating == "green"
    assert by_epic["ATL-5"].rating == "green"


def test_auth_failure_aborts_the_run(week6):
    from pewa.llm import LLMAuthError

    state, found = week6
    llm = LLM("test-model", dry_run=True)

    def reject(*args, **kwargs):
        raise LLMAuthError("API rejected the credentials (401): invalid x-api-key")

    llm.complete = reject  # type: ignore[method-assign]
    with pytest.raises(LLMAuthError):
        assess.assess_epic(llm, state, state.epics["ATL-1"], found["ATL-1"])


def test_pewa_specific_key_name_is_used(monkeypatch):
    from pewa.config import Settings

    monkeypatch.setenv("PEWA_ANTHROPIC_API_KEY", "sk-ant-pewa")
    assert Settings(_env_file=None).anthropic_key() == "sk-ant-pewa"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-standard")
    assert Settings(_env_file=None).anthropic_key() == "sk-ant-pewa"  # pewa name wins
    monkeypatch.delenv("PEWA_ANTHROPIC_API_KEY")
    assert Settings(_env_file=None).anthropic_key() == "sk-ant-standard"
