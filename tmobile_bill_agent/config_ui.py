# Streamlit UI for the billing config: where it is loaded from (sidebar upload or
# st.secrets) and the onboarding editor that builds one from the phone numbers on
# an uploaded bill. Nothing here is written to disk on the server.
from __future__ import annotations

import pandas as pd
import streamlit as st

from tmobile_bill_agent.bill_config import (
    DEFAULT_DUE_DAY, DEFAULT_SPLIT, SPLIT_RULES, BillingConfig, ConfigError,
    format_phone, normalize_phone, parse_config, parse_file, to_toml, to_yaml)

SESSION_KEY = "billing_config_session"   # config built in the onboarding editor


def _from_secrets():
    try:
        if "billing" in st.secrets:
            return st.secrets["billing"]
    except Exception:
        pass  # no secrets.toml at all
    return None


def billing_config_sidebar() -> BillingConfig | None:
    """Load the billing config. Precedence: built in this session > uploaded > secrets."""
    with st.sidebar:
        st.title("Billing config")
        uploaded = st.file_uploader(
            "Upload billing config (.toml / .yaml)", type=["toml", "yaml", "yml"],
            key="billing_config_upload",
            help="Same [billing] data as in .streamlit/secrets.toml, as TOML or YAML. "
                 "Read for this session only; never saved on the server.")

        if st.session_state.get(SESSION_KEY):
            st.caption("Using the config set up in this session.")
            if st.button("Discard session config"):
                st.session_state.pop(SESSION_KEY)
                st.rerun()
            config = st.session_state[SESSION_KEY]
        elif uploaded:
            try:
                config = parse_file(uploaded.name, uploaded.getvalue())
                st.caption(f"Using uploaded config: {uploaded.name}")
            except ConfigError as exc:
                st.error("Uploaded config has problems:\n- " + "\n- ".join(exc.problems))
                config = None
        elif (raw := _from_secrets()) is not None:
            try:
                config = parse_config(raw)
                st.caption("Using config from secrets.toml.")
            except ConfigError as exc:
                st.error("[billing] in secrets.toml has problems:\n- "
                         + "\n- ".join(exc.problems))
                config = None
        else:
            st.caption("No billing config yet. Upload a bill to set one up.")
            config = None
        st.divider()
    return config


def onboarding_editor(bill_phones, config: BillingConfig | None, key="onboard"):
    """Assign each phone number on the bill to a person, then download the TOML or
    use it for this session. bill_phones: phone numbers as printed on the bill."""
    existing = config.lines_by_phone if config else {}
    phones = [format_phone(p) for p in bill_phones if p != "Account"]

    st.markdown("**Who pays for each line?** Lines with the same *Person* are added "
                "together. Leave *Person* empty to see what is missing.")
    st.caption(" · ".join(f"**{name}**: {text}" for name, text in SPLIT_RULES.items()))
    lines_df = pd.DataFrame([{
        "Phone": p,
        "Label": existing[normalize_phone(p)].label if normalize_phone(p) in existing else "",
        "Person": existing[normalize_phone(p)].person if normalize_phone(p) in existing else "",
        "Split": existing[normalize_phone(p)].split if normalize_phone(p) in existing
        else DEFAULT_SPLIT,
    } for p in phones], columns=["Phone", "Label", "Person", "Split"])
    lines_df = st.data_editor(
        lines_df, key=f"{key}_lines", hide_index=True, use_container_width=True,
        disabled=["Phone"],
        column_config={"Split": st.column_config.SelectboxColumn(
            options=list(SPLIT_RULES), required=True)})
    lines_df = lines_df.fillna("")

    persons = list(dict.fromkeys(p.strip() for p in lines_df["Person"] if str(p).strip()))
    old_emails = {m.name: m.emails for m in config.members} if config else {}
    st.markdown("**Email addresses** (comma separated). Everyone listed gets the bill email.")
    members_df = st.data_editor(
        pd.DataFrame({"Person": persons,
                      "Emails": [", ".join(old_emails.get(p, [])) for p in persons]}),
        key=f"{key}_members_{'|'.join(persons)}", hide_index=True,
        use_container_width=True, disabled=["Person"])
    members_df = members_df.fillna("")

    col1, col2 = st.columns(2)
    owner_name = col1.text_input("Account owner (email signature)",
                                 config.owner_name if config else "", key=f"{key}_owner")
    due_day = col2.number_input("Bill due day of month", 1, 28,
                                config.due_day if config else DEFAULT_DUE_DAY,
                                key=f"{key}_due")

    emails = {r.Person: [e.strip() for e in str(r.Emails).split(",") if e.strip()]
              for r in members_df.itertuples()}
    unassigned = [r.Phone for r in lines_df.itertuples() if not str(r.Person).strip()]
    raw = {"owner_name": owner_name, "due_day": int(due_day), "members": [
        {"name": person, "emails": emails.get(person, []),
         "lines": [{"phone": r.Phone, "label": str(r.Label).strip(), "split": r.Split}
                   for r in lines_df.itertuples() if str(r.Person).strip() == person]}
        for person in persons]}

    problems = [f"{p} has no Person." for p in unassigned]
    try:
        new_config = parse_config(raw)
    except ConfigError as exc:
        problems += exc.problems
        new_config = None
    if problems:
        st.warning("To finish the setup:\n- " + "\n- ".join(problems))
        return

    toml_text, yaml_text = to_toml(new_config), to_yaml(new_config)
    # A toggle, not an expander: this editor is itself shown inside an expander
    # ("Edit billing config") and Streamlit does not allow nested expanders.
    if st.toggle("Preview config", key=f"{key}_preview"):
        tab_toml, tab_yaml = st.tabs(["TOML", "YAML"])
        tab_toml.code(toml_text, language="toml")
        tab_yaml.code(yaml_text, language="yaml")
    col1, col2, col3 = st.columns(3)
    col1.download_button("Download config (TOML)", toml_text, "billing_config.toml",
                         mime="application/toml", key=f"{key}_dl_toml",
                         help="Paste into .streamlit/secrets.toml (or Streamlit Cloud → "
                              "Settings → Secrets), or upload it in the sidebar next time.")
    col2.download_button("Download config (YAML)", yaml_text, "billing_config.yaml",
                         mime="application/yaml", key=f"{key}_dl_yaml",
                         help="Easier to edit by hand. Upload it in the sidebar next time. "
                              "(secrets.toml itself must be TOML.)")
    if col3.button("Use this config now", type="primary", key=f"{key}_use"):
        st.session_state[SESSION_KEY] = new_config
        st.rerun()
