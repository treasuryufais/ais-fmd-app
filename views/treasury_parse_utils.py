"""Parsing and auto-categorization helpers for treasury uploads (no Streamlit)."""

from __future__ import annotations

import re

import pandas as pd

from .treasury_auto_categorize import apply_enhanced_auto_categorization


def classify_purpose(text: str) -> str | None:
    """Classify purpose from transaction details (legacy keyword dues)."""
    if not isinstance(text, str) or not text:
        return None
    s = text.lower().strip()
    dues_keywords = ["dues", "due", "membership fee", "membership payment", "membership"]
    return "Dues" if any(keyword in s for keyword in dues_keywords) else None


def numeric_amount(x):
    """Parse numeric amount from various formats."""
    try:
        if pd.isna(x):
            return 0.0
        s = str(x).replace("\u2032", "").replace("\u2019", "").replace("\xa0", " ").replace("$", "")
        s = re.sub(r"\s+", " ", s).strip()
        m = re.search(r"([+-]?)\s*([0-9]{1,3}(?:[,\d]*)(?:\.\d+)?)", s)
        if not m:
            return 0.0
        return float(m.group(1) + m.group(2).replace(",", ""))
    except Exception:
        return 0.0


def clean_proc_df(df_proc: pd.DataFrame) -> pd.DataFrame:
    """Clean processed dataframe by removing footer/empty rows."""
    df = df_proc.copy()
    df["amount"] = df["amount"].apply(lambda x: float(x) if pd.notna(x) else 0.0)
    df["details"] = df.get("details", "").fillna("").astype(str).str.strip()

    no_date = df["transactiondate"].isna()
    details_empty = df["details"].str.replace(r"[\|\-\s]+", "", regex=True) == ""
    mask_footer = no_date & details_empty
    mask_zero_blank = no_date & (df["amount"] == 0) & (df["details"].str.strip() == "")

    return df[~(mask_footer | mask_zero_blank)].reset_index(drop=True)


def merge_legacy_and_enhanced_auto_cat(df_proc: pd.DataFrame) -> pd.DataFrame:
    """Legacy keyword dues + enhanced rules; enhanced wins when it assigns a committee."""
    df = df_proc.copy()
    df["purpose"] = df["details"].apply(classify_purpose)
    df = clean_proc_df(df)
    df["budget"] = df["purpose"].apply(lambda p: "1 - Dues" if p == "Dues" else "")
    enhanced = apply_enhanced_auto_categorization(df)
    mask = enhanced["budget"].astype(str).str.strip() != ""
    df.loc[mask, "purpose"] = enhanced.loc[mask, "purpose"].values
    df.loc[mask, "budget"] = enhanced.loc[mask, "budget"].values
    return df
