import sys, pathlib, types

# Ensure project root on path
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

# Stub external dependencies to avoid import-time failures
fake_ac = types.ModuleType("answer_composer")

class DummyClient:
    def __init__(self, model: str | None = None):
        pass

    def get_completion(self, prompt: str, json_output: bool = False):
        return "", {}

fake_ac.CompletionsClient = DummyClient
fake_ac.get_openai_completion = lambda prompt, model, json_output=False: ("", {})
sys.modules["answer_composer"] = fake_ac

fake_search = types.ModuleType("search.vector_search")
fake_search.search = lambda *args, **kwargs: []
sys.modules["search.vector_search"] = fake_search

import my_module


def test_developer_mode_returns_steps(monkeypatch):
    """gen_answer should include debug steps when developer_mode=True."""

    my_module.QUESTION_HISTORY.clear()

    monkeypatch.setattr(my_module, "_classify_intent", lambda q, h: "new")
    monkeypatch.setattr(my_module, "_detect_followup", lambda q, h: [])

    def fake_answer(q, mode, fund, k, length, approx_words, min_conf, llm, extra_docs=None, return_steps=False):
        if return_steps:
            return "ans", [], ["core step"]
        return "ans", []

    monkeypatch.setattr(my_module, "answer_question", fake_answer)

    res = my_module.gen_answer("What?", developer_mode=True)
    assert "debug_steps" in res
    assert res["debug_steps"][0] == "intent: new"
    assert "core step" in res["debug_steps"]
