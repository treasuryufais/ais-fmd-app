"""
Rewrite relative imports to absolute in ais_fmd/views/*.py.

Streamlit's st.Page executes page files with exec() rather than importing them
as package members, so `from .. import x` has no parent package to resolve
against. Pages must use absolute imports.
"""

import re
from pathlib import Path

VIEWS = Path(__file__).resolve().parent.parent / "ais_fmd" / "views"

# `from .. import auth`  ->  `from ais_fmd import auth`
BARE = re.compile(r"^from \.\. import ", re.M)
# `from ..data.x import y` -> `from ais_fmd.data.x import y`
DOTTED = re.compile(r"^from \.\.(?=\w)", re.M)


def main() -> int:
    changed = 0
    for path in sorted(VIEWS.glob("*.py")):
        original = path.read_text(encoding="utf-8")
        updated = DOTTED.sub("from ais_fmd.", BARE.sub("from ais_fmd import ", original))
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
            print(f"rewrote {path.name}")
    print(f"\n{changed} file(s) updated")

    remaining = [p.name for p in VIEWS.glob("*.py") if "from .." in p.read_text(encoding="utf-8")]
    if remaining:
        print("STILL RELATIVE:", remaining)
        return 1
    print("no relative imports remain in views/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
