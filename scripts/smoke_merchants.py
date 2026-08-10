"""Verify merchant-key normalization collapses real bank noise. M4 depends on this."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.data.repositories import load_merchant_memory  # noqa: F401  (import check only)
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize.merchants import MerchantMemory, merchant_key
from ais_fmd.domain.categorize.pipeline import categorize_records

SAMPLES = [
    "PURCHASE AUTHORIZED ON 09/16 PUBLIX SUPER MAR GAINESVILLE FL S304512345678 CARD 1234",
    "PURCHASE AUTHORIZED ON 10/02 PUBLIX SUPER MAR GAINESVILLE FL S987654321098 CARD 1234",
    "PURCHASE AUTHORIZED ON 11/19 CHIPOTLE 1462 GAINESVILLE FL S111222333444 CARD 1234",
    "3421987654321098 | Fall dues | Ava Mitchell | UF AIS",
    "3421987654321099 | Fall dues | Noah Patel | UF AIS",
]


def main() -> int:
    print("-- merchant_key normalization --")
    for sample in SAMPLES:
        print(f"  {sample[:62]:<62} -> {merchant_key(sample)!r}")

    a, b = merchant_key(SAMPLES[0]), merchant_key(SAMPLES[1])
    print(f"\ntwo different Publix rows collapse to the same key: {a == b} ({a!r})")

    backend = SqliteBackend()
    memory = MerchantMemory.from_records(backend.fetch_merchants().to_dict("records"))
    print(f"seeded merchant rules: {len(memory)}")

    tx = backend.fetch_transactions().head(200).to_dict("records")
    without = categorize_records(tx)
    with_memory = categorize_records(tx, memory)

    print("\n-- effect of merchant memory on model load --")
    print(f"  without memory: {without.summary_line()}")
    print(f"  with memory:    {with_memory.summary_line()}")
    print(
        f"  rows needing a model: {without.rows_sent_to_model} -> "
        f"{with_memory.rows_sent_to_model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
