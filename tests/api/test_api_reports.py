"""Report export: CSV formula-injection escaping, HTML escaping, headers."""

from __future__ import annotations

import csv
import io

from jt_api_helpers import invoice_change

API = "/api/v1"
EVIL = "=HYPERLINK(1)<script>x</script>"


def test_csv_and_html_escaping(client, alice, worker):
    ch = invoice_change(EVIL, 1_000, cp="@SUM(1)")
    r = client.post(f"{API}/changes", headers=alice, json={"changes": [ch]})
    assert r.status_code == 202, r.text
    worker.run_once()
    ids = [d["id"] for d in client.get(f"{API}/decisions", headers=alice).json()["items"]]
    assert ids == [f"dec:{EVIL}"]

    r = client.post(f"{API}/reports/export", headers=alice, json={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    header, row = rows[0], rows[1]
    cell = dict(zip(header, row, strict=True))
    assert cell["subject_id"] == "'" + EVIL  # formula prefix neutralised
    for value in row:
        assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), value
    assert cell["valid_at_export"] == "no"

    r = client.post(f"{API}/reports/export", headers=alice, json={"format": "html"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    html = r.content.decode("utf-8")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    for word in ("위법", "받을 수 있"):
        assert word not in html
