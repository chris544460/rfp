import rfp_xlsx_slot_finder as finder


def test_aladdin_llm_call_without_openai_key(monkeypatch):
    """Ensure Aladdin framework does not require OPENAI_API_KEY."""

    # Provide the credentials expected by the Aladdin client
    monkeypatch.setenv("aladdin_studio_api_key", "key")
    monkeypatch.setenv("defaultWebServer", "server")
    monkeypatch.setenv("aladdin_user", "user")
    monkeypatch.setenv("aladdin_passwd", "pass")

    # Explicitly remove any OpenAI key
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Force the framework to aladdin
    monkeypatch.setattr(finder, "FRAMEWORK", "aladdin")

    # Track whether the LLM helper was invoked
    called = {}

    def fake_call_llm(prompt_file, payload, *, model):
        called["was_called"] = True
        return {"sheet": "Sheet1", "answer_cell": "B1"}

    monkeypatch.setattr(finder, "_call_llm", fake_call_llm)

    sheet, cell = finder._llm_choose_answer_slot(
        {"question_cell": "A1"}, {}, model="test-model", debug=False
    )

    assert called.get("was_called"), "LLM call was skipped for aladdin framework"
    assert (sheet, cell) == ("Sheet1", "B1")


def test_openai_requires_api_key(monkeypatch):
    """OpenAI framework should skip when OPENAI_API_KEY is missing."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(finder, "FRAMEWORK", "openai")

    called = {}

    def fake_call_llm(prompt_file, payload, *, model):
        called["was_called"] = True
        return {}

    monkeypatch.setattr(finder, "_call_llm", fake_call_llm)

    sheet, cell = finder._llm_choose_answer_slot({}, {}, model="test-model", debug=False)

    assert not called.get("was_called")
    assert (sheet, cell) == (None, None)

