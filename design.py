"""Shared design system utilities for the Streamlit UI."""

from __future__ import annotations

from typing import Iterable, List

import streamlit as st

try:
    import plotly.graph_objects as go
    import plotly.io as pio
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    go = None
    pio = None

# ---------------------------------------------------------------------------
# Palette


APP_NAME = "AI Deriv Docs"


class StyleColors:
    """Centralised brand palette."""

    white = "#ffffff"
    black = "#000000"

    grey_02 = "#fbfbfc"
    grey_05 = "#f8f8f9"
    grey_10 = "#f1f2f4"
    grey_15 = "#e8e9eb"
    grey_20 = "#dbdde1"
    grey_30 = "#c0c3ca"
    grey_40 = "#a5aab3"
    grey_50 = "#888e9a"
    grey_60 = "#6d7481"
    grey_70 = "#575c66"
    grey_80 = "#44444c"
    grey_90 = "#282a2f"
    grey_95 = "#17191c"

    action_blue_10 = "#f5f8ff"
    action_blue_20 = "#e6ecff"
    action_blue_30 = "#cad7ff"
    action_blue_40 = "#80a0ff"
    action_blue_50 = "#6e84ff"
    action_blue_55 = "#4c64f8"
    action_blue_60 = "#0000f3"
    action_blue_70 = "#1d1db1"
    action_blue_80 = "#18187c"
    action_blue_90 = "#171f46"

    blue_60 = "#007ac9"
    purple_red_50 = "#cb2cc0"
    green_blue_60 = "#1fae96"
    red_orange_40 = "#ff7132"
    purple_50 = "#9952e0"
    orange_yellow_60 = "#d7a720"
    green_70 = "#26732d"
    red_60 = "#bc0300"
    blue_80 = "#003e65"

    DATAVIZ_COLORS: Iterable[str] = (
        blue_60,
        purple_red_50,
        green_blue_60,
        red_orange_40,
        purple_50,
        orange_yellow_60,
        action_blue_70,
        green_70,
        action_blue_55,
        red_60,
        blue_80,
    )


class StyleCSS:
    """Encapsulates application-wide CSS fragments."""

    PRIMARY_FONT = "'Roboto', sans-serif"

    APP_TITLE_HEADER = "app-title-header"
    HEADER_SVG_A = "header-svg-a"
    HEADER_SVG_ALADDIN = "header-svg-aladdin"
    CUSTOM_LINE = "custom-line"

    @staticmethod
    def set_css_styling() -> None:
        """Inject global CSS into the Streamlit app."""

        st.markdown(
            f"""
            <style>
            * {{
                font-family: {StyleCSS.PRIMARY_FONT};
                font-size: 14px;
            }}

            :root {{
                --streamlit-primary-color: {StyleColors.action_blue_60};
            }}

            .stApp {{
                padding-top: 0;
            }}

            section[data-testid="stSidebar"] > div:first-child {{
                background-color: {StyleColors.grey_02};
            }}

            div.block-container {{
                margin-left: 0rem;
                margin-right: 0rem;
                margin-top: 3.3rem;
                margin-bottom: 0rem;
                padding-top: 4rem;
                padding-bottom: 4rem;
                padding-left: 1.75rem;
                padding-right: 1.75rem;
                background-color: {StyleColors.grey_15};
            }}

            [data-testid="stVerticalBlock"] {{
                gap: 0.75rem;
            }}

            [data-testid="stColumn"] {{
                background-color: {StyleColors.white};
                padding: 1.75rem;
                border: 1px solid {StyleColors.grey_30};
            }}

            iframe {{
                height: 48px;
            }}

            #stDecoration {{
                background-image: linear-gradient(90deg, {StyleColors.action_blue_60}, {StyleColors.action_blue_30});
                height: 0rem;
            }}

            [data-testid="stToolbar"] {{
                top: 0.5rem !important;
                right: 0.5rem !important;
                z-index: 100 !important;
            }}

            header {{
                position: fixed !important;
                top: 0 !important;
                left: 0 !important;
                right: 0 !important;
                height: 48px !important;
                background: {StyleColors.grey_05} !important;
                z-index: 2 !important;
                border-bottom: 1px solid {StyleColors.grey_30} !important;
                display: flex !important;
                align-items: center !important;
                gap: 1rem;
                padding-left: 1.5rem;
                font-family: 'BLK Fort', sans-serif !important;
                font-size: 17px !important;
                font-weight: 700 !important;
            }}

            .{StyleCSS.APP_TITLE_HEADER} {{
                display: flex;
                align-items: center;
                gap: 0.75rem;
            }}

            .{StyleCSS.HEADER_SVG_A} {{
                height: 2rem;
                width: 2rem;
                border-radius: 6px;
                background-color: {StyleColors.grey_95};
                display: inline-flex;
                align-items: center;
                justify-content: center;
            }}

            .{StyleCSS.HEADER_SVG_A} svg {{
                height: 1.2rem;
                width: 1.2rem;
                fill: {StyleColors.grey_05};
            }}

            .{StyleCSS.HEADER_SVG_ALADDIN} svg {{
                height: 2.6rem;
                fill: {StyleColors.grey_95};
            }}

            div.stSlider > div[data-baseweb="slider"] > div > div[role="slider"] {{
                background-color: {StyleColors.action_blue_60};
                color: {StyleColors.action_blue_60};
            }}

            div.stSlider div[data-testid="stSliderThumbValue"] {{
                color: {StyleColors.action_blue_60};
            }}

            div.stButton > button,
            div.stDownloadButton > button,
            div.stFormSubmitButton > button {{
                border-radius: 2px;
                min-height: 36px;
            }}

            div.stButton > button[kind="secondary"],
            div.stDownloadButton > button[kind="secondary"],
            div.stFormSubmitButton > button[kind="secondaryFormSubmit"] {{
                background-color: {StyleColors.action_blue_60};
                color: {StyleColors.white};
                border-color: {StyleColors.action_blue_60};
            }}

            div.stButton > button[kind="secondary"]:hover,
            div.stDownloadButton > button[kind="secondary"]:hover,
            div.stFormSubmitButton > button[kind="secondaryFormSubmit"]:hover {{
                background-color: {StyleColors.action_blue_50};
                border-color: {StyleColors.action_blue_50};
            }}

            div.stButton > button[kind="primary"],
            div.stDownloadButton > button[kind="primary"],
            div.stFormSubmitButton > button[kind="primaryFormSubmit"] {{
                background-color: {StyleColors.white};
                color: {StyleColors.action_blue_60};
                border: 1px solid {StyleColors.action_blue_55};
            }}

            div.stButton > button[kind="primary"]:hover,
            div.stDownloadButton > button[kind="primary"]:hover,
            div.stFormSubmitButton > button[kind="primaryFormSubmit"]:hover {{
                color: {StyleColors.action_blue_50};
                border-color: {StyleColors.action_blue_50};
            }}

            button[data-baseweb="tab"] {{
                padding-left: 12px;
                padding-right: 12px;
            }}

            button[data-baseweb="tab"][aria-selected="true"] {{
                background-color: {StyleColors.action_blue_10};
                color: {StyleColors.grey_95};
            }}

            div[data-baseweb="tab-list"] {{
                border-top: 2px solid {StyleColors.grey_10};
                gap: 0rem;
            }}

            div[data-testid="metric-container"] {{
                background-color: {StyleColors.grey_05};
                border: 1px solid {StyleColors.grey_30};
                border-radius: 5px;
                padding: 10px;
                margin: 5px 0;
            }}

            div[data-testid="metric-container"] > div {{
                color: {StyleColors.action_blue_60};
                font-weight: 600 !important;
            }}

            .{StyleCSS.CUSTOM_LINE} {{
                border: none;
                border-top: 2px solid {StyleColors.grey_10};
                margin: 5px 0 15px 0;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )

    @staticmethod
    def insert_line_break(
        color: str = StyleColors.grey_10,
        margin_top: int = 5,
        margin_bottom: int = 15,
        weight: int = 2,
    ) -> None:
        """Render a custom divider."""

        st.markdown(
            f"""
            <div style="border-top: {weight}px solid {color}; margin-top: {margin_top}px; margin-bottom: {margin_bottom}px;"></div>
            """,
            unsafe_allow_html=True,
        )

    @staticmethod
    def set_plotly_template(
        template_name: str,
        template_colors: List[str],
        set_as_default: bool = False,
        font_family: str = PRIMARY_FONT,
    ) -> None:
        """Register a Plotly template for branded charts."""

        if go is None or pio is None:
            return

        custom_template = go.layout.Template(
            layout=go.Layout(colorway=template_colors, font=dict(family=font_family))
        )
        pio.templates[template_name] = custom_template
        if set_as_default:
            pio.templates.default = template_name


SVG_ALADDIN_A = "M2 14 L7 2 L12 14 Z"
SVG_ALADDIN_WORDMARK = "M0 12 L0 2 L2 2 L2 12 Z"


def display_aladdin_logos_and_app_title() -> None:
    """Render the header logo cluster."""

    st.markdown(
        f"""
        <header>
            <div class="{StyleCSS.APP_TITLE_HEADER}">
                <div class="{StyleCSS.HEADER_SVG_A}">
                    <svg viewBox="0 0 14 16" xmlns="http://www.w3.org/2000/svg">
                        <path d="{SVG_ALADDIN_A}" />
                    </svg>
                </div>
                <div class="{StyleCSS.HEADER_SVG_ALADDIN}">
                    <svg viewBox="0 0 120 28" xmlns="http://www.w3.org/2000/svg">
                        <path d="{SVG_ALADDIN_WORDMARK}" />
                    </svg>
                </div>
                <span>{APP_NAME}</span>
            </div>
        </header>
        """,
        unsafe_allow_html=True,
    )


# Backwards compatibility with earlier naming in notebooks.
StyledCSS = StyleCSS
