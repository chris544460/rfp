import sys, pathlib, types, json

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

def test_gen_answer_returns_text(monkeypatch):
    my_module.QUESTION_HISTORY.clear()
    def fake_answer_question(q, mode, fund, k, length, approx_words, min_conf, llm):
        data = {"correct": ["A"], "explanations": {"A": "Because it's correct [1]"}}
        return json.dumps(data), [("1", "src.txt", "snippet", 0.9, "2024-01-01")]
    monkeypatch.setattr(my_module, "answer_question", fake_answer_question)
    res = my_module.gen_answer("Which option?", ["Option1", "Option2"])
    assert res["text"] == "The correct answer is: Option1. A. Because it's correct [1]"
    assert res["citations"] == {1: {"text": "snippet", "source_file": "src.txt"}}
