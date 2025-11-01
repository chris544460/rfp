#!/usr/bin/env python3

"""Streamlit application entrypoint for the RFP responder experience."""

from __future__ import annotations

import subprocess
import sys
from typing import Tuple

import streamlit as st

from typing import TYPE_CHECKING, Tuple

from frontend.session_state import initialize_session_state


def configure_page() -> None:
    """Configure Streamlit and apply the shared design system."""

    from backend.design import (
        APP_NAME,
        StyleCSS,
        StyleColors,
        display_aladdin_logos_and_app_title,
    )

    st.set_page_config(
        page_title=APP_NAME,
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    StyleCSS.set_css_styling()
    StyleCSS.set_plotly_template(
        "aladdin",
        list(StyleColors.DATAVIZ_COLORS),
        set_as_default=True,
    )
    display_aladdin_logos_and_app_title()


@st.cache_resource
def cached_install(package: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])


SETUP_VERSION = "2025-09-azure-feedback"

REQUIRED_PACKAGES = [
    "certifi",
    "charset-normalizer",
    "faiss-cpu",
    "idna",
    "numpy",
    "packaging",
    "python-dotenv",
    "requests",
    "urllib3",
    "pyarrow",
    "PyPDF2",
    "python-docx",
    "spacy",
    "azure-storage-blob",
]


def ensure_packages() -> None:
    """Install user-space dependencies inside the Streamlit session."""

    if st.session_state.get("setup_version") == SETUP_VERSION:
        return

    progress_placeholder = st.empty()
    total = len(REQUIRED_PACKAGES)
    for idx, package in enumerate(REQUIRED_PACKAGES, start=1):
        try:
            cached_install(package)
        except subprocess.CalledProcessError:
            progress_placeholder.empty()
            st.error(
                "Something went wrong while setting things up. Please try again or contact support."
            )
            return
        percent = int(idx / total * 100)
        message = f"Setting up step {idx} of {total}..."
        progress_placeholder.info(f"{message} ({percent}% complete)")

    progress_placeholder.success("Setup complete.")
    st.session_state["setup_version"] = SETUP_VERSION
    st.toast(
        "You're all set! Choose 'Upload document' to load an RFP or 'Ask a question' to chat. "
        "Provide any required API keys in the sidebar."
    )


class StreamlitApp:
    """Thin orchestrator wiring together the chat and document modes."""

    def __init__(self) -> None:
        from backend.components import FeedbackUI
        from backend.workflows import DocumentJobController
        from frontend.feedback import build_feedback_manager
        from frontend.chat_page import render_chat_page
        from frontend.config_panel import (
            collect_app_config,
            ensure_api_credentials,
            select_framework,
        )
        from frontend.document_page import render_document_page

        self._render_chat_page = render_chat_page
        self._collect_app_config = collect_app_config
        self._ensure_api_credentials = ensure_api_credentials
        self._select_framework = select_framework
        self._render_document_page = render_document_page

        feedback_manager = build_feedback_manager()
        self.feedback_manager = feedback_manager
        self.feedback_ui = FeedbackUI(
            log_feedback=feedback_manager.log,
            get_current_user=feedback_manager.get_current_user,
            serialize_list=feedback_manager.serialize_list,
            format_context=feedback_manager.format_context,
        )
        self.document_controller = DocumentJobController(self.feedback_ui)

    def run(self) -> None:
        st.title("RFP Responder")
        initialize_session_state()
        self._render_styles()

        view_mode = "User"
        input_mode = st.radio(
            "How would you like to proceed?",
            ["Upload document", "Ask a question"],
            index=1,
            horizontal=True,
        )

        framework, llm_model = self._select_framework(view_mode)
        self._ensure_api_credentials(framework, view_mode)
        config = self._collect_app_config(view_mode, framework, llm_model)

        if input_mode == "Ask a question":
            self._render_chat_page(view_mode, config, self.feedback_ui)
        else:
            self._render_document_page(
                view_mode,
                config,
                self.feedback_ui,
                self.feedback_manager,
                self.document_controller,
            )

    def _select_framework(self, view_mode: str) -> Tuple[str, str]:
        return select_framework(view_mode)

    @staticmethod
    def _render_styles() -> None:
        st.markdown(
            """
            <style>
            div.block-container{
                max-width: 900px;
                padding: 32px 48px;
            }
            div[data-testid="stChatMessage"]{
                border-radius: 0;
                border: 1px solid #d4d6db;
                padding: 16px;
                margin-bottom: 16px;
                box-shadow: 0 2px 6px rgba(17, 24, 39, 0.08);
            }
            div[data-testid="stChatMessage-user"]{
                background-color: #f2f5f8;
            }
            div[data-testid="stChatMessage-assistant"]{
                background-color: #ffffff;
            }
            div[data-testid="stChatInput"] textarea{
                border-radius: 0;
                border: 1px solid #d4d6db;
                padding: 12px;
            }
            @keyframes shimmer{
                0%{background-position:-1000px 0;}
                100%{background-position:1000px 0;}
            }
            .shimmer{
                background:linear-gradient(90deg,#d0d0d0 0%,#b0b0b0 50%,#d0d0d0 100%);
                background-size:1000px 100%;
                animation:shimmer 2s infinite linear;
                -webkit-background-clip:text;
                -webkit-text-fill-color:transparent;
            }
            .hit-card{
                border:1px solid #d4d6db;
                border-radius:0;
                padding:18px 22px;
                background-color:#f5f6f8;
                margin-top:18px;
                box-shadow: 0 2px 6px rgba(17, 24, 39, 0.08);
            }
            .hit-meta{
                font-size:0.85rem;
                color:#525760;
                margin-bottom:12px;
            }
            .hit-reason{
                font-size:0.9rem;
                color:#30343b;
                margin-bottom:12px;
            }
            .hit-reason-label{
                font-weight:600;
                color:#1f232a;
                margin-right:6px;
            }
            .hit-snippet{
                font-size:0.95rem;
                line-height:1.6;
                color:#111827;
            }
            .hit-score{
                font-size:0.8rem;
                color:#6b7280;
                margin-top:6px;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )


def main() -> None:
    configure_page()
    ensure_packages()
    app = StreamlitApp()
    app.run()


if __name__ == "__main__":
    main()
