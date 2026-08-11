"""
Module M9 -- receipt capture.

Stores receipt files and links them to a transaction or a reimbursement request.
In the sandbox files land under `sandbox_data/receipts/`; in production the same
interface would write to Supabase Storage and keep only the object key.

Deliberately no OCR. Extraction needs either a network service or a local model,
and the sandbox has neither by design -- so vendor, amount and date are entered
by the person attaching the file. The metadata fields exist so an extraction
step can populate them later without a schema change.

Two safety properties matter here, because this is the only place the app
accepts an arbitrary file from a user:

  * filenames are sanitised and never used as a path directly, so a name like
    "../../etc/passwd" cannot escape the receipts directory
  * only a small allowlist of image/PDF types is accepted
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import settings

ALLOWED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"})
MAX_BYTES = 10 * 1024 * 1024  # 10 MB

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class StoredReceipt:
    file_name: str
    stored_path: str
    content_type: str | None
    byte_size: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def receipts_dir() -> Path:
    directory = settings.sandbox_db_path().parent / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_filename(name: str) -> str:
    """
    Reduce an uploaded filename to something safe to place on disk.

    Never trust the client-supplied name: it can contain path separators,
    traversal sequences, or unicode that normalises into them.
    """
    normalised = unicodedata.normalize("NFKD", str(name or "receipt"))
    # Take the final path component under both separators before sanitising.
    base = normalised.replace("\\", "/").split("/")[-1]
    cleaned = _UNSAFE.sub("_", base).strip("._") or "receipt"
    return cleaned[:120]


def extension_of(name: str) -> str:
    return Path(safe_filename(name)).suffix.lower()


def validate(file_name: str, data: bytes) -> str | None:
    """Return an error message, or None when the file is acceptable."""
    if not data:
        return "The file is empty."
    if len(data) > MAX_BYTES:
        return f"File is {len(data) / 1_048_576:.1f} MB; the limit is {MAX_BYTES // 1_048_576} MB."
    extension = extension_of(file_name)
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return f"'{extension or 'no extension'}' is not an accepted receipt type. Allowed: {allowed}"
    return None


def store(file_name: str, data: bytes, content_type: str | None = None) -> StoredReceipt:
    """
    Write the file into the receipts directory under a content-addressed name.

    Hashing the contents means uploading the same receipt twice reuses one file
    rather than accumulating copies.
    """
    problem = validate(file_name, data)
    if problem:
        return StoredReceipt(file_name, "", content_type, len(data or b""), error=problem)

    safe = safe_filename(file_name)
    digest = hashlib.sha256(data).hexdigest()[:16]
    stamp = datetime.now().strftime("%Y%m%d")
    target = receipts_dir() / f"{stamp}_{digest}_{safe}"

    # Final containment check: the resolved path must stay inside the directory.
    if not str(target.resolve()).startswith(str(receipts_dir().resolve())):
        return StoredReceipt(file_name, "", content_type, len(data), error="Invalid file path.")

    if not target.exists():
        target.write_bytes(data)

    return StoredReceipt(
        file_name=safe,
        stored_path=str(target),
        content_type=content_type,
        byte_size=len(data),
    )


def load(stored_path: str) -> bytes | None:
    path = Path(stored_path)
    if not path.exists():
        return None
    if not str(path.resolve()).startswith(str(receipts_dir().resolve())):
        return None
    return path.read_bytes()


def coverage(df_transactions, df_receipts) -> dict:
    """
    How much spending is backed by a receipt.

    Expressed in dollars as well as counts, because an audit cares about the
    value covered rather than the number of rows.
    """
    if df_transactions is None or df_transactions.empty:
        return {"expenses": 0, "with_receipt": 0, "amount": 0.0, "covered_amount": 0.0, "rate": 0.0}

    expenses = df_transactions[df_transactions["amount"] < 0]
    total_amount = float(expenses["amount"].abs().sum())
    if expenses.empty:
        return {"expenses": 0, "with_receipt": 0, "amount": 0.0, "covered_amount": 0.0, "rate": 0.0}

    linked: set[int] = set()
    if df_receipts is not None and not df_receipts.empty:
        linked = {
            int(value)
            for value in df_receipts["transaction_id"].dropna().tolist()
        }

    covered = expenses[expenses["transactionid"].isin(linked)]
    covered_amount = float(covered["amount"].abs().sum())
    return {
        "expenses": int(len(expenses)),
        "with_receipt": int(len(covered)),
        "amount": total_amount,
        "covered_amount": covered_amount,
        "rate": (covered_amount / total_amount * 100) if total_amount else 0.0,
    }
