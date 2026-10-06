"""Report rendering (CSV / HTML) from ``app.dto.Report``.

CSV: every cell passes through ``csv_safe`` (formula-injection guard: a leading ``= + - @``
tab or CR gets a ``'`` prefix), UTF-8 with BOM for spreadsheet apps.
HTML: every value is HTML-escaped; served with a restrictive CSP by the route.
"""

from __future__ import annotations

import csv
import io
import json
from html import escape

from jettae.app.dto import Report, ReportItem, csv_safe
from jettae.domain.dates import to_display

REVIEW_LABEL = {
    "DRAFT": "초안",
    "VERIFIED": "기계 검사 통과(법적 판단 아님)",
    "APPROVED": "현재 결과 기준 확인됨",
    "REVIEW_REQUIRED": "근거 변경으로 재검토 필요",
    "SUPERSEDED": "대체됨(현재 결과 아님)",
}


def _sources(it: ReportItem) -> str:
    parts = []
    for s in it.sources:
        loc = json.dumps(dict(s.locator), ensure_ascii=False, sort_keys=True)
        parts.append(f'{s.doc_version_id} {loc} "{s.excerpt}"')
    return " | ".join(parts)


def _checks(it: ReportItem) -> str:
    return "; ".join(f"{c.name}={'ok' if c.passed else 'fail'}" for c in it.checks)


def render_csv(report: Report) -> bytes:
    rows = report.to_rows()  # already formula-safe
    header = rows[0] + ["snapshot_hash", "sources", "checks", "valid_at_export", "generated_at"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([csv_safe(h) for h in header])
    gen = to_display(report.generated_at).isoformat()
    for row, it in zip(rows[1:], report.items, strict=True):
        valid = "yes" if it.review_status.value == "APPROVED" else "no"
        w.writerow(
            row + [csv_safe(x) for x in (it.snapshot_hash, _sources(it), _checks(it), valid, gen)]
        )
    return ("﻿" + buf.getvalue()).encode("utf-8")


_CSS = (
    "body{font-family:system-ui,sans-serif;margin:24px;color:#111}"
    "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;"
    "vertical-align:top;text-align:left}pre{white-space:pre-wrap;margin:0}"
    ".warn{background:#fff4e5;padding:8px}.ok{background:#e8f5e9;padding:8px}"
    "code{font-size:12px}"
)


def render_html(report: Report) -> bytes:
    e = escape
    gen = to_display(report.generated_at).strftime("%Y-%m-%d %H:%M:%S (Asia/Seoul)")
    banner = (
        '<p class="ok">모든 항목이 내보내기 시점의 현재 결과 기준으로 확인(승인)되어 있습니다.</p>'
        if report.all_approved
        else '<p class="warn">확인(승인)되지 않았거나 근거 변경으로 재검토가 필요한 항목이 '
        "있습니다. 각 항목의 상태를 확인하세요.</p>"
    )
    rows = []
    for it in report.items:
        srcs = "".join(
            f"<li><code>{e(s.doc_version_id)}</code> "
            f"<code>{e(json.dumps(dict(s.locator), ensure_ascii=False, sort_keys=True))}</code>"
            f" &ldquo;{e(s.excerpt)}&rdquo;</li>"
            for s in it.sources
        )
        checks = "".join(
            f"<li>{e(c.name)}: {'통과' if c.passed else '실패'}"
            + (f" &mdash; {e('; '.join(c.details))}" if c.details else "")
            + "</li>"
            for c in it.checks
        )
        rows.append(
            "<tr>"
            f"<td><code>{e(it.decision_id)}</code><br>{e(it.subject_id)}</td>"
            f"<td>{e(it.status.value)}</td>"
            f"<td>{e(REVIEW_LABEL.get(it.review_status.value, it.review_status.value))}</td>"
            f"<td><pre>{e(it.explanation)}</pre></td>"
            f"<td><ul>{srcs}</ul></td>"
            f"<td><ul>{checks}</ul><code>{e(it.result_hash[:16])}</code></td>"
            "</tr>"
        )
    html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f"<title>제때받기 확인 보고서</title><style>{_CSS}</style></head><body>"
        "<h1>제때받기 확인 보고서</h1>"
        f"<p>생성 시각: {e(gen)}</p>{banner}"
        "<p>이 보고서는 차이, 필요 서류, 확인이 필요한 조건, 관련 근거만 정리합니다. "
        "법적 판단을 포함하지 않습니다.</p>"
        "<table><thead><tr><th>항목</th><th>대사 상태</th><th>검토 상태</th>"
        "<th>설명</th><th>원문 위치</th><th>검사</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )
    return html.encode("utf-8")
