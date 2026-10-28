"""Test suite for Phase 3: water_group module compatibility shim."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_imports():
    # 1. Standard package import
    import water_group
    assert hasattr(water_group, "memory_engine")
    assert hasattr(water_group, "bootstrap")

    # 2. Submodule direct import
    import water_group.memory_engine
    assert hasattr(water_group.memory_engine, "MemoryEngine")
    assert hasattr(water_group.memory_engine, "get_memory_engine")

    # 3. from ... import ...
    from water_group import bootstrap
    assert hasattr(bootstrap, "ensure_napcat")
    assert hasattr(bootstrap, "ensure_tulpa")
    assert hasattr(bootstrap, "check_tulpa_mcp_health")

    # 4. Deep import
    from water_group.memory_engine import get_memory_engine, MemoryEngine
    assert callable(get_memory_engine)
    assert isinstance(MemoryEngine, type)

    # 5. Aliased import
    import water_group.sync_groups as sg
    assert hasattr(sg, "inspect_and_sync")

    # 6. Extracted memory helpers stay reachable through the bridge
    from water_group.memory_commands import parse_command
    from water_group.memory_format import render_l1_card
    assert callable(parse_command) and callable(render_l1_card)

    # 7. Test __all__ and clean namespace export
    assert sorted(water_group.__all__) == sorted([
        "memory_engine", "memory_commands", "memory_format",
        "memory_store", "memory_profile", "memory_observe", "memory_dossier",
        "memory_pollution", "bootstrap", "sync_groups"])
    local_ns = {}
    exec("from water_group import *", local_ns)
    for expected in water_group.__all__:
        assert expected in local_ns, f"{expected} must be exported by star import"
    assert "sys" not in local_ns and "Path" not in local_ns, "Internal helper imports must not leak into star import"

    print("✓ All water_group import variations succeeded seamlessly!")


if __name__ == "__main__":
    test_imports()
    print("PASS: water_group module compatibility suite passed.")
