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

def test_gen_answer_returns_fallback_when_no_comments(monkeypatch):
    my_module.QUESTION_HISTORY.clear()
    my_module.QA_HISTORY.clear()

    def fake_answer_question(q, mode, fund, k, length, approx_words, min_conf, llm, **kwargs):
        return "Some answer", []

    monkeypatch.setattr(my_module, "answer_question", fake_answer_question)

    res = my_module.gen_answer("Is anything available?")
    assert res["text"] == my_module.NO_SOURCES_MSG
    assert res["citations"] == {}
