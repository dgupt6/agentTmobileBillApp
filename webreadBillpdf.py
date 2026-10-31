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
########################################################################
import pdfplumber
import pandas as pd
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime
import streamlit as st
import json
from io import StringIO

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


def deriveActualAmt(dflocal):
    # Replace the value using the replace method
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(480) 567-6735", "Devrajx6735")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(602) 554-7693", "Devraj Spousex7693")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(623) 296-3453", "Bireswar Spousex3453")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(281) 710-7794", "Sri Spousex7794")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(713) 859-6667", "Srix6667")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(623) 628-0940", "Bireswarx0940")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(512) 906-6312", "Kaustubhx6312")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(623) 759-2902", "Atri spousex2902")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(623) 986-7834", "Atrix7834")
    dflocal["Phone Number"] = dflocal["Phone Number"].replace("(737) 287-4083", "Atri Daughterx4083")

    # Change the Kids Watch line charge from Plans to Equipment amount.
    watchCharge = dflocal.loc[dflocal["Phone Number"] == "Atri Daughterx4083","Plans"]
    dflocal.loc[dflocal["Phone Number"] == "Atri Daughterx4083","Plans"] = 0.0
    dflocal.loc[dflocal["Phone Number"] == "Atri Daughterx4083","Equipment"] = watchCharge

    ny_rows = dflocal[dflocal['Phone Number'] == 'Account']
    actlvl_charge = float(ny_rows['Total'].to_string(index=False))

    mask2 = (dflocal["Phone Number"] != "Atri Daughterx4083") & (dflocal["Phone Number"] != "Account")
    line_charges = dflocal[mask2]["Plans"].tolist()
    #line_charges = dflocal[dflocal["Phone Number"] != "Account"]["Plans"].tolist()

    sum_linecharges = 0
    num_of_line = 0
    for num_of_line, linecharge in enumerate(line_charges):
        sum_linecharges += linecharge

    num_of_line = num_of_line + 1

    # Add acct and line charge
    totalacctandlinecharge = actlvl_charge + sum_linecharges
    perhead_linecharge = round((totalacctandlinecharge / num_of_line), 2)

    # Set updated Plan amount
    mask3 = (dflocal["Phone Number"] != "Atri Daughterx4083") & (dflocal["Phone Number"] != "Account")
    dflocal.loc[mask3, "Plans"] = perhead_linecharge
    #dflocal.loc[dflocal["Phone Number"] != "Account", "Plans"] = perhead_linecharge

    dflocal["Individual amount"] = dflocal[["Plans", "Equipment", "Services"]].sum(axis=1)

    # Define a mapping for grouping
    dflocal["Person"] = dflocal["Phone Number"].apply(
        lambda x: "Devraj" if "Devraj" in x else
        "Sri" if "Sri" in x else
        "Bireswar" if "Bireswar" in x else
        "Atri" if "Atri" in x else
        "Kaustubh" if "Kaustubh" in x else
        None
    )

    # Filter out rows that don't belong to any group
    df_grouped = dflocal[dflocal["Person"].notna()]

    # Aggregate Total by Group
    resultdf = df_grouped.groupby("Person", as_index=False).agg({"Individual amount": "sum"})
    return resultdf


def getemailcontent(output):
    today = datetime.now()
    BillMonth = today.strftime("%b")
    BillYear = today.year

    if today.day > 16:
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

    Please check and pay the T-Mobile bill, due on {DueMonth} 6 {DueYear}.

    {output}

    Note: Generated through automation

    Regards,  
    Devraj Gupta
    """
    # ----------------------------------------------------------------------#

    return subject, body


def load_email_list():
    with open('config.json', 'r') as file:
        config = json.load(file)
        return config['to_address_list'], config['main_address_list']


def send_email(subject, body):
    with st.sidebar:
        st.title("Provide email and app password:")
        from_email = st.text_input("Your personal email")
        emailAppPwd = st.text_input("Your app password",type="password")

    if not emailAppPwd or not from_email:
        st.stop()

    to_address_list, main_address_list = load_email_list()

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    #msg['To'] = to_address_list
    msg['To'] = main_address_list
    msg.set_content(body)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(from_email, emailAppPwd)
        smtp.send_message(msg)

# Validate whether amount is in exceptable limit
def validateAndSend(billamtDF, totalBill, TotalfromPersons):
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
        subject, body = getemailcontent(billamt_str)
        send_email(subject, body)
        st.write("Email has been sent!")
    elif sendemailvar.upper() == 'N':
        st.write("Need further review..")


# Extract and display the consolidated bill summary
if __name__ == "__main__":
    st.title(" Welcome to T-mobile Bill Analyzer ( Devraj Gupta) ")
    filename = ""
    emailsendflag = True
    st.write("Upload the T-mobile bill file to extract and analyze.")
    uploaded_file = st.file_uploader("Choose a PDF file", type=["pdf"])
    if uploaded_file:
        consolidated_df, totalBill = extract_bill_summary(uploaded_file)
        if consolidated_df is not None:
            # emailsendflag = True
            billamtDF = deriveActualAmt(consolidated_df)
            sumofindividualamt = billamtDF['Individual amount'].sum()
            st.write(billamtDF)  # this is important print for validation. Keep it.
            st.write(
                f"Total Individual Amount : {sumofindividualamt}")  # this is important print for validation. Keep it.
            st.write(f"Total Bill Stmt Amount : {totalBill}")  # this is important print for validation. Keep it.
            validateAndSend(billamtDF, totalBill, sumofindividualamt)
        else:
            emailsendflag = False
            st.stop()
    else:
        st.stop()

    if emailsendflag == False:
        st.write("Error in generation of T-mobile bill report")
        subject = "Pay T-mobile Bill : Error"
        body = "Error in generation of bill details. Devraj is checking"
        send_email(subject, body)