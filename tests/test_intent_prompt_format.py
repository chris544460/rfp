from rfp import my_module
from types import SimpleNamespace


def test_intent_prompt_handles_json_braces(monkeypatch):
    # Patch LLM client to avoid external calls and return valid JSON
    dummy_client = SimpleNamespace(get_completion=lambda prompt: ('{"intent": "follow_up"}', None))
    monkeypatch.setattr(my_module, "_llm_client", dummy_client)

    # Provide history so the classification logic constructs the prompt
    result = my_module.classify_intent("Is this related?", ["Previous question"])
    assert result == "follow_up"
