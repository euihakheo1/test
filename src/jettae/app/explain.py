"""Deterministic explanation text rendered only from engine values.

Wording rule: differences, required documents, unconfirmed conditions and sources only.
Identifiers are written in [brackets] so that number checks skip them.
"""

from __future__ import annotations

from jettae.domain.models import Decision
from jettae.domain.status import ReconcileStatus

STATUS_LABEL = {
    ReconcileStatus.MATCHED: "입금액 일치",
    ReconcileStatus.PARTIAL: "일부 입금",
    ReconcileStatus.UNMATCHED: "입금 미확인",
    ReconcileStatus.AMBIGUOUS: "입금 대상 특정 불가",
    ReconcileStatus.CONFLICT: "조건 충돌",
    ReconcileStatus.INSUFFICIENT_EVIDENCE: "근거 부족(계산 보류)",
}
VARIANT_LABEL = {"rollover_off": "말일 그대로", "rollover_on": "다음 영업일 이월"}
UNRESOLVED_LABEL = {
    "rollover": "기한 말일이 휴일일 때 다음 영업일 이월 여부",
    "allocation": "입금이 어느 거래에 대한 것인지",
    "agreement_conflict": "약정 조건 간 차이",
    "trade_type_conflict": "거래 형태(문서와 약정의 차이)",
    "interest_rule_version": "지연기간에 적용할 이율 고시 버전",
    "evidence_link": "같은 거래의 정산 행과 세금계산서인지",
    "duplicate_line": "다른 문서에 같은 참조번호로 있는 정산 행과 같은 행인지(정산서 중복 업로드)",
    "invoice_direction": "세금계산서의 매출·매입 구분과 거래처",
    "evidence_amount_conflict": "연결된 문서 간 금액 차이",
    "contract_term_base": "약정 지급기한의 기산점",
    "contract_term_conflict": "약정 간 지급기한 일수 차이",
}
DOC_LABEL = {"invoice": "세금계산서", "settlement_line": "정산 행"}
ROLE_LABEL = {"basis": "금액 기준", "corroborating": "보강 증빙", "candidate": "연결 후보"}
METHOD_LABEL = {"reference": "참조번호 일치", "user_confirmation": "사용자 확인"}
PENDING_TEXT = {
    "evidence_link": "같은 거래처의 정산 행과 같은 거래인지",
    "duplicate_line": "먼저 올린 다른 문서의 같은 참조번호 정산 행과 같은 행인지",
    "invoice_direction": "매출 세금계산서인지(매출·매입 구분과 거래처)",
}


def _evidence_lines(decision: Decision) -> list[str]:
    """Which document the amount comes from and which documents only corroborate it.

    A document row is not a receivable: a settlement line and the tax invoice for the same
    sale are one receivable (basis = settlement line). Only engine outputs are printed."""
    ev = decision.computation("evidence")
    if ev is None:
        return []
    o = ev.outputs
    lines = [
        f"금액 기준 문서: {DOC_LABEL.get(o['basis'], o['basis'])} [{o['basis_id']}]"
        f" {o['basis_amount']}"
    ]
    for d in o.get("documents", ())[1:]:
        how = METHOD_LABEL.get(d.get("method") or "", "")
        lines.append(
            f"  - {ROLE_LABEL.get(d['role'], d['role'])}: {DOC_LABEL.get(d['entity'], d['entity'])}"
            f" [{d['id']}] {d['amount']}" + (f" ({how})" if how else "")
        )
    if not o.get("counted", True):
        conf = o.get("confirmation_required") or {}
        what = PENDING_TEXT.get(conf.get("kind", ""), PENDING_TEXT["evidence_link"])
        lines.append(f"확인 대기: {what} 확인되기 전까지 미수 합계와 입금 배분에서 제외")
    if o.get("amount_conflict"):
        lines.append("연결된 문서 간 금액이 다름: 지급기한·지연이자 계산 보류")
    return lines


def _contract_lines(decision: Decision) -> list[str]:
    """Contractual due date (약정 기한), reported separately from the statutory due date.
    The statutory delay-interest rate is never applied to it."""
    c = decision.computation("contractual_due")
    if c is None:
        return []
    o = c.outputs
    if o.get("conflict"):
        return ["약정 기한: 적용 가능한 약정 간 지급기한 일수가 달라 계산하지 않음"]
    if o.get("insufficient"):
        return ["약정 기한: 기준일 또는 거래 형태 미확인으로 계산하지 않음"]
    line = f"약정 기한(약정 {o['term_days']}일) {o['due_date'].isoformat()}"
    diffs = [
        f"{VARIANT_LABEL.get(v['label'], v['label'])} 법정 기한과 {v['difference_days']}일 차이"
        for v in o.get("statutory_comparison", ())
    ]
    if diffs:
        line += ": " + ", ".join(diffs)
    return [line, "약정 기한 기준 지연이자 미계산(법정 지연이율을 약정 기한에 적용하지 않음)"]


def render_explanation(decision: Decision) -> str:
    lines = [f"[{decision.subject_id}] 상태: {STATUS_LABEL[decision.status]}"]
    lines.extend(_evidence_lines(decision))
    recon = decision.computation("recon")
    if recon is not None:
        o = recon.outputs
        line = f"금액 {recon.inputs['amount']}, 배분 {o['allocated']}, 미결 {o['open']}"
        if not o["fee_difference"].is_zero:
            line += f", 허용 오차로 처리한 차액 {o['fee_difference']}"
        lines.append(line)
        for a in decision.allocations:
            lines.append(f"  - 입금 [{a.source_id}] {a.amount}")
    due = decision.computation("due")
    if due is not None:
        o = due.outputs
        if o.get("insufficient"):
            lines.append("지급기한 계산 보류: 기준일 또는 거래 형태 미확인")
        else:
            lines.append(
                f"적용 규칙: [{due.rule_version}] (기준일 {o['base_date'].isoformat()},"
                f" {o['term_days']}일)"
            )
            withheld = o.get("delay_withheld")
            for v in o["variants"]:
                head = (
                    f"지급기한({VARIANT_LABEL.get(v['label'], v['label'])}) "
                    f"{v['due_date'].isoformat()}"
                )
                if withheld or v["max_delay_days"] is None:
                    lines.append(f"{head}: 지연일수·지연이자 미계산(입금 배분 미확정)")
                    continue
                interest = v["interest_total"]
                itxt = str(interest) if interest is not None else "미계산"
                lines.append(f"{head}: 최대 지연 {v['max_delay_days']}일, 지연이자 합계 {itxt}")
            as_of = due.inputs.get("as_of")
            if as_of is not None:
                lines.append(f"미지급분 계산 기준일(as_of): {as_of.isoformat()}")
    lines.extend(_contract_lines(decision))
    if decision.required_documents:
        lines.append("필요 서류: " + "; ".join(decision.required_documents))
    if decision.unresolved:
        lines.append(
            "확인이 필요한 조건: "
            + "; ".join(UNRESOLVED_LABEL.get(u, u) for u in decision.unresolved)
        )
    if decision.assumptions:
        lines.append("계산 가정: " + " / ".join(decision.assumptions))
    if decision.rule_versions:
        lines.append("관련 근거: " + ", ".join(f"[{r}]" for r in decision.rule_versions))
    return "\n".join(lines)
