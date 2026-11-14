from pathlib import Path
import pytest
from scanamp_audit.config import load_config
from scanamp_audit.engine.auditor import run_audit
from scanamp_audit.loader import load_table
from scanamp_audit.workload import load_workload

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "fixtures" / "demo_lake"
WORKLOAD = ROOT / "fixtures" / "workloads" / "demo_workload.yaml"
CONFIG = ROOT / "fixtures" / "configs" / "demo_audit.yaml"
GOLDEN = ROOT / "tests" / "golden"

@pytest.fixture(scope="session")
def demo_table():
    return load_table(LAKE)

@pytest.fixture(scope="session")
def demo_config():
    return load_config(CONFIG)

@pytest.fixture(scope="session")
def demo_workload(demo_table):
    return load_workload(WORKLOAD, demo_table)

@pytest.fixture(scope="session")
def demo_result(demo_table, demo_workload, demo_config):
    return run_audit(demo_table, demo_workload, demo_config, "demo-run", "2024-01-01T00:00:00Z")

@pytest.fixture(scope="session")
def demo_ledger(tmp_path_factory, demo_result):
    from scanamp_audit.ledger import Ledger
    path = tmp_path_factory.mktemp("ledger") / "ledger.sqlite"
    ledger = Ledger(path, allow_outside=True)
    ledger.record_run(demo_result, [])
    ledger.close()
    return path
