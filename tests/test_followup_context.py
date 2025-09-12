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


def test_followup_context_is_appended(monkeypatch):
    calls = []

    def fake_answer_question(q, mode, fund, k, length, approx_words, min_conf, llm):
        calls.append(q)
        return "ans", []

    monkeypatch.setattr(my_module, "answer_question", fake_answer_question)
    monkeypatch.setattr(my_module, "_detect_followup", lambda q, h: [1] if h else [])
    monkeypatch.setattr(
        my_module, "_classify_intent", lambda q, h: "follow_up" if h else "new"
    )

    my_module.QUESTION_HISTORY.clear()
    my_module.gen_answer("Do you provide IT support?")
    my_module.gen_answer("Please provide comments if yes.")

    assert "Do you provide IT support?" in calls[1]
    assert "Please provide comments if yes." in calls[1]
    assert len(my_module.QUESTION_HISTORY) == 2


def test_clarify_intent_asks_for_more_info(monkeypatch):
    calls = []

    def fake_answer_question(q, mode, fund, k, length, approx_words, min_conf, llm):
        calls.append(q)
        return "ans", []

    monkeypatch.setattr(my_module, "answer_question", fake_answer_question)
    monkeypatch.setattr(my_module, "_classify_intent", lambda q, h: "clarify")

    my_module.QUESTION_HISTORY.clear()
    resp = my_module.gen_answer("More details?")

    assert "clarify" in resp["text"].lower()
    assert calls == []
    assert my_module.QUESTION_HISTORY == []
