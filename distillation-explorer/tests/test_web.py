"""End-to-end through Flask against the demo datalog. Skipped if the demo
data has not been generated (python run.py --make-demo)."""
import pytest

from explorer import config as config_mod
from explorer.process_db import demo_path_exists
from run import demo_overrides

CFG = config_mod.load(overrides=demo_overrides())
pytestmark = pytest.mark.skipif(not demo_path_exists(CFG), reason="no demo data")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from explorer import eflow
    from explorer.service import Service
    from explorer.web import create_app
    cfg = config_mod.load(overrides={**demo_overrides(), "app": {"cache_db": str(tmp_path_factory.mktemp("c") / "c.sqlite")}})
    svc = Service(cfg)
    eflow.refresh(cfg, svc.store)
    return create_app(cfg, svc).test_client()


def test_run_190_join(client):
    d = client.get("/api/run?col=CD02&dist=190").get_json()
    numbered = [f for f in d["fractions"] if not f["reactor"]]
    assert numbered[0]["start"] == "2026-07-28 01:00:00" and numbered[0]["end"] == "2026-07-28 10:50:00"
    assert [f["start"] for f in numbered] == sorted(f["start"] for f in numbered)
    assert all(f["metrics"]["coverage_pct"] > 99 for f in numbered)
    assert all(f["metrics"]["pot_min"] > 1 for f in numbered)   # dropouts gone


def test_unlogged_column_is_honest(client):
    runs = client.get("/api/runs?col=CD01").get_json()["runs"]
    d = client.get(f"/api/run?col=CD01&dist={runs[0]['distillation']}").get_json()
    assert d["has_process_data"] is False and d["overview"] is None and d["fractions"]


def test_compare_and_exports(client):
    c = client.get("/api/compare?col=CD02&frac=3&role=delta_t").get_json()
    assert len(c["x"]) == 200 and c["runs"]
    for u in ["/export/run.xlsx?col=CD02&dist=190", "/export/compare.csv?col=CD02&frac=3"]:
        assert client.get(u).status_code == 200


def test_coverage_finds_outage(client):
    cov = client.post("/api/coverage/scan?col=CD03&full=1").get_json()
    assert any(g["from"].startswith("2026-07-19 06") for g in cov["gaps"])
