# This streamlit based script reads PDF,extracts and sends email containing T-mobile bill amount per line.
# Developer :  Devraj Gupta
# Version 1.0 Jan 2025
#Cloned with Git
# In terminal do : streamlit run webreadBillpdf.py
# CTRL-C to stop the app in terminal (  mac os)
########################################################################
# -----------Mod log -------------                                     #
# Date      : Sept 2 2025                    Author: Devraj Gupta      #
# Revision 1: Added change to support new line for kids watch addition.#
# Date      : Nov 11 2026                                              #
# Revision 2: Names, phone numbers and emails moved to a billing config #
#             (secrets.toml [billing] or uploaded .toml) + onboarding. #
########################################################################
import pdfplumber
import pandas as pd
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime
import streamlit as st
from io import StringIO

from tmobile_bill_agent.bill_config import find_unmapped, normalize_phone
from tmobile_bill_agent.config_ui import billing_config_sidebar, onboarding_editor

debug_mode = False


# Function to extract bill summary from PDF
def extract_bill_summary(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        st.write("File opened successfully:")
        text = ""
        for page in pdf.pages:
            text += page.extract_text()

    # Locate the "THIS BILL SUMMARY" section
    summary_match = re.search(r"THIS BILL SUMMARY(.*?)DETAILED CHARGES", text, re.DOTALL)
    if not summary_match:
        print("'THIS BILL SUMMARY' section not found.")
        return None

    summary_text = summary_match.group(1)

    # Refined regex to extract Account and line-level details
    account_pattern = re.compile(r"Account\s+\$([\d\.]+)\s+-\s+\$([\d\.]+)\s+\$([\d\.]+)")

    #line_pattern = re.compile(
    #    r"\((\d{3})\)\s(\d{3})-(\d{4})\s(\w+)\s\$([\d\.]+)(?:\s\$([\d\.]+))?(?:\s\$([\d\.]+))?(?:\s\$([\d\.]+))?")

    line_pattern_new = re.compile(
        r"\((\d{3})\)\s(\d{3})-(\d{4})(?:\s-\sNew)?\s+([A-Za-z]+(?:\s[A-Za-z]+)*)\s\$([\d\.]+)(?:\s\$([\d\.]+))?(?:\s\$([\d\.]+))?(?:\s\$([\d\.]+))?")
    # Extract Total bill amount
    totals_pattern = re.compile(r"Totals\s+\$([\d\.]+)\s+\$([\d\.]+)\s+\$([\d\.]+)\s+\$([\d\.]+)")
    totals_match = totals_pattern.search(summary_text)
    totals_data = 0.0
    if totals_match:
        totals_data = float(totals_match.group(4)) if totals_match.group(4) else 0.0

    # Extract account-level charges
    account_match = account_pattern.search(summary_text)
    data = []
    if account_match:
        data.append([
            "Account",
            "Account",
            float(account_match.group(1)) if account_match.group(1) else 0.0,
            0.0,  # No equipment charges for account level
            float(account_match.group(2)) if account_match.group(2) else 0.0,
            float(account_match.group(3)) if account_match.group(3) else 0.0
        ])

    # Extract line-level charges
    #line_matches = line_pattern.findall(summary_text)
    line_matches = line_pattern_new.findall(summary_text)

    for match in line_matches:
        phone_number = f"({match[0]}) {match[1]}-{match[2]}"
        line_type = match[3]
        plans = float(match[4]) if match[4] else 0.0
        equipment = float(match[5]) if match[5] else 0.0
        services = float(match[6]) if match[6] else 0.0
        total = float(match[7]) if match[7] else 0.0
        data.append([phone_number, line_type, plans, equipment, services, total])

    # Convert to DataFrame
    df = pd.DataFrame(data, columns=["Phone Number", "Line Type", "Plans", "Equipment", "Services", "Total"])
    return df, totals_data


def deriveActualAmt(dflocal, config):
    """Per person amounts. config (BillingConfig) says who owns each line and whether
    its plan cost is 'shared' (pooled with the account charge and split equally) or
    'own' (the line pays exactly its own charges)."""
    lines = config.lines_by_phone
    dflocal = dflocal.copy()

    unmapped = find_unmapped(dflocal["Phone Number"], config)
    if unmapped:
        raise ValueError("Phone numbers not in the billing config: " + ", ".join(unmapped))

    is_account = dflocal["Phone Number"] == "Account"
    line_of = dflocal["Phone Number"].map(
        lambda p: None if p == "Account" else lines[normalize_phone(p)])

    # 'own' lines (e.g. a kids watch): show the plan charge as Equipment and keep
    # it out of the shared pool.
    is_own = line_of.map(lambda line: line is not None and line.split == "own")
    dflocal.loc[is_own, "Equipment"] = dflocal.loc[is_own, "Equipment"] + dflocal.loc[is_own, "Plans"]
    dflocal.loc[is_own, "Plans"] = 0.0

    actlvl_charge = float(dflocal.loc[is_account, "Total"].sum())
    is_shared = ~is_account & ~is_own
    num_of_line = int(is_shared.sum())
    if num_of_line == 0:
        if actlvl_charge:
            raise ValueError("No 'shared' lines to split the account charge across.")
    else:
        # Add acct and line charge, then split equally
        totalacctandlinecharge = actlvl_charge + float(dflocal.loc[is_shared, "Plans"].sum())
        perhead_linecharge = round((totalacctandlinecharge / num_of_line), 2)
        dflocal.loc[is_shared, "Plans"] = perhead_linecharge

    dflocal["Individual amount"] = dflocal[["Plans", "Equipment", "Services"]].sum(axis=1)
    dflocal["Person"] = line_of.map(lambda line: line.person if line else None)
    dflocal["Phone Number"] = [line.label if line else "Account" for line in line_of]

    # Aggregate Total by Person (the Account row has no person)
    df_grouped = dflocal[dflocal["Person"].notna()]
    resultdf = df_grouped.groupby("Person", as_index=False).agg({"Individual amount": "sum"})
    return resultdf


def getemailcontent(output, config):
    today = datetime.now()
    BillMonth = today.strftime("%b")
    BillYear = today.year

    if today.day > config.due_day + 10:
        due_date = (today.replace(day=1) + pd.DateOffset(months=1))
        DueMonth = due_date.strftime("%b")
        DueYear = int(due_date.year)
    else:
        DueMonth = BillMonth
        DueYear = BillYear

    subject = ('Pay T-Mobile bill for ' + BillMonth + ' ' + str(BillYear))
    # --------------------------------------------------------------------#
    body = f"""
    Hi All,

    Please check and pay the T-Mobile bill, due on {DueMonth} {config.due_day} {DueYear}.

    {output}

    Note: Generated through automation

    Regards,  
    {config.owner_name}
    """
    # ----------------------------------------------------------------------#

    return subject, body


def send_email(subject, body, recipients):
    if not recipients:
        st.error("No email addresses in the billing config: add 'emails' to the members.")
        st.stop()

    with st.sidebar:
        st.title("Provide email and app password:")
        from_email = st.text_input("Your personal email")
        emailAppPwd = st.text_input("Your app password",type="password")

    if not emailAppPwd or not from_email:
        st.stop()

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = recipients
    msg.set_content(body)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(from_email, emailAppPwd)
        smtp.send_message(msg)

# Validate whether amount is in exceptable limit
def validateAndSend(billamtDF, totalBill, TotalfromPersons, config):
    diff = abs(TotalfromPersons - totalBill)
    diff_flag = False
    if diff < 0.05:
        diff_flag = True
    else:
        st.write(f"Difference ({diff:.2f}) exceeds acceptable limits.Exiting...")

    # Save CSV logic
    savecsvvar = st.text_input("Save CSV? (Y/N): ")
    if savecsvvar.upper() == 'Y':
        csv=billamtDF.to_csv(index=False)
        st.write("Preview of the CSV file:")
        st.dataframe(billamtDF)
        csv_file = StringIO(csv)
        # Provide a download button
        st.download_button(
            label="Download CSV",
            data=csv_file.getvalue(),
            file_name="data.csv",
            mime="text/csv"
        )
        st.write("CSV file is ready to download !!")

    # Send email logic
    sendemailvar = st.text_input("Send out email? (Y/N): ")
    if sendemailvar.upper() == 'Y' and diff_flag == True:
        # Placeholder for email-sending logic
        billamt_str = billamtDF.to_string(index=False).strip()
        subject, body = getemailcontent(billamt_str, config)
        send_email(subject, body, config.recipients)
        st.write("Email has been sent!")
    elif sendemailvar.upper() == 'N':
        st.write("Need further review..")


# Extract and display the consolidated bill summary
if __name__ == "__main__":
    st.title(" Welcome to T-mobile Bill Analyzer ")
    config = billing_config_sidebar()
    st.write("Upload the T-mobile bill file to extract and analyze.")
    uploaded_file = st.file_uploader("Choose a PDF file", type=["pdf"])
    if not uploaded_file:
        st.stop()

    result = extract_bill_summary(uploaded_file)
    if result is None:
        st.error("Could not find the 'THIS BILL SUMMARY' section in this PDF.")
        if config:
            send_email("Pay T-mobile Bill : Error",
                       f"Error in generation of bill details. {config.owner_name} is checking",
                       config.recipients)
        st.stop()
    consolidated_df, totalBill = result

    if config is None or find_unmapped(consolidated_df["Phone Number"], config):
        st.subheader("Set up billing config")
        if config is not None:
            st.warning("Some phone numbers on this bill are not in the billing config.")
        onboarding_editor(consolidated_df["Phone Number"], config)
        st.stop()
    with st.expander("Edit billing config"):
        onboarding_editor(consolidated_df["Phone Number"], config, key="edit")

    billamtDF = deriveActualAmt(consolidated_df, config)
    sumofindividualamt = billamtDF['Individual amount'].sum()
    st.write(billamtDF)  # this is important print for validation. Keep it.
    st.write(
        f"Total Individual Amount : {sumofindividualamt}")  # this is important print for validation. Keep it.
    st.write(f"Total Bill Stmt Amount : {totalBill}")  # this is important print for validation. Keep it.
    validateAndSend(billamtDF, totalBill, sumofindividualamt, config)
