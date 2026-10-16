"""
Every `self.<name>` the Supabase backend uses must exist.

The sandbox cannot reach Supabase, so no test executes these methods. That let
the VP flag methods keep calling `self._anon` -- the publishable-key client --
after the lock-down removed it: every flag read or write would have raised
AttributeError in production while the whole suite stayed green. This reads the
source instead of running it, so a renamed or removed client is caught here.
"""

from __future__ import annotations

import ast
import inspect

from ais_fmd.data import supabase_backend
from ais_fmd.data.supabase_backend import SupabaseBackend


def _assigned_in_methods(tree: ast.ClassDef) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    names.add(target.attr)
    return names


def test_every_self_attribute_the_supabase_backend_uses_exists():
    module = ast.parse(inspect.getsource(supabase_backend))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "SupabaseBackend")
    assigned = _assigned_in_methods(cls)

    used = {
        node.attr
        for node in ast.walk(cls)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
    }
    missing = sorted(name for name in used if name not in assigned and not hasattr(SupabaseBackend, name))
    assert not missing, f"SupabaseBackend uses attributes it never defines: {missing}"
