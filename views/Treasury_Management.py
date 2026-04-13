import streamlit as st
import pandas as pd
import plotly.express as px
from utils import get_supabase, get_admin, load_committees_df, load_committee_budgets_df, load_transactions_df, load_terms_df
from components import animated_typing_title, apply_nav_title
from datetime import datetime
import re

from views.treasury_parse_utils import (
    merge_legacy_and_enhanced_auto_cat,
    numeric_amount,
)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def check_duplicate_transactions(records: list, existing_transactions: pd.DataFrame) -> tuple[list, list]:
    """Check for duplicates by comparing details and dates."""
    # Ensure transaction_date is datetime before converting to string
    if not existing_transactions.empty:
        existing_transactions = existing_transactions.copy()
        existing_transactions['transaction_date'] = pd.to_datetime(existing_transactions['transaction_date'], errors='coerce')
        existing_transactions['date_str'] = existing_transactions['transaction_date'].dt.strftime('%Y-%m-%d')
    else:
        existing_transactions['date_str'] = []
    
    non_duplicates, duplicates = [], []
    
    for record in records:
        matches = existing_transactions[
            (existing_transactions['details'].str.strip() == record['details'].strip()) &
            (existing_transactions['date_str'] == record['transaction_date'])
        ]
        
        if matches.empty:
            non_duplicates.append(record)
        else:
            match = matches.iloc[0]
            record['existing_id'] = int(match['transactionid'])
            duplicates.append(record)
    
    return non_duplicates, duplicates

def show_committee_reference():
    """Display Committee ID reference table."""
    st.markdown("""
    #### 🔑 Committee ID Reference
    | ID | Committee | ID | Committee |
    |----|-----------|-----|-----------|
    | 1 | Dues | 10 | Professional Development |
    | 2 | Treasury | 11 | Sponsorship / Donation |
    | 3 | Transfers | 12 | Overhead |
    | 4 | President | 13 | Merch |
    | 5 | Membership | 14 | Road Trip |
    | 6 | Corporate Relations | 15 | Technology |
    | 7 | Consulting | 16 | Passport |
    | 8 | Meeting Food | 17 | Refunded |
    | 9 | Marketing | 18 | Formal |
    """)

def map_purpose_to_budget_id(purpose: str) -> int | None:
    """Map purpose to committee ID for legacy insert paths."""
    PURPOSE_MAP = {
        "Dues": 1,
        "Refunded": 17,
        "Formal": 18,
        "Meeting Food": 8,
        "Food & Drink": 5,
        "Professional Development": 7,
    }
    if not purpose or pd.isna(purpose):
        return None
    return PURPOSE_MAP.get(str(purpose).strip())


def prepare_transaction_records(df_proc: pd.DataFrame, df_committees: pd.DataFrame) -> list:
    """Convert processed dataframe to transaction records with proper budget mapping."""
    records = []
    for _, r in df_proc.iterrows():
        # Auto-map Dues purpose to budget ID 1
        mapped_budget = map_purpose_to_budget_id(r.get('purpose'))
        
        records.append({
            'transaction_date': r['transactiondate'].strftime('%Y-%m-%d') if pd.notna(r['transactiondate']) else None,
            'amount': float(r['amount']) if pd.notna(r['amount']) else 0.0,
            'details': str(r['details']) if pd.notna(r['details']) else '',
            'purpose': r['purpose'] if pd.notna(r['purpose']) else None,
            'account': r['account'],
            'budget_category': mapped_budget
        })
    
    return records

def insert_transactions_with_duplicate_check(records: list, filename: str, supabase, key_prefix: str):
    """Insert transactions after checking for duplicates."""
    if not records:
        st.info("No records to insert.")
        return
    
    existing_transactions = load_transactions_df()
    non_duplicates, duplicates = check_duplicate_transactions(records, existing_transactions)
    
    if duplicates:
        st.warning(f"⚠️ Found {len(duplicates)} duplicate transactions that will be skipped:")
        dup_df = pd.DataFrame(duplicates)
        st.dataframe(
            dup_df[['transaction_date', 'amount', 'details', 'existing_id']].rename(
                columns={'existing_id': 'Existing Transaction ID'}
            ),
            hide_index=True
        )
        records = non_duplicates
        
        if not records:
            st.info("All transactions were duplicates. Nothing to insert.")
            return
    
    # Show new rows that will be uploaded
    st.success(f"✅ Ready to upload {len(records)} new transactions:")
    new_df = pd.DataFrame(records)
    # Format for display
    display_new_df = new_df.copy()
    display_new_df['amount'] = display_new_df['amount'].apply(lambda x: f"${x:,.2f}")
    
    # Load committees to map budget_category IDs to "ID - Name" format
    df_committees = load_committees_df()
    committee_map = {int(row['CommitteeID']): f"{int(row['CommitteeID'])} - {row['Committee_Name']}" 
                     for _, row in df_committees.iterrows()}
    
    # Map budget_category to readable format (e.g., "1 - Dues")
    display_new_df['budget'] = display_new_df['budget_category'].apply(
        lambda x: committee_map.get(int(x), '') if pd.notna(x) else ''
    )
    
    st.dataframe(
        display_new_df[['transaction_date', 'amount', 'details', 'budget', 'purpose', 'account']].rename(
            columns={
                'transaction_date': 'Date',
                'amount': 'Amount',
                'details': 'Details',
                'budget': 'Budget',
                'purpose': 'Purpose',
                'account': 'Account'
            }
        ),
        hide_index=True,
        use_container_width=True
    )
    
    # Confirmation button - use regular button since this function is called outside form context
    if st.button(f"🔒 Confirm Upload {len(records)} Transactions", key=f"{key_prefix}_confirm_upload"):
        try:
            admin_client = None
            try:
                admin_client = get_admin()
            except:
                pass
            
            client = admin_client or supabase
            client.table('transactions').insert(records).execute()
            client.table('uploaded_files').insert({'file_name': filename}).execute()
            
            # Clear the ready_to_upload flag immediately after successful insert
            st.session_state[f'{key_prefix}_ready_to_upload'] = False
            if f'{key_prefix}_records' in st.session_state:
                del st.session_state[f'{key_prefix}_records']
            if f'{key_prefix}_filename' in st.session_state:
                del st.session_state[f'{key_prefix}_filename']
            
            st.success(f"✅ Successfully uploaded {len(records)} transactions!")
            if duplicates:
                st.info(f"Skipped {len(duplicates)} duplicate transactions.")
            
            st.cache_data.clear()
            st.balloons()
            st.rerun()
        except Exception as e:
            msg = str(e)
            if 'row-level security' in msg.lower() or '42501' in msg:
                st.error("❌ Insert blocked by Row-Level Security. Add service_role key to secrets or update RLS policies.")
            else:
                st.error(f"❌ Failed to insert: {e}")

# ============================================================================
# AUTHENTICATION
# ============================================================================

def check_treasury_password():
    """Check treasury password authentication."""
    if "treasury_authenticated" not in st.session_state:
        st.session_state.treasury_authenticated = False
    
    if not st.session_state.treasury_authenticated:
        st.warning("🔒 Treasury Access Required")
        password = st.text_input("Enter Treasury Password", type="password")
        
        if st.button("Access Treasury Portal"):
            treasury_password = st.secrets.get("treasury", {}).get("password", "default_password")
            
            if password == treasury_password:
                st.session_state.treasury_authenticated = True
                st.success("✅ Access granted!")
                st.rerun()
            else:
                st.error("❌ Incorrect password.")
                return False
    
    return st.session_state.treasury_authenticated

# ============================================================================
# MAIN UI
# ============================================================================

apply_nav_title()
animated_typing_title("Treasury Management Portal")
st.divider()

if not check_treasury_password():
    st.stop()

st.success("🎯 Welcome to the Treasury Management Portal")

# Initialize
supabase = get_supabase()

@st.cache_data
def load_treasury_data():
    return (load_committees_df(), load_committee_budgets_df(), 
            load_transactions_df(), load_terms_df())

df_committees, df_budgets, df_transactions, df_terms = load_treasury_data()

# Sidebar
st.sidebar.header("🛠️ Treasury Tools")
page = st.sidebar.selectbox(
    "Select Tool",
    ["📊 Data Overview", "📤 Upload Transactions", "📅 Manage Terms", 
     "💰 Manage Budgets", "🔧 Database Tools"]
)

# ============================================================================
# PAGE: DATA OVERVIEW
# ============================================================================

if page == "📊 Data Overview":
    st.header("📊 Treasury Data Overview")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Transactions", f"{len(df_transactions):,}")
    with col2:
        total_income = df_transactions[df_transactions["amount"] > 0]["amount"].sum()
        st.metric("Total Income", f"${total_income:,.2f}")
    with col3:
        total_expenses = abs(df_transactions[df_transactions["amount"] < 0]["amount"].sum())
        st.metric("Total Expenses", f"${total_expenses:,.2f}")
    
    st.divider()
    st.subheader("Recent Activity")
    recent_txns = df_transactions.sort_values("transaction_date", ascending=False).head(10)
    if not recent_txns.empty:
        st.dataframe(
            recent_txns[["transaction_date", "amount", "details", "purpose"]]
            .rename(columns={"transaction_date": "Date", "amount": "Amount", 
                           "details": "Details", "purpose": "Purpose"})
            .style.format({"Amount": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True
        )

# ============================================================================
# PAGE: UPLOAD TRANSACTIONS
# ============================================================================

elif page == "📤 Upload Transactions":
    st.header("📤 Upload Transaction Data")
    st.info("Upload Venmo or Checking statements. Filenames should include 'VenmoStatement_' or 'checking'.")
    
    show_committee_reference()
    
    # Initialize active tab in session state if not present
    if 'active_upload_tab' not in st.session_state:
        st.session_state.active_upload_tab = 0
    
    # Create tabs with session state to maintain active tab
    tab_venmo, tab_checking = st.tabs(["Venmo", "Checking"])
    
    # ========== VENMO TAB ==========
    with tab_venmo:
        venmo_uploaded_file = st.file_uploader("Upload Venmo statement (Excel/CSV)", 
                                        type=["xlsx", "xls", "csv"], key="venmo_upload")
        if venmo_uploaded_file:
            venmo_filename = venmo_uploaded_file.name
            if 'venmostatement' not in venmo_filename.lower():
                st.error("Filename should include 'VenmoStatement_'.")
            else:
                existing = supabase.table("uploaded_files").select("*").eq("file_name", venmo_filename).execute()
                if existing.data:
                    st.warning("File already uploaded.")
                else:
                    try:
                        df_raw = pd.read_csv(venmo_uploaded_file) if venmo_filename.lower().endswith('.csv') else pd.read_excel(venmo_uploaded_file)
                        
                        # Find columns
                        df_cols = {c.lower().strip().replace('\xa0', ' '): c for c in df_raw.columns}
                        date_col = next((c for k, c in df_cols.items() if 'date' in k), None)
                        note_col = next((c for k, c in df_cols.items() if 'note' in k), None)
                        amount_col = next((c for k, c in df_cols.items() if 'amount' in k and 'total' in k), None)
                        if not amount_col:
                            amount_col = next((c for k, c in df_cols.items() if 'amount' in k), None)
                        
                        if not date_col or not amount_col:
                            st.error("Missing required columns (date, amount).")
                        else:
                            # Find additional columns for robust duplicate checking
                            transaction_id_col = next((c for k, c in df_cols.items() if 'transaction' in k and 'id' in k), None)
                            from_col = next((c for k, c in df_cols.items() if k == 'from'), None)
                            to_col = next((c for k, c in df_cols.items() if k == 'to'), None)
                            
                            # Build robust details column from Transaction ID, Note, From, To
                            details_parts = []
                            if transaction_id_col:
                                details_parts.append(df_raw[transaction_id_col].fillna('').astype(str))
                            if note_col:
                                details_parts.append(df_raw[note_col].fillna('').astype(str))
                            if from_col:
                                details_parts.append(df_raw[from_col].fillna('').astype(str))
                            if to_col:
                                details_parts.append(df_raw[to_col].fillna('').astype(str))
                            
                            # Combine all parts with separator
                            if details_parts:
                                combined_details = details_parts[0]
                                for part in details_parts[1:]:
                                    combined_details = combined_details + ' | ' + part
                            else:
                                combined_details = ''
                            
                            df_proc = pd.DataFrame({
                                'transactiondate': pd.to_datetime(df_raw[date_col], errors='coerce').dt.date,
                                'amount': df_raw[amount_col].apply(numeric_amount),
                                'details': combined_details,
                                'budget': '',
                                'account': 'Venmo'
                            })

                            # Filter out footer rows (like "Account Statement - (@UFAIS)")
                            df_proc = df_proc[
                                ~df_proc['details'].str.lower().str.contains('account statement', na=False, regex=False)
                            ].reset_index(drop=True)

                            # Auto-classify (legacy dues keywords + enhanced rules)
                            df_proc = merge_legacy_and_enhanced_auto_cat(df_proc)
                            
                            st.subheader("Preview and Edit")
                            
                            # Display with formatted values
                            display_df = df_proc.copy()
                            display_df["transactiondate"] = display_df["transactiondate"].apply(
                                lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else ""
                            )
                            display_df["amount"] = display_df["amount"].apply(
                                lambda x: f"${x:,.2f}" if pd.notna(x) else ""
                            )
                            
                            purpose_options = sorted([
                                "Dues", "Food & Drink", "Tax", "Road Trip", "Social Events",
                                "Sponsorship / Donation", "Travel Reimbursement", "Transfers",
                                "Merch", "Professional Events", "Misc.", "ISOM Passport",
                                "GBM Catering", "Formal", "Refunded", "Meeting Food",
                                "Technology", "Marketing", "Professional Development"
                            ])
                            
                            with st.form("venmo_form"):
                                # Reorder columns for display
                                display_df_ordered = display_df[["transactiondate", "amount", "details", "budget", "purpose", "account"]]
                                
                                # Create committee ID options with labels
                                committee_id_options = [""] + [
                                    "1 - Dues", "2 - Treasury", "3 - Transfers", "4 - President",
                                    "5 - Membership", "6 - Corporate Relations", "7 - Consulting",
                                    "8 - Meeting Food", "9 - Marketing", "10 - Professional Development",
                                    "11 - Sponsorship / Donation", "12 - Overhead", "13 - Merch",
                                    "14 - Road Trip", "15 - Technology", "16 - Passport",
                                    "17 - Refunded", "18 - Formal"
                                ]
                                
                                edited_df = st.data_editor(
                                    display_df_ordered,
                                    column_config={
                                        "transactiondate": st.column_config.TextColumn("Date", disabled=True),
                                        "amount": st.column_config.TextColumn("Amount", disabled=True),
                                        "details": st.column_config.TextColumn("Details", disabled=True),
                                        "budget": st.column_config.SelectboxColumn(
                                            "Committee ID",
                                            options=committee_id_options,
                                            required=False,
                                            help="Select the committee ID (see reference above)"
                                        ),
                                        "purpose": st.column_config.SelectboxColumn(
                                            "Purpose", options=[""] + purpose_options, required=False
                                        ),
                                        "account": st.column_config.TextColumn("Account", disabled=True)
                                    },
                                    hide_index=True,
                                    key="venmo_editor"
                                )
                                
                                if st.form_submit_button("Process and Insert Venmo Transactions"):
                                    # Update df_proc with edited values
                                    for idx, row in edited_df.iterrows():
                                        if row['purpose']:
                                            df_proc.at[idx, 'purpose'] = row['purpose']
                                        if row['budget']:
                                            df_proc.at[idx, 'budget'] = row['budget']
                                    
                                    # Store in session state for confirmation step
                                    st.session_state.venmo_ready_to_upload = True
                                    st.session_state.venmo_records = []
                                    
                                    for _, r in df_proc.iterrows():
                                        budget_id = None
                                        if r['budget'] and str(r['budget']).strip():
                                            budget_str = str(r['budget']).strip()
                                            try:
                                                if '-' in budget_str:
                                                    budget_id = int(budget_str.split('-')[0].strip())
                                                else:
                                                    budget_id = int(budget_str)
                                            except Exception:
                                                pass
                                        if budget_id is None:
                                            budget_id = map_purpose_to_budget_id(r.get('purpose'))
                                        
                                        st.session_state.venmo_records.append({
                                            'transaction_date': r['transactiondate'].strftime('%Y-%m-%d') if pd.notna(r['transactiondate']) else None,
                                            'amount': float(r['amount']) if pd.notna(r['amount']) else 0.0,
                                            'details': str(r['details']) if pd.notna(r['details']) else '',
                                            'purpose': r['purpose'] if pd.notna(r['purpose']) else None,
                                            'account': r['account'],
                                            'budget_category': budget_id
                                        })
                                    
                                    st.session_state.venmo_filename = venmo_filename
                                    st.rerun()

                            # Outside the form - check if ready to upload
                            if st.session_state.get('venmo_ready_to_upload', False):
                                insert_transactions_with_duplicate_check(
                                    st.session_state.venmo_records, 
                                    st.session_state.venmo_filename, 
                                    supabase, 
                                    "venmo"
                                )
                    
                    except Exception as e:
                        st.error(f"Error processing file: {e}")
    
    # ========== CHECKING TAB ==========
    with tab_checking:
        checking_uploaded_file = st.file_uploader("Upload Checking/Wells Fargo statement (CSV/Excel)", 
                                        type=["xlsx", "xls", "csv"], key="checking_upload")
        if checking_uploaded_file:
            checking_filename = checking_uploaded_file.name
            if 'checking' not in checking_filename.lower():
                st.error("Filename should include 'checking'.")
            else:
                existing = supabase.table("uploaded_files").select("*").eq("file_name", checking_filename).execute()
                if existing.data:
                    st.warning("File already uploaded.")
                else:
                    try:
                        df_raw = pd.read_csv(checking_uploaded_file, header=None) if checking_filename.lower().endswith('.csv') else pd.read_excel(checking_uploaded_file, header=None)
                        
                        if df_raw.shape[1] < 3:
                            st.error("File doesn't have expected columns.")
                        else:
                            df_proc = pd.DataFrame({
                                'transactiondate': pd.to_datetime(df_raw.iloc[:,0], errors='coerce').dt.date,
                                'amount': df_raw.iloc[:,1].apply(numeric_amount),
                                'details': (df_raw.iloc[:,4] if df_raw.shape[1] > 4 else df_raw.iloc[:,-1]).astype(str),
                                'budget': '',
                                'account': 'Wells'
                            })
                            
                            df_proc = merge_legacy_and_enhanced_auto_cat(df_proc)
                            
                            st.subheader("Preview and Edit")
                            
                            display_df = df_proc.copy()
                            display_df["transactiondate"] = display_df["transactiondate"].apply(
                                lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else ""
                            )
                            display_df["amount"] = display_df["amount"].apply(
                                lambda x: f"${x:,.2f}" if pd.notna(x) else ""
                            )
                            
                            purpose_options = sorted([
                                "Dues", "Food & Drink", "Tax", "Road Trip", "Social Events",
                                "Sponsorship / Donation", "Travel Reimbursement", "Transfers",
                                "Merch", "Professional Events", "Misc.", "ISOM Passport",
                                "GBM Catering", "Formal", "Refunded", "Meeting Food",
                                "Technology", "Marketing", "Professional Development"
                            ])
                            
                            with st.form("checking_form"):
                                    # Reorder columns for display
                                    display_df_ordered = display_df[["transactiondate", "amount", "details", "budget", "purpose", "account"]]
                                    
                                    # Create committee ID options with labels
                                    committee_id_options = [""] + [
                                        "1 - Dues", "2 - Treasury", "3 - Transfers", "4 - President",
                                        "5 - Membership", "6 - Corporate Relations", "7 - Consulting",
                                        "8 - Meeting Food", "9 - Marketing", "10 - Professional Development",
                                        "11 - Sponsorship / Donation", "12 - Overhead", "13 - Merch",
                                        "14 - Road Trip", "15 - Technology", "16 - Passport",
                                        "17 - Refunded", "18 - Formal"
                                    ]
                                    
                                    edited_df = st.data_editor(
                                        display_df_ordered,
                                        column_config={
                                            "transactiondate": st.column_config.TextColumn("Date", disabled=True),
                                            "amount": st.column_config.TextColumn("Amount", disabled=True),
                                            "details": st.column_config.TextColumn("Details", disabled=True),
                                            "budget": st.column_config.SelectboxColumn(
                                                "Committee ID",
                                                options=committee_id_options,
                                                required=False,
                                                help="Select the committee ID (see reference above)"
                                            ),
                                            "purpose": st.column_config.SelectboxColumn(
                                                "Purpose", options=[""] + purpose_options, required=False
                                            ),
                                            "account": st.column_config.TextColumn("Account", disabled=True)
                                        },
                                        hide_index=True,
                                        key="checking_editor"
                                    )
                                
                                    if st.form_submit_button("Process and Insert Checking Transactions"):
                                        # Update df_proc with edited values
                                        for idx, row in edited_df.iterrows():
                                            if row['purpose']:
                                                df_proc.at[idx, 'purpose'] = row['purpose']
                                            if row['budget']:
                                                df_proc.at[idx, 'budget'] = row['budget']
                                        
                                        # Store in session state for confirmation step
                                        st.session_state.checking_ready_to_upload = True
                                        st.session_state.checking_records = []
                                        
                                        for _, r in df_proc.iterrows():
                                            budget_id = None
                                            if r['budget'] and str(r['budget']).strip():
                                                budget_str = str(r['budget']).strip()
                                                try:
                                                    if '-' in budget_str:
                                                        budget_id = int(budget_str.split('-')[0].strip())
                                                    else:
                                                        budget_id = int(budget_str)
                                                except Exception:
                                                    pass
                                            if budget_id is None:
                                                budget_id = map_purpose_to_budget_id(r.get('purpose'))
                                            
                                            st.session_state.checking_records.append({
                                                'transaction_date': r['transactiondate'].strftime('%Y-%m-%d') if pd.notna(r['transactiondate']) else None,
                                                'amount': float(r['amount']) if pd.notna(r['amount']) else 0.0,
                                                'details': str(r['details']) if pd.notna(r['details']) else '',
                                                'purpose': r['purpose'] if pd.notna(r['purpose']) else None,
                                                'account': r['account'],
                                                'budget_category': budget_id
                                            })
                                        
                                        st.session_state.checking_filename = checking_filename
                                        st.rerun()

                            # Outside the form - check if ready to upload
                            if st.session_state.get('checking_ready_to_upload', False):
                                insert_transactions_with_duplicate_check(
                                    st.session_state.checking_records, 
                                    st.session_state.checking_filename, 
                                    supabase, 
                                    "checking"
                                )
                                                        
                    except Exception as e:
                        st.error(f"Error reading file: {e}")

# ============================================================================
# PAGE: MANAGE TERMS
# ============================================================================

elif page == "📅 Manage Terms":
    st.header("📅 Manage Academic Terms")
    
    st.subheader("Current Terms")
    if not df_terms.empty:
        st.dataframe(
            df_terms[["TermID", "Semester", "start_date", "end_date"]]
            .sort_values("start_date", ascending=False),
            use_container_width=True,
            hide_index=True
        )
    
    st.divider()
    st.subheader("Add New Term")
    
    col1, col2 = st.columns(2)
    with col1:
        term_id = st.text_input("Term ID (e.g., FA25, SP26)")
        semester = st.text_input("Semester Name (e.g., Fall 2024)")
        
        if semester:
            semester_lower = semester.lower()
            season_valid = any(semester_lower.startswith(s) for s in ["fall", "spring", "summer", "winter"])
            year_valid = re.search(r'\b(19|20)\d{2}\b', semester) is not None
            
            if not season_valid:
                st.error("❌ Start with: Fall, Spring, Summer, or Winter")
            elif not year_valid:
                st.error("❌ Include a 4-digit year")
            elif semester != semester.title():
                st.warning("⚠️ Consider proper capitalization")
            else:
                st.success("✅ Valid format!")
    
    with col2:
        start_date = st.date_input("Start Date")
        end_date = st.date_input("End Date")
    
    if st.button("➕ Add Term"):
        if term_id and semester and start_date and end_date:
            semester_lower = semester.lower()
            season_valid = any(semester_lower.startswith(s) for s in ["fall", "spring", "summer", "winter"])
            year_valid = re.search(r'\b(19|20)\d{2}\b', semester) is not None
            
            if not season_valid or not year_valid:
                st.error("❌ Invalid semester format")
            else:
                try:
                    term_data = {
                        "TermID": term_id,
                        "Semester": semester.title(),
                        "start_date": start_date.strftime("%Y-%m-%d"),
                        "end_date": end_date.strftime("%Y-%m-%d")
                    }
                    
                    existing = supabase.table("terms").select("*").eq("TermID", term_id).execute()
                    if existing.data:
                        st.warning(f"Term {term_id} already exists!")
                    else:
                        supabase.table("terms").insert(term_data).execute()
                        st.success(f"✅ Term {term_id} added!")
                        st.cache_data.clear()
                        st.rerun()
                except Exception as e:
                    st.error(f"❌ Error: {e}")

# ============================================================================
# PAGE: MANAGE BUDGETS
# ============================================================================

elif page == "💰 Manage Budgets":
    st.header("💰 Manage Committee Budgets")
    
    st.subheader("Current Budgets")
    current_terms = df_terms.sort_values("start_date", ascending=False)
    
    if not current_terms.empty:
        selected_term = st.selectbox(
            "Select Term",
            current_terms["TermID"].tolist(),
            format_func=lambda x: f"{x} - {current_terms[current_terms['TermID'] == x]['Semester'].iloc[0]}"
        )
        
        term_budgets = df_budgets[df_budgets["termid"] == selected_term].copy()
        
        allowed_committees = {
            "consulting", "corporate relations", "marketing", "meeting food",
            "membership", "merch", "overhead", "passport", "president",
            "professional development", "treasury"
        }
        
        committees_df = df_committees.copy()
        committees_df["_name_lower"] = committees_df["Committee_Name"].str.lower()
        allowed_committees_df = committees_df[committees_df["_name_lower"].isin(allowed_committees)].copy()
        allowed_committee_ids = allowed_committees_df["CommitteeID"].tolist()
        
        if not term_budgets.empty:
            term_budgets = term_budgets.merge(
                df_committees[["CommitteeID", "Committee_Name"]], 
                left_on="committeeid", right_on="CommitteeID", how="left"
            )
            term_budgets = term_budgets[term_budgets["committeeid"].isin(allowed_committee_ids)]
            
            display_budgets = (
                allowed_committees_df[["CommitteeID", "Committee_Name"]]
                .merge(term_budgets[["committeeid", "budget_amount"]], 
                      left_on="CommitteeID", right_on="committeeid", how="left")
                .fillna({"budget_amount": 0.0})
            )
            st.dataframe(
                display_budgets[["Committee_Name", "budget_amount"]]
                .rename(columns={"Committee_Name": "Committee", "budget_amount": "Budget"}),
                use_container_width=True,
                hide_index=True
            )
        else:
            semester_name = current_terms[current_terms['TermID'] == selected_term]['Semester'].iloc[0]
            st.info(f"No budgets for {semester_name}")
        
        st.divider()
        st.subheader("Set Committee Budgets")
        
        committees = allowed_committees_df["Committee_Name"].tolist()
        committee_ids = allowed_committees_df[["CommitteeID", "Committee_Name"]]
        
        budget_inputs = {}
        col1, col2 = st.columns(2)
        
        for i, committee in enumerate(committees):
            existing_budget = 0.0
            if not term_budgets.empty:
                committee_id = committee_ids[committee_ids["Committee_Name"] == committee]["CommitteeID"].iloc[0]
                matching = term_budgets[term_budgets["committeeid"] == committee_id]
                if not matching.empty:
                    existing_budget = float(matching["budget_amount"].iloc[0])
            
            with col1 if i % 2 == 0 else col2:
                budget_inputs[committee] = st.number_input(
                    f"{committee} Budget",
                    min_value=0.0,
                    value=existing_budget,
                    step=100.0,
                    format="%.2f"
                )
        
        if st.button("💾 Save Budgets"):
            try:
                supabase.table("committeebudgets").delete().eq("termid", selected_term).in_("committeeid", allowed_committee_ids).execute()
                
                for committee_name, budget_amount in budget_inputs.items():
                    committee_id = committee_ids[committee_ids["Committee_Name"] == committee_name]["CommitteeID"].iloc[0]
                    
                    supabase.table("committeebudgets").insert({
                        "termid": selected_term,
                        "committeeid": int(committee_id),
                        "budget_amount": float(budget_amount)
                    }).execute()
                
                st.success("✅ Budgets saved!")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error: {e}")

# ============================================================================
# PAGE: DATABASE TOOLS
# ============================================================================

elif page == "🔧 Database Tools":
    st.header("🔧 Database Management Tools")
    
    st.subheader("Data Export")
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("📥 Export Transactions"):
            csv = df_transactions.to_csv(index=False)
            st.download_button(
                "Download CSV",
                csv,
                f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                "text/csv"
            )
    
    with col2:
        if st.button("📥 Export Budgets"):
            csv = df_budgets.to_csv(index=False)
            st.download_button(
                "Download CSV",
                csv,
                f"budgets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                "text/csv"
            )
    
    with col3:
        if st.button("📥 Export Terms"):
            csv = df_terms.to_csv(index=False)
            st.download_button(
                "Download CSV",
                csv,
                f"terms_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                "text/csv"
            )
    
    st.divider()
    st.subheader("Database Statistics")
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Terms", len(df_terms))
    with col2:
        st.metric("Total Committees", len(df_committees))
    with col3:
        st.metric("Total Budgets", len(df_budgets))
    with col4:
        st.metric("Total Transactions", len(df_transactions))
    
    st.subheader("Data Validation")
    orphaned_budgets = df_budgets[~df_budgets["committeeid"].isin(df_committees["CommitteeID"])]
    orphaned_transactions = df_transactions[~df_transactions["budget_category"].isin(df_committees["CommitteeID"])]
    
    if not orphaned_budgets.empty:
        st.warning(f"⚠️ Found {len(orphaned_budgets)} orphaned budget records")
    if not orphaned_transactions.empty:
        st.warning(f"⚠️ Found {len(orphaned_transactions)} orphaned transaction records")
    if orphaned_budgets.empty and orphaned_transactions.empty:
        st.success("✅ No data integrity issues found")

# Logout
st.sidebar.divider()
if st.sidebar.button("🚪 Logout from Treasury"):
    st.session_state.treasury_authenticated = False
    st.rerun()