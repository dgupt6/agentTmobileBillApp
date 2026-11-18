# "Ask AI" popover: chat with a Groq model about the bill shown on the page.
from __future__ import annotations

import os

import streamlit as st

from tmobile_bill_agent.bill_chat import (
    DEFAULT_CHAT_MODEL, DEFAULT_PDF_CHARS, GROQ, build_messages, select_statement_text,
    stream_answer)
from tmobile_bill_agent.llm_providers import LLMError

HISTORY_KEY = "ai_chat_messages"
SUGGESTIONS = ["Who pays the most this month and why?",
               "What is the account number and the due date?",
               "Explain how the account charge is split.",
               "How does this bill compare with last month?"]


def _secret(name, default=""):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass  # no secrets.toml
    return os.environ.get(name, default)


def chat_popover(context: str, pdf_pages: list[str] | None = None, api_key: str | None = None,
                 container=None):
    """Render the 'Ask AI' button; clicking it opens the chat. context is the bill
    summary from bill_chat.build_bill_context(); pdf_pages is the statement text per
    page (or None). container: where to put the button."""
    api_key = api_key or _secret(GROQ["key_env"])
    model = _secret("GROQ_CHAT_MODEL", DEFAULT_CHAT_MODEL)
    try:
        pdf_budget = int(_secret("CHAT_PDF_CHARS", DEFAULT_PDF_CHARS))
    except (TypeError, ValueError):
        pdf_budget = DEFAULT_PDF_CHARS
    history = st.session_state.setdefault(HISTORY_KEY, [])

    with (container or st).popover("💬 Ask AI about this bill", use_container_width=True):
        read_pdf = False
        if pdf_pages:
            read_pdf = st.toggle(
                "Read the statement PDF", value=True, key="ai_chat_read_pdf",
                help="Sends text from the bill PDF (page 1 plus the pages that match your "
                     f"question, up to {pdf_budget:,} characters) so the assistant can answer "
                     "about account details and itemized charges.")
        st.caption(f"Answers come from Groq ({model}) using the bill data on this page. "
                   + ("The PDF text sent includes the account number, address and full phone "
                      "numbers. " if read_pdf else
                      "Line labels are sent, not full phone numbers. ")
                   + "Check important numbers.")
        if not api_key:
            api_key = st.text_input("Groq API key", type="password", key="ai_chat_key",
                                    help=GROQ["help"])
        messages_box = st.container(height=380)
        with messages_box:
            if not history:
                st.markdown("Ask anything about this bill, for example:\n"
                            + "\n".join(f"- *{s}*" for s in SUGGESTIONS))
            for msg in history:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        question = st.chat_input("Ask about the bill...", key="ai_chat_input",
                                 disabled=not api_key)
        if history and st.button("Clear chat", key="ai_chat_clear"):
            st.session_state[HISTORY_KEY] = []
            st.rerun()

        if question:
            with messages_box:
                with st.chat_message("user"):
                    st.markdown(question)
                with st.chat_message("assistant"):
                    try:
                        statement = ""
                        if read_pdf:
                            last_user = next((m["content"] for m in reversed(history)
                                              if m["role"] == "user"), "")
                            statement = select_statement_text(
                                pdf_pages, question + " " + last_user, pdf_budget)
                        answer = st.write_stream(stream_answer(
                            api_key, model,
                            build_messages(context, history, question, statement)))
                    except LLMError as exc:
                        st.error(str(exc))
                        return
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": str(answer)}]
