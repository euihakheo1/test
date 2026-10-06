"""Upload limits: size (streaming + Content-Length), extension/MIME/magic checks, filenames."""

from __future__ import annotations

import pytest

from jettae.api.uploads import sanitize_filename

API = "/api/v1"


def _up(client, headers, name, content, ctype="text/csv"):
    return client.post(f"{API}/documents", headers=headers, files={"file": (name, content, ctype)})


def test_too_large_is_413(client, alice, rt):
    limit = rt.settings.max_upload_bytes
    r = _up(client, alice, "big.csv", b"a," * (limit // 2 + 10))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"
    # far over the limit: rejected by the streaming body guard before parsing
    r = _up(client, alice, "huge.csv", b"a" * (limit * 3))
    assert r.status_code == 413
    # JSON bodies have their own limit
    r = client.post(
        f"{API}/changes",
        headers=alice,
        content=b'{"changes": [' + b" " * (rt.settings.max_json_body_bytes + 10) + b"]}",
    )
    assert r.status_code == 413


@pytest.mark.parametrize(
    ("name", "content", "ctype"),
    [
        ("tool.exe", b"MZ\x90\x00", "application/octet-stream"),
        ("sheet.xlsx", b"date,amount\n", "application/octet-stream"),  # not a zip
        ("scan.pdf", b"PK\x03\x04....", "application/pdf"),  # not a pdf
        ("data.csv", b"\x00\x01\x02binary", "text/csv"),  # NUL bytes
        ("data.csv", b"PK\x03\x04zip", "text/csv"),  # zip disguised as csv
        ("data.csv", b"a,b\n", "application/x-msdownload"),  # declared type mismatch
        ("noext", b"a,b\n", "text/csv"),
        ("legacy.xls", b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel"),
    ],
)
def test_bad_types_are_415(client, alice, name, content, ctype):
    r = _up(client, alice, name, content, ctype)
    assert r.status_code == 415, (name, r.text)
    assert r.json()["error"]["code"] == "unsupported_media_type"


def test_accepted_types_and_filename_sanitising(client, alice):
    r = _up(client, alice, "../../etc/pass:wd.csv", b"a,b\n1,2\n")
    assert r.status_code == 201, r.text
    assert r.json()["document"]["filename"] == "pass_wd.csv"
    assert r.json()["document"]["media_type"] == "text/csv"
    r = _up(client, alice, "s.xlsx", b"PK\x03\x04rest", "application/octet-stream")
    assert r.status_code == 201
    assert r.json()["document"]["media_type"].endswith("spreadsheetml.sheet")
    r = _up(client, alice, "p.pdf", b"%PDF-1.7\n...", "application/pdf")
    assert r.status_code == 201
    assert _up(client, alice, "empty.csv", b"").status_code == 422
    r = client.post(
        f"{API}/documents",
        headers=alice,
        files={"file": ("k.csv", b"x,y\n", "text/csv")},
        data={"kind": "nonsense"},
    )
    assert r.status_code == 422


def test_sanitize_filename_unit():
    assert sanitize_filename("C:\\Users\\x\\정산서.xlsx") == "정산서.xlsx"  # secret-scan: allow
    assert sanitize_filename("a\x00b\r\n.csv") == "ab.csv"
    assert sanitize_filename("..") == "upload"
    assert sanitize_filename(None) == "upload"
    long = "가" * 300 + ".csv"
    out = sanitize_filename(long)
    assert len(out) <= 200 and out.endswith(".csv")


def test_viewer_cannot_upload_or_approve(client, alice):
    r = client.post(
        f"{API}/auth/api-tokens", headers=alice, json={"name": "read-only", "role": "viewer"}
    )
    assert r.status_code == 201
    viewer = {"Authorization": f"Bearer {r.json()['token']}"}
    assert _up(client, viewer, "a.csv", b"a,b\n").status_code == 403
    r = client.post(f"{API}/jobs", headers=viewer, json={"type": "run_analysis", "params": {}})
    assert r.status_code == 403
    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=viewer, json={"expected_result_hash": "0" * 64}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert client.get(f"{API}/documents", headers=viewer).status_code == 200
