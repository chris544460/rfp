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


def test_followup_skips_search_and_uses_history(monkeypatch):
    search_calls = []
    llm_calls = []

    def fake_answer_question(q, mode, fund, k, length, approx_words, min_conf, llm):
        search_calls.append(q)
        return "Initial answer [1]", [("1", "src.txt", "snippet", 0.9, "2024")]

    def fake_completion(prompt, json_output=False):
        llm_calls.append(prompt)
        return "Follow-up reply", {}

    monkeypatch.setattr(my_module, "answer_question", fake_answer_question)
    monkeypatch.setattr(my_module, "_llm_client", types.SimpleNamespace(get_completion=fake_completion))
    monkeypatch.setattr(my_module, "_detect_followup", lambda q, h: [1] if h else [])
    monkeypatch.setattr(
        my_module, "_classify_intent", lambda q, h: "follow_up" if h else "new"
    )

    my_module.QUESTION_HISTORY.clear()
    my_module.QA_HISTORY.clear()
    my_module.gen_answer("Do you provide IT support?")
    my_module.gen_answer("Please provide comments if yes.")

    assert len(search_calls) == 1
    assert "Do you provide IT support?" in search_calls[0]
    assert len(llm_calls) == 1
    # Follow-up prompt should include previous Q and answer snippet
    assert "Do you provide IT support?" in llm_calls[0]
    assert "Initial answer" in llm_calls[0]
    assert "snippet" in llm_calls[0]
    assert len(my_module.QUESTION_HISTORY) == 2
