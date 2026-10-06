"""Status and kind enums (SPEC §5)."""

from __future__ import annotations

from enum import StrEnum


class ReconcileStatus(StrEnum):
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ReviewStatus(StrEnum):
    """Review state. VERIFIED = mechanical checks passed; it does not mean legal correctness."""

    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class DocumentStatus(StrEnum):
    REGISTERED = "REGISTERED"
    PARSED = "PARSED"
    NEEDS_MAPPING = "NEEDS_MAPPING"
    UNSUPPORTED_SCAN = "UNSUPPORTED_SCAN"
    CORRUPT = "CORRUPT"
    FAILED = "FAILED"


class TradeType(StrEnum):
    """Trade form deciding which statutory payment-term rule applies."""

    DIRECT = "direct"  # 직매입 (대규모유통업법 제8조)
    CONSIGNMENT = "consignment"  # 특약매입·위수탁·임대을 (대규모유통업법 제8조)
    SUBCONTRACT = "subcontract"  # 하도급법 제13조


class LineKind(StrEnum):
    SALE = "sale"
    DEDUCTION = "deduction"  # 공제
    RETURN = "return"  # 반품
    REFUND = "refund"
    FEE = "fee"


class DocKind(StrEnum):
    SETTLEMENT = "settlement"  # 정산서
    TAX_INVOICE = "tax_invoice"  # 세금계산서
    BANK = "bank"  # 입금 내역
    AGREEMENT = "agreement"  # 약정서·계약서
    DELIVERY = "delivery"  # 입고·하차 기록
    SALES_CLOSE = "sales_close"  # 판매마감 내역
    OTHER = "other"


class ChangeKind(StrEnum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"
