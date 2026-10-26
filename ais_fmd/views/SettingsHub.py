"""
Settings hub: Officer Access and the Audit Log.

CONSOLIDATION (Pebble). The merged pages are unchanged files, run one at a time
by shell.hub; the switcher renders beside each page's title. Remove this file
and restore the original entries in app.py to undo the merge.

Officer Access came back 2026-10-26 for the VP portal (see app.py). In
production it needs the `profiles` table from migration 013.
"""

from __future__ import annotations

from ais_fmd import auth
from ais_fmd.ui import shell

auth.current_user()

shell.hub(
    "settings_hub",
    [
        ("Officer Access", "OfficerAccess.py", auth.Role.TREASURER),
        ("Audit Log", "AuditLog.py", auth.Role.TREASURER),
    ],
)
