# Self-correcting extraction agent for the T-Mobile "THIS BILL SUMMARY" table.
# Developer :  Devraj Gupta
# Version 1.0 Sep 2026
#
# Loop:  read PDF -> locate summary section -> LLM extracts JSON (schema enforced)
#        -> Python validates the arithmetic -> on failure, feed the exact errors back
#        to the LLM and retry (max_attempts). No regex is used anywhere.
# Output DataFrame has exactly the columns webreadBillpdf.deriveActualAmt() expects,
# so the per-line math there is reused unchanged.
from dataclasses import dataclass, field
import json

import pandas as pd
import pdfplumber
from pydantic import BaseModel, ValidationError

TOLERANCE = 0.011

BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "totals_amounts": {"type": "array", "items": {"type": "string"}},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "line_type": {"type": "string"},
                    "amounts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "line_type", "amounts"],
            },
        },
    },
    "required": ["columns", "totals_amounts", "rows"],
}

# The LLM only copies what it sees (header names + amounts in printed order);
# Python lines amounts up with columns and does the arithmetic checks.
SYSTEM_PROMPT = """You are a careful data-extraction agent for T-Mobile bill PDFs.
You receive the raw text of the "THIS BILL SUMMARY" table. Copy it into JSON:

- "columns": the money column headers in order, from the header row after "Line Type"
  (e.g. ["Plans", "Equipment", "Services", "Total"]). Multi-word headers such as
  "One-time charges" are ONE column.
- "totals_amounts": every amount on the "Totals" row, in order, exactly as printed.
- "rows": one entry for the "Account" row and one per phone line, in table order:
    label     : "Account", or the phone number exactly as printed, e.g. "(480) 567-6735".
    line_type : the words between the phone number and the first amount
                (e.g. "Voice", "Mobile Internet"); drop markers like "- New". "Account" for Account.
    amounts   : every amount on that row in order, exactly as printed, including "-"
                for empty cells. There must be one amount per column.
Do NOT put the "Totals" row in "rows". Never compute, skip or guess values.

Example input:
Line Type Plans Equipment Total
Totals $50.00 $10.00 $60.00
Account $30.00 - $30.00
(111) 222-3333 Voice $10.00 $10.00 $20.00
(444) 555-6666 - New Wearable $10.00 - $10.00

Example output:
{"columns": ["Plans", "Equipment", "Total"],
 "totals_amounts": ["$50.00", "$10.00", "$60.00"],
 "rows": [
  {"label": "Account", "line_type": "Account", "amounts": ["$30.00", "-", "$30.00"]},
  {"label": "(111) 222-3333", "line_type": "Voice", "amounts": ["$10.00", "$10.00", "$20.00"]},
  {"label": "(444) 555-6666", "line_type": "Wearable", "amounts": ["$10.00", "-", "$10.00"]}]}"""

# Bill column header -> DataFrame column. Unknown per-line charge columns are folded
# into "One-time" so they still reach the per-line total.
FIELD_KEYWORDS = [("plan", "Plans"), ("equip", "Equipment"), ("service", "Services"),
                  ("one", "One-time"), ("total", "Total")]
DF_COLUMNS = ["Phone Number", "Line Type", "Plans", "Equipment", "Services", "One-time", "Total"]


class BillRow(BaseModel):
    label: str
    line_type: str
    amounts: list[str]


class BillSummary(BaseModel):
    columns: list[str]
    totals_amounts: list[str]
    rows: list[BillRow]


@dataclass
class AgentResult:
    df: pd.DataFrame | None = None          # columns expected by deriveActualAmt
    statement_total: float = 0.0
    issues: list[str] = field(default_factory=list)
    attempts: int = 0
    steps: list[str] = field(default_factory=list)
    summary_text: str = ""

    @property
    def ok(self):
        return self.df is not None and not self.issues


# ---------------------------------------------------------------- tools
def read_pdf_text(pdf_file):
    with pdfplumber.open(pdf_file) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def locate_summary(pages):
    """Cut out the summary table with plain string search; fall back to the whole bill."""
    text = "\n".join(pages)
    start = text.find("THIS BILL SUMMARY")
    end = text.find("DETAILED CHARGES", start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        return text[start:end].strip(), True
    return text, False


def count_phone_lines(summary_text):
    """Rows starting with '(ddd)' -- used only to check the LLM didn't drop or invent lines."""
    count = 0
    for line in summary_text.splitlines():
        line = line.strip()
        if len(line) > 4 and line[0] == "(" and line[1:4].isdigit() and line[4] == ")":
            count += 1
    return count


def normalize_phone(value):
    if value.strip().lower().startswith("account"):
        return "Account"
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return value.strip()
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def clean_line_type(value, phone):
    if phone == "Account":
        return "Account"
    words = value.replace("- New", " ").replace("-New", " ").split()
    return " ".join(w for w in words if w not in ("-", "New")) or value.strip()


def parse_amount(text):
    """'$1,234.56' -> 1234.56, '-$5.00' -> -5.0, '-' / '' -> 0.0. Raises ValueError otherwise."""
    value = str(text).strip().replace("$", "").replace(",", "").replace(" ", "")
    if value in ("", "-", "--", "\u2014", "\u2013"):
        return 0.0
    if value.startswith("(") and value.endswith(")"):  # accounting style credit
        value = "-" + value[1:-1]
    return float(value)


def column_field(header):
    name = header.lower()
    for keyword, field_name in FIELD_KEYWORDS:
        if keyword in name:
            return field_name
    return "One-time"


def to_dataframe(summary):
    """Line the copied amounts up with the copied headers. Returns (df, statement_total, issues)."""
    issues = []
    fields = [column_field(c) for c in summary.columns]
    if "Total" not in fields:
        issues.append(f"No 'Total' column found in columns {summary.columns}.")

    try:
        statement_total = parse_amount(summary.totals_amounts[-1]) if summary.totals_amounts else 0.0
    except ValueError:
        statement_total = 0.0
        issues.append(f"Totals row amount {summary.totals_amounts[-1]!r} is not a number.")
    if not summary.totals_amounts:
        issues.append("totals_amounts is empty; copy the amounts on the 'Totals' row.")

    data = []
    for row in summary.rows:
        phone = normalize_phone(row.label)
        record = {"Phone Number": phone, "Line Type": clean_line_type(row.line_type, phone),
                  "Plans": 0.0, "Equipment": 0.0, "Services": 0.0, "One-time": 0.0, "Total": 0.0}
        if len(row.amounts) != len(fields):
            issues.append(f"Row {row.label}: {len(row.amounts)} amounts {row.amounts} but there are "
                          f"{len(fields)} columns {summary.columns}; include '-' for empty cells.")
        for field_name, amount in zip(fields, row.amounts):
            try:
                record[field_name] = round(record[field_name] + parse_amount(amount), 2)
            except ValueError:
                issues.append(f"Row {row.label}: amount {amount!r} is not a number.")
        data.append(record)
    return pd.DataFrame(data, columns=DF_COLUMNS), statement_total, issues


def validate_table(df, statement_total, summary_text=None):
    """Arithmetic + sanity checks done in Python. Returns a list of human-readable issues."""
    issues = []
    if df is None or df.empty:
        return ["No rows were extracted."]

    account_rows = (df["Phone Number"] == "Account").sum()
    if account_rows != 1:
        issues.append(f"Expected exactly 1 'Account' row, found {account_rows}.")

    phones = df.loc[df["Phone Number"] != "Account", "Phone Number"]
    if phones.empty:
        issues.append("No phone lines were extracted.")
    dupes = phones[phones.duplicated()].unique().tolist()
    if dupes:
        issues.append(f"Duplicate phone numbers: {dupes}.")
    bad = [p for p in phones if not (len(p) == 14 and p[0] == "(")]
    if bad:
        issues.append(f"Unrecognised phone numbers: {bad}.")

    for _, r in df.iterrows():
        parts = r["Plans"] + r["Equipment"] + r["Services"] + r["One-time"]
        if abs(parts - r["Total"]) > TOLERANCE:
            issues.append(
                f"Row {r['Phone Number']}: Plans {r['Plans']:.2f} + Equipment {r['Equipment']:.2f} "
                f"+ Services {r['Services']:.2f} + One-time {r['One-time']:.2f} = {parts:.2f}, "
                f"but Total is {r['Total']:.2f}.")

    rows_sum = df["Total"].sum()
    if abs(rows_sum - statement_total) > TOLERANCE:
        issues.append(f"Row totals add up to {rows_sum:.2f} but statement_total is {statement_total:.2f}.")

    if summary_text:
        expected_lines = count_phone_lines(summary_text)
        if expected_lines and expected_lines != len(phones):
            issues.append(f"The bill table lists {expected_lines} phone lines but {len(phones)} were extracted.")
        missing = [p for p in phones if p not in summary_text]
        if missing:
            issues.append(f"Phone numbers not present in the bill text: {missing}.")
    return issues


def to_derive_frame(df):
    """Shape expected by webreadBillpdf.deriveActualAmt(): one-time charges are per-line,
    so they are folded into Services (deriveActualAmt sums Plans+Equipment+Services)."""
    out = df.copy()
    out["Services"] = out["Services"] + out["One-time"]
    return out[["Phone Number", "Line Type", "Plans", "Equipment", "Services", "Total"]]


def balance_cents(amounts, target):
    """Spread the rounding gap (target - sum) one cent at a time, largest amounts first."""
    amounts = {k: round(float(v), 2) for k, v in amounts.items()}
    gap_cents = int(round((target - sum(amounts.values())) * 100))
    if not amounts or gap_cents == 0:
        return amounts
    step = 1 if gap_cents > 0 else -1
    order = sorted(amounts, key=lambda k: amounts[k], reverse=True)
    for i in range(abs(gap_cents)):
        person = order[i % len(order)]
        amounts[person] = round(amounts[person] + step / 100, 2)
    return amounts


# ---------------------------------------------------------------- agent loop
def run_bill_agent(pdf_file, llm, max_attempts=3, on_step=None):
    result = AgentResult()

    def log(msg):
        result.steps.append(msg)
        if on_step:
            on_step(msg)

    pages = read_pdf_text(pdf_file)
    log(f"Read {len(pages)} page(s) from the PDF.")

    summary_text, found = locate_summary(pages)
    result.summary_text = summary_text
    log("Located 'THIS BILL SUMMARY' section." if found
        else "Summary markers not found -- asking the LLM to find the table in the full bill.")

    base_prompt = "Bill summary text:\n<<<\n" + summary_text + "\n>>>"
    prompt = base_prompt
    best = None
    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        log(f"Attempt {attempt}: asking {llm.provider_name} / {llm.model} to extract the table...")
        try:
            raw = llm.extract_json(SYSTEM_PROMPT, prompt, BILL_SCHEMA)
            summary = BillSummary.model_validate(raw)
        except ValidationError as exc:
            issues, df, total = [f"Output did not match the schema: {exc.errors()[:3]}"], None, 0.0
        else:
            df, total, issues = to_dataframe(summary)
            issues += validate_table(df, total, summary_text if found else None)

        rank = (df is None, len(issues))
        if best is None or rank < best[0]:
            best = (rank, df, total, issues)
        if not issues:
            log(f"Attempt {attempt}: all validation checks passed ✓")
            break
        log(f"Attempt {attempt}: {len(issues)} issue(s) found -- sending them back to the LLM.")
        prompt = (base_prompt
                  + "\n\nYour previous answer was:\n" + json.dumps(raw)[:6000]
                  + "\n\nIt has these problems:\n- " + "\n- ".join(issues)
                  + "\n\nRe-read the table carefully and return the corrected JSON.")

    _, result.df, result.statement_total, result.issues = best
    return result
