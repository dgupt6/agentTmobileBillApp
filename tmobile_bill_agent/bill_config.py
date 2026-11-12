# Billing configuration: who owns which phone line, how each line's plan cost is
# split, and who receives the bill email. Pure Python (no Streamlit) so it can be
# unit tested; the Streamlit UI for it lives in config_ui.py.
#
# The config is the [billing] table of .streamlit/secrets.toml, or the same data
# uploaded in the sidebar as TOML or YAML. See .streamlit/secrets.toml.example.
from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

SPLIT_RULES = {
    "shared": "Plan cost is pooled with the account-level charge and split equally "
              "across all 'shared' lines.",
    "own": "Line pays exactly its own Plans + Equipment + Services and stays out of the "
           "shared pool (e.g. a kids watch or tablet).",
}
DEFAULT_SPLIT = "shared"
DEFAULT_DUE_DAY = 6


class ConfigError(ValueError):
    """Raised with every problem found, one per line."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Line:
    phone: str            # 10 digits, e.g. "5550001111"
    label: str            # display name, e.g. "Alice Spouse"
    person: str           # member who pays for this line
    split: str = DEFAULT_SPLIT


@dataclass
class Member:
    name: str
    emails: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)


@dataclass
class BillingConfig:
    owner_name: str
    due_day: int = DEFAULT_DUE_DAY
    members: list[Member] = field(default_factory=list)

    @property
    def lines_by_phone(self) -> dict[str, Line]:
        return {line.phone: line for m in self.members for line in m.lines}

    @property
    def recipients(self) -> list[str]:
        """Every member email, de-duplicated, in config order."""
        seen = {}
        for m in self.members:
            for email in m.emails:
                seen.setdefault(email.lower(), email)
        return list(seen.values())


# ---------------------------------------------------------------- phone numbers
def normalize_phone(value) -> str:
    """'(555) 000-1111', '555-000-1111', '+1 555 000 1111' -> '5550001111'."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def format_phone(value) -> str:
    """Format the way T-Mobile bills print it: '(555) 000-1111'."""
    d = normalize_phone(value)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else str(value)


# ---------------------------------------------------------------- parsing
def to_plain(obj):
    """st.secrets returns read-only mapping types; turn them into dict/list."""
    if isinstance(obj, Mapping):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


def _text(value, where, problems) -> str:
    """Config value as stripped text. YAML reads bare yes/no/on/off as true/false,
    so a boolean where text is expected is reported instead of becoming 'False'."""
    if isinstance(value, bool):
        problems.append(f"{where} was read as {str(value).lower()}; put it in quotes.")
        return ""
    return "" if value is None else str(value).strip()


def parse_config(data) -> BillingConfig:
    """Build a BillingConfig from the [billing] table (or a mapping that contains it)."""
    data = to_plain(data or {})
    if not isinstance(data, Mapping):
        raise ConfigError(["The config must be a table/mapping with a 'billing' section."])
    if "billing" in data:
        data = data["billing"]
    if not isinstance(data, Mapping):
        raise ConfigError(["'billing' must be a table/mapping."])
    problems = []

    owner_name = _text(data.get("owner_name", ""), "billing.owner_name", problems)
    if not owner_name:
        problems.append("billing.owner_name is missing.")

    try:
        raw_due = data.get("due_day", DEFAULT_DUE_DAY)
        due_day = int(raw_due)
        if isinstance(raw_due, bool) or not 1 <= due_day <= 28:
            raise ValueError
    except (TypeError, ValueError):
        problems.append("billing.due_day must be a whole number from 1 to 28.")
        due_day = DEFAULT_DUE_DAY

    raw_members = data.get("members") or []
    if not isinstance(raw_members, list) or not raw_members:
        problems.append("Add at least one [[billing.members]] entry.")
        raw_members = []

    members, seen_names, seen_phones = [], set(), {}
    for i, raw in enumerate(raw_members, start=1):
        if not isinstance(raw, Mapping):
            problems.append(f"Member #{i} is not a table.")
            continue
        name = _text(raw.get("name", ""), f"Member #{i} name", problems)
        if not name:
            problems.append(f"Member #{i} has no name.")
            continue
        if name.lower() in seen_names:
            problems.append(f"Member '{name}' is listed twice.")
        seen_names.add(name.lower())

        emails = raw.get("emails", [])
        if isinstance(emails, str):
            emails = [emails]
        if not isinstance(emails, list):
            problems.append(f"Member '{name}': emails must be a list.")
            emails = []
        emails = [e for e in (_text(e, f"Member '{name}' email", problems) for e in emails) if e]
        for email in emails:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                problems.append(f"Member '{name}': '{email}' is not a valid email address.")

        lines = []
        raw_lines = raw.get("lines") or []
        if not isinstance(raw_lines, list):
            problems.append(f"Member '{name}': lines must be a list.")
            raw_lines = []
        for j, raw_line in enumerate(raw_lines, start=1):
            if not isinstance(raw_line, Mapping):
                problems.append(f"Member '{name}' line #{j} is not a table.")
                continue
            phone = normalize_phone(raw_line.get("phone", ""))
            if len(phone) != 10:
                problems.append(f"Member '{name}' line #{j}: phone "
                                f"'{raw_line.get('phone', '')}' must have 10 digits.")
                continue
            if phone in seen_phones:
                problems.append(f"Phone {format_phone(phone)} is listed under both "
                                f"'{seen_phones[phone]}' and '{name}'.")
                continue
            seen_phones[phone] = name
            where = f"Phone {format_phone(phone)}"
            split = _text(raw_line.get("split", DEFAULT_SPLIT), f"{where} split",
                          problems) or DEFAULT_SPLIT
            if split not in SPLIT_RULES:
                problems.append(f"Phone {format_phone(phone)}: split '{split}' must be one of "
                                f"{', '.join(SPLIT_RULES)}.")
            label = (_text(raw_line.get("label", ""), f"{where} label", problems)
                     or f"{name} x{phone[-4:]}")
            lines.append(Line(phone=phone, label=label, person=name, split=split))
        members.append(Member(name=name, emails=emails, lines=lines))

    if problems:
        raise ConfigError(problems)
    return BillingConfig(owner_name=owner_name, due_day=due_day, members=members)


def parse_toml(text) -> BillingConfig:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"Not valid TOML: {exc}"]) from exc
    return parse_config(data)


def parse_yaml(text) -> BillingConfig:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        data = yaml.safe_load(text)   # safe_load: never builds arbitrary Python objects
    except yaml.YAMLError as exc:
        raise ConfigError([f"Not valid YAML: {exc}"]) from exc
    return parse_config(data)


def parse_file(filename, content) -> BillingConfig:
    """Pick the parser from the file extension: .toml, .yaml or .yml."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("yaml", "yml"):
        return parse_yaml(content)
    if ext == "toml":
        return parse_toml(content)
    raise ConfigError([f"Unsupported config file '{filename}': use .toml, .yaml or .yml."])


# ---------------------------------------------------------------- writing
def to_toml(config: BillingConfig) -> str:
    """Serialize to the [billing] TOML block (paste into secrets.toml or upload)."""
    q = json.dumps  # JSON string escapes are valid TOML basic-string escapes
    out = ["[billing]",
           f"owner_name = {q(config.owner_name)}",
           f"due_day = {config.due_day}",
           ""]
    for m in config.members:
        out += ["[[billing.members]]",
                f"name = {q(m.name)}",
                f"emails = [{', '.join(q(e) for e in m.emails)}]",
                "lines = ["]
        for line in m.lines:
            out.append(f"  {{ phone = {q(format_phone(line.phone))}, label = {q(line.label)}, "
                       f"split = {q(line.split)} }},")
        out += ["]", ""]
    return "\n".join(out)


def to_yaml(config: BillingConfig) -> str:
    """Serialize to YAML (for uploading or hand-editing; secrets.toml needs TOML).
    Every text value is double-quoted so YAML never re-reads it as a bool or number."""
    q = json.dumps  # a JSON string is a valid YAML double-quoted scalar
    out = ["billing:",
           f"  owner_name: {q(config.owner_name)}",
           f"  due_day: {config.due_day}",
           "  members:"]
    for m in config.members:
        out += [f"    - name: {q(m.name)}",
                f"      emails: [{', '.join(q(e) for e in m.emails)}]",
                "      lines:" + ("" if m.lines else " []")]
        for line in m.lines:
            out.append(f"        - {{phone: {q(format_phone(line.phone))}, "
                       f"label: {q(line.label)}, split: {q(line.split)}}}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- bill checks
def find_unmapped(phone_numbers, config: BillingConfig | None) -> list[str]:
    """Phone numbers on the bill that the config does not assign to anyone."""
    known = config.lines_by_phone if config else {}
    return [p for p in phone_numbers
            if p != "Account" and normalize_phone(p) not in known]
