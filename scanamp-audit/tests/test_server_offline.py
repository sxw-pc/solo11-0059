import copy
import socket
from concurrent.futures import ThreadPoolExecutor
from scanamp_audit.cli import main
from scanamp_audit.errors import SecurityError
from scanamp_audit.ledger import Ledger
from scanamp_audit.security import validate_local_host
from scanamp_audit.server import create_app
import pytest

def client(path):
    return create_app(path, allow_outside=True).test_client()

def test_routes_and_read_only(demo_ledger):
    http = client(demo_ledger)
    health = http.get("/healthz")
    assert health.status_code == 200 and health.get_json()["status"] == "ok"
    index = http.get("/")
    assert index.status_code == 200 and b"demo-run" in index.data
    run = http.get("/runs/demo-run")
    assert run.status_code == 200 and b"q01_partition_plus_amount" in run.data
    assert http.get("/runs/missing").status_code == 404
    assert http.post("/healthz").status_code == 405
    assert http.put("/runs/demo-run").status_code == 405
    assert http.head("/").status_code == 200

def test_request_from_worker_thread(demo_ledger):
    app = create_app(demo_ledger, allow_outside=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        status = pool.submit(lambda: app.test_client().get("/healthz").status_code).result()
    assert status == 200
    app.extensions["scanamp_ledger"].close()

def test_self_contained_security_headers(demo_ledger):
    http = client(demo_ledger)
    response = http.get("/runs/demo-run")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    lowered = response.data.lower()
    for marker in (b"http://", b"https://", b"<link", b"src=", b"cdn"):
        assert marker not in lowered

def test_html_escaping_in_server(demo_result, tmp_path):
    result = copy.deepcopy(demo_result)
    result.diagnostics[0]["message"] = "<script>alert(1)</script>"
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path, allow_outside=True)
    ledger.record_run(result, [])
    ledger.close()
    data = client(path).get("/runs/demo-run").data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in data
    assert b"<script>alert(1)</script>" not in data

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "not-local.invalid", ""])
def test_non_local_host_rejected(host):
    with pytest.raises(SecurityError):
        validate_local_host(host)

def test_loopback_hosts_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert validate_local_host(host) == host

def test_serve_rejects_non_local_host(demo_ledger):
    assert main(["serve", "--ledger", str(demo_ledger), "--host", "0.0.0.0",
                 "--allow-ledger-outside-workdir"]) == 2

def test_offline_demo_with_sockets_disabled(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    code = main(["demo", "--out", str(tmp_path / "demo"), "--ledger", str(tmp_path / "l.sqlite"),
                 "--allow-ledger-outside-workdir"])
    assert code == 0
    assert (tmp_path / "demo" / "report.html").is_file()
