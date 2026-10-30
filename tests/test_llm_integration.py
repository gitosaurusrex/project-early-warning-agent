"""Opt-in: runs the real model on Atlas (SPEC §15).

    PEWA_ANTHROPIC_API_KEY=... uv run pytest -m llm   (ANTHROPIC_API_KEY also works)
"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from conftest import REAL_ANTHROPIC_API_KEY
from pewa import assess, snapshot
from pewa.config import get_settings
from pewa.llm import LLM

pytestmark = pytest.mark.llm


@pytest.fixture
def real_key(monkeypatch):
    # conftest strips credentials for hermetic tests; restore the real key for this one.
    if not REAL_ANTHROPIC_API_KEY:
        pytest.skip("set PEWA_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) to run the real-model test")
    monkeypatch.setenv("PEWA_ANTHROPIC_API_KEY", REAL_ANTHROPIC_API_KEY)
    get_settings.cache_clear()


def test_real_model_flags_epic_a_a_week_early(atlas: Session, real_key):
    planned = date(2026, 2, 20)
    as_of = snapshot.parse_as_of("2026-02-13")  # one week before Epic A's planned date
    state = snapshot.build(atlas, "atlas", as_of)
    result = assess.run(atlas, "atlas", as_of, LLM(get_settings().pewa_model, session=atlas))
    by_epic = {a.epic_key: a for a in result.assessments}
    linked = {e.id: e.epic_key for e in state.events}

    epic_a = by_epic["ATL-1"]
    assert (planned - as_of.date()).days >= 7
    assert epic_a.rating in ("amber", "red"), epic_a.rationale
    assert epic_a.evidence and all(linked.get(e.id) == "ATL-1" for e in epic_a.evidence)
    assert by_epic["ATL-4"].rating == "green"
    assert by_epic["ATL-5"].rating == "green", by_epic["ATL-5"].rationale
