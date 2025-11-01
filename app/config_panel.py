from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import streamlit as st


MODEL_DESCRIPTIONS = {
    "gpt-4.1-nano-2025-04-14_research": "Lighter, faster model",
    "o3-2025-04-16_research": "Slower, reasoning model",
}

MODEL_SHORT_NAMES = {
    "gpt-4.1-nano-2025-04-14_research": "4.1",
    "o3-2025-04-16_research": "o3",
}

MODEL_OPTIONS = list(MODEL_DESCRIPTIONS.keys())
FOLLOWUP_DEFAULT_MODEL = "gpt-4.1-nano-2025-04-16_research"
DEFAULT_MODEL = "o3-2025-04-16_research"
DOC_DEFAULT_MODEL = "o3-2025-04-16_research"


try:
    DEFAULT_INDEX = MODEL_OPTIONS.index(DEFAULT_MODEL)
except ValueError:
    DEFAULT_INDEX = 0
    DEFAULT_MODEL = MODEL_OPTIONS[0]


@dataclass
class AppConfig:
    framework: str
    llm_model: str
    search_mode: str
    fund: str
    k_max_hits: int
    min_confidence: float
    length_opt: str
    approx_words: Optional[int]
    include_citations: bool
    show_live: bool
    docx_as_text: bool
    docx_write_mode: str
    extra_uploads: List[Any] = field(default_factory=list)


def select_framework(view_mode: str) -> Tuple[str, str]:
    """Collect framework and initial model selection."""

    doc_default_model = (
        DOC_DEFAULT_MODEL if DOC_DEFAULT_MODEL in MODEL_OPTIONS else MODEL_OPTIONS[DEFAULT_INDEX]
    )
    llm_model = doc_default_model
    framework_env = os.getenv("ANSWER_FRAMEWORK")
    if framework_env:
        framework = framework_env
        if view_mode == "Developer":
            st.info(f"Using framework from ANSWER_FRAMEWORK: {framework_env}")
    else:
        framework = st.selectbox(
            "Framework",
            ["aladdin", "openai"],
            index=0,
            help="Choose backend for language model.",
        )
    return framework, llm_model


def ensure_api_credentials(framework: str, view_mode: str) -> None:
    """Prompt for framework credentials when they are not provided via environment."""

    if framework == "aladdin":
        for key, label in [
            ("aladdin_studio_api_key", "Aladdin Studio API key"),
            ("defaultWebServer", "Default Web Server"),
            ("aladdin_user", "Aladdin user"),
            ("aladdin_passwd", "Aladdin password"),
        ]:
            if st.session_state.get(key) or st.session_state.get(key.upper()):
                if view_mode == "Developer":
                    st.info(f"{key} loaded from session or environment")
                continue
            val = st.text_input(
                label,
                type="password" if "passwd" in key or "api_key" in key else "default",
            )
            if val:
                st.session_state[key] = val
    else:
        if os.getenv("OPENAI_API_KEY"):
            if view_mode == "Developer":
                st.info("OPENAI_API_KEY loaded from environment")
        else:
            api_key = st.text_input(
                "OpenAI API key",
                type="password",
                help="API key for OpenAI.",
            )
            if api_key:
                st.session_state["OPENAI_API_KEY"] = api_key


def collect_app_config(
    view_mode: str,
    framework: str,
    llm_model: str,
) -> AppConfig:
    """Render the options sidebars and capture configuration for downstream flows."""

    if view_mode == "Developer":
        st.info("Search mode fixed to 'both'")
        search_mode = "both"
        fund = st.selectbox(
            "Fund",
            [""] + load_fund_tags(),
            index=0,
            help="Filter answers for a specific fund or strategy.",
        )
        llm_model = st.selectbox(
            "LLM model",
            MODEL_OPTIONS,
            index=DEFAULT_INDEX,
            format_func=lambda m: f"{m} - {MODEL_DESCRIPTIONS[m]}",
            help="Model name for generating answers.",
        )
        k_max_hits = st.number_input(
            "Hits per question",
            value=10,
            help="Maximum documents retrieved per question.",
        )
        min_confidence = st.number_input(
            "Min confidence",
            value=0.0,
            help="Minimum score for retrieved documents.",
        )
        docx_as_text = st.checkbox("Treat DOCX as text", value=False)
        docx_write_mode = st.selectbox("DOCX write mode", ["fill", "replace", "append"], index=0)
        extra_uploads = st.file_uploader(
            "Additional documents",
            type=["pdf", "docx", "xls", "xlsx"],
            accept_multiple_files=True,
            help="Additional PDF or Word documents to include in search.",
        )
        extra_uploads = filter_extra_uploads(extra_uploads)
    else:
        search_mode = "both"
        fund = st.selectbox(
            "Fund",
            [""] + load_fund_tags(),
            index=0,
            help="Select fund or strategy context for better answers.",
        )
        k_max_hits = 20
        min_confidence = 0.0
        docx_as_text = False
        docx_write_mode = "fill"
        extra_uploads = st.file_uploader(
            "Additional documents",
            type=["pdf", "docx", "xls", "xlsx"],
            accept_multiple_files=True,
            help="Additional PDF or Word documents to include in search.",
        )
        extra_uploads = filter_extra_uploads(extra_uploads)

    with st.expander("More options"):
        if view_mode == "User":
            try:
                index = MODEL_OPTIONS.index(llm_model)
            except ValueError:
                index = 0
            llm_model = st.selectbox(
                "Model",
                MODEL_OPTIONS,
                index=index,
                format_func=lambda m: f"{MODEL_SHORT_NAMES[m]} - {MODEL_DESCRIPTIONS[m]}",
                help="Choose which model generates answers.",
            )
        length_opt = st.selectbox("Answer length", ["auto", "short", "medium", "long"], index=3)
        approx_words_text = st.text_input(
            "Approx words",
            value="",
            help="Approximate words per answer (optional).",
        )
        include_env = st.session_state.get("RFP_INCLUDE_COMMENTS")
        if include_env is not None:
            include_citations = include_env != "0"
            st.info(f"Using include citations from RFP_INCLUDE_COMMENTS: {include_citations}")
        else:
            include_citations = st.checkbox("Include citations", value=True)
        show_live = st.checkbox("Show questions and answers during processing", value=True)

    approx_words = None
    if approx_words_text:
        try:
            approx_words = int(approx_words_text)
        except ValueError:
            st.warning("Approx words must be an integer. Ignoring value.")

    return AppConfig(
        framework=framework,
        llm_model=llm_model,
        search_mode=search_mode,
        fund=fund,
        k_max_hits=int(k_max_hits),
        min_confidence=float(min_confidence),
        length_opt=length_opt,
        approx_words=approx_words,
        include_citations=include_citations,
        show_live=show_live,
        docx_as_text=bool(docx_as_text),
        docx_write_mode=docx_write_mode,
        extra_uploads=extra_uploads,
    )


def filter_extra_uploads(files) -> List[Any]:
    """Filter unsupported files from the optional extra uploads control."""

    if not files:
        return []
    allowed: List[Any] = []
    invalid: List[str] = []
    for file in files:
        if Path(file.name).suffix.lower() in {".pdf", ".docx", ".xls", ".xlsx"}:
            allowed.append(file)
        else:
            invalid.append(file.name)
    if invalid:
        st.warning("Unsupported file types were ignored: " + ", ".join(invalid))
    return allowed


def load_fund_tags() -> List[str]:
    path = Path("structured_extraction/parsed_json_outputs/embedding_data.json")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    tags = {t for item in data for t in item.get("metadata", {}).get("tags", [])}
    return sorted(tags)


__all__ = [
    "AppConfig",
    "collect_app_config",
    "ensure_api_credentials",
    "filter_extra_uploads",
    "select_framework",
    "MODEL_DESCRIPTIONS",
    "MODEL_SHORT_NAMES",
    "MODEL_OPTIONS",
    "FOLLOWUP_DEFAULT_MODEL",
]
