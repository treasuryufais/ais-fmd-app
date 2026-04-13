# UF AIS Financial Management System

A modern, streamlined financial management application for the University of Florida's Association for Information Systems (AIS) organization.

## Features

### Consolidated Financial Dashboard
- **Single Dashboard**: All committee financials in one place with powerful filtering
- **Interactive Filters**: Filter by semester, committee, and date range
- **Real-time Metrics**: Key financial indicators at a glance
- **Visual Analytics**: Beautiful charts and graphs for budget vs spending analysis
- **Export Capabilities**: Download filtered data as CSV

### Treasury Management Portal
- **Password Protected**: Secure access for treasury committee members only
- **Excel Upload**: Bulk upload transaction data via Excel files
- **Term Management**: Add and manage academic terms
- **Budget Management**: Set and update committee budgets
- **Database Tools**: Export data and validate database integrity

## Technology Stack

- **Frontend**: Streamlit
- **Backend**: Python
- **Database**: Supabase (PostgreSQL)
- **Data Processing**: Pandas
- **Visualization**: Plotly

## Prerequisites
- Python 3.8+
- Supabase account and database
- Streamlit Cloud account (for deployment)

## Installation
1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd AIS-FMD
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure secrets**
   - Copy `secrets.toml.example` to `.streamlit/secrets.toml`
   - Fill in your Supabase credentials and treasury password:
   ```toml
   [supabase]
   url = "your_supabase_url"
   key = "your_supabase_anon_key"
   service_key = "your_supabase_service_role_key"
   
   [treasury]
   password = "your_treasury_password"
   ```

4. **Run the application**
   ```bash
   streamlit run app.py
   ```

## Database Schema
The application uses the following Supabase tables:

### `committees`
- `CommitteeID` (Primary Key)
- `Committee_Name`
- `Committee_Type`

### `terms`
- `TermID` (Primary Key)
- `Semester`
- `start_date`
- `end_date`

### `committeebudgets`
- `committeebudgetid` (Primary Key)
- `termid` (Foreign Key to terms)
- `committeeid` (Foreign Key to committees)
- `budget_amount`

### `transactions`
- `transactionid` (Primary Key)
- `transaction_date`
- `amount`
- `details`
- `budget_category` (Foreign Key to committees)
- `purpose`
- `account`

## Usage Guide
### For General Members
1. **Access the Dashboard**: Navigate to the "Financial Dashboard" tab
2. **Filter Data**: Use the sidebar filters to view specific committees, semesters, or date ranges
3. **Analyze Spending**: View budget vs spending analysis with interactive charts
4. **Export Data**: Download filtered transaction data as needed

### For Treasury Committee
1. **Access Treasury Portal**: Navigate to "Treasury Management" and enter the password
2. **Upload Transactions**: Use the Excel upload feature to update transaction data
3. **Manage Terms**: Add new academic terms with start and end dates
4. **Set Budgets**: Allocate budgets to committees for each term
5. **Monitor Data**: Use database tools to export data and check integrity

## Excel Upload Format
When uploading transaction data via Excel, ensure your file contains these columns:

| Column | Type | Description |
|--------|------|-------------|
| `transaction_date` | Date | Transaction date (YYYY-MM-DD) |
| `amount` | Number | Transaction amount (positive for income, negative for expenses) |
| `details` | Text | Transaction details/description |
| `budget_category` | Number | Committee ID (from committees table) |
| `purpose` | Text | Transaction purpose/category |
| `account` | Text | Account information |

## Security
- **Authentication**: User authentication via Supabase Auth
- **Treasury Access**: Password-protected treasury portal
- **Data Validation**: Input validation and data integrity checks
- **Secure Storage**: Credentials stored in Streamlit secrets

## Deployment
### Streamlit Cloud
1. Push your code to GitHub
2. Connect your repository to Streamlit Cloud
3. Add your secrets in the Streamlit Cloud dashboard
4. Deploy!

### Local Development
```bash
streamlit run app.py --server.port 8501
```

## Future Enhancements
- [ ] Real-time notifications for budget overruns
- [ ] Advanced reporting and analytics
- [ ] Integration with banking APIs
- [ ] Mobile-responsive design improvements
- [ ] Multi-user role management
- [ ] Audit trail and change logging

## Contributing
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Support
For technical support or questions, contact the Treasury Committee or create an issue in the repository.

## License
This project is proprietary to the University of Florida AIS organization.

---

**Made with love by the UF AIS Treasury Committee**
