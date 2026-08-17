# Backing up the live Supabase project

**Do this before any migration runs against the live database.** The migration is
additive — it drops nothing and retypes nothing — but "should be safe" is not a
backup, and this database holds the organisation's entire financial history.

Budget 20–30 minutes. Most of it is waiting on downloads.

---

## Before you start: two things about credentials

1. **Do not paste the database password, service key, or anon key into a chat,
   a commit, or a shared doc.** Every command below is one *you* run in your own
   terminal, so the credential never leaves your machine.
2. The connection string contains the database password in plain text. Your
   shell records commands in history. On Windows/Git Bash, prefix the command
   with a space (` npx ...`) or clear history afterwards if that matters to you.

---

## Step 0 — Check what automatic backups you already have

Supabase Dashboard → your project → **Database** → **Backups**.

* **Pro plan or higher**: daily backups are taken automatically, retained 7 days,
  and Point-in-Time Recovery may be available. You still want a manual copy —
  automated backups live in the same account you are about to change.
* **Free plan**: **there are no automatic backups.** Everything below is not
  belt-and-braces, it is the only copy. Do not skip it.

Note down which of these is true. It decides how nervous to be later.

---

## Step 1 — Get the connection string

Dashboard → **Project Settings** → **Database** → **Connection string** → **URI**.

Choose the **direct connection** (port 5432), not the transaction pooler
(port 6543). Pooled connections do not support the statements a dump needs.

It looks like this — `[YOUR-PASSWORD]` is a placeholder you replace with the
database password:

```
postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijkl.supabase.co:5432/postgres
```

If you do not know the database password, the same page has **Reset database
password**. Resetting it does not affect the anon/service keys the app uses, so
the running app will not break.

---

## Step 2 — Full logical backup (the one that can actually restore)

`pg_dump` is not installed on this machine, but the Supabase CLI bundles a
matching version and `npx` will fetch it on demand. Run these from anywhere —
they write into the current folder.

Make a folder to hold the backup first, with today's date in the name:

```bash
mkdir -p ~/supabase-backup-2026-08-15 && cd ~/supabase-backup-2026-08-15
```

Then take three dumps. Together they are a complete, restorable copy:

```bash
npx --yes supabase@latest db dump --db-url "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" -f schema.sql
```

```bash
npx --yes supabase@latest db dump --db-url "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" --data-only -f data.sql
```

```bash
npx --yes supabase@latest db dump --db-url "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" --role-only -f roles.sql
```

Replace `PASSWORD` and `REF` in each. The first run takes a minute while npx
downloads the CLI; the rest are quick.

**`schema.sql`** is the table and constraint definitions, **`data.sql`** is every
row, **`roles.sql`** is the database roles. A restore needs all three.

---

## Step 3 — Prove the backup is real

A file existing is not a backup. Two checks, both quick:

```bash
ls -la
```

`data.sql` should be substantially larger than `schema.sql`. If `data.sql` is
only a few kilobytes, it did not capture the transaction rows — stop and work
out why before going further.

```bash
grep -c "INSERT INTO\|COPY public" data.sql
```

Should be non-zero. Then open `data.sql` and confirm you can see real
transaction rows and real committee names — the actual data, not just table
headers.

---

## Step 4 — A second, human-readable copy

Dumps are for restoring; CSVs are for reading when something looks wrong and you
want to check a number by eye without restoring anything.

Dashboard → **Table Editor** → for each table: the **⋮** menu → **Export as CSV**.

Do all six:

```
committees            committeebudgets      terms
transactions          uploaded_files        stagingtransactions
```

`transactions` is the important one. Save them alongside the `.sql` files.

---

## Step 5 — Store it somewhere that is not this laptop

Copy the whole `supabase-backup-2026-08-15` folder to Google Drive, a OneDrive
folder, or an external disk.

**Do not commit it to git.** It contains every member payment with names
attached. `sandbox_data/` and `.streamlit/secrets.toml` are gitignored for the
same reason; a backup folder inside the repo would not be, unless you add it.

---

## Step 6 — Note the row counts

Write these down before the migration, so afterwards you can prove nothing was
lost. In the Dashboard SQL Editor:

```sql
select 'committees' as table_name, count(*) from public.committees
union all select 'terms',                count(*) from public.terms
union all select 'committeebudgets',     count(*) from public.committeebudgets
union all select 'transactions',         count(*) from public.transactions
union all select 'uploaded_files',       count(*) from public.uploaded_files
union all select 'stagingtransactions',  count(*) from public.stagingtransactions;
```

And the financial totals, which are the real check — a row count can match while
the numbers underneath have shifted:

```sql
select
    count(*)                                    as rows,
    sum(amount)                                 as net,
    sum(amount) filter (where amount > 0)       as income,
    sum(amount) filter (where amount < 0)       as expenses,
    min(transaction_date)                       as earliest,
    max(transaction_date)                       as latest
from public.transactions;
```

Save the output. `scripts/verify_migration.py` compares against these afterwards.

---

## If you ever need to restore

```bash
psql "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" -f roles.sql
psql "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" -f schema.sql
psql "postgresql://postgres:PASSWORD@db.REF.supabase.co:5432/postgres" -f data.sql
```

In that order. `psql` is not installed here either — install the PostgreSQL
client tools, or restore into a fresh Supabase project via the Dashboard's SQL
Editor by pasting the file contents.

Restoring into a **new** project and pointing the app at that is usually safer
than restoring over a database that is already in a bad state.

---

## Checklist

- [ ] Checked whether the plan has automatic backups (Step 0)
- [ ] `schema.sql`, `data.sql`, `roles.sql` all downloaded
- [ ] `data.sql` verified to contain real rows, not just headers
- [ ] Six CSVs exported
- [ ] Copied somewhere off this machine
- [ ] Row counts and financial totals written down

Only after every box is ticked should the migration run.
