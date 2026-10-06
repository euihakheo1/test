import type { DocKind } from "@/lib/api";
import { DOC_KIND_LABEL } from "@/lib/fmt";

export const KINDS: DocKind[] = [
  "other",
  "settlement",
  "tax_invoice",
  "bank",
  "agreement",
  "delivery",
  "sales_close",
];

export function KindSelect({
  id,
  value,
  onChange,
}: {
  id: string;
  value: DocKind;
  onChange: (k: DocKind) => void;
}) {
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value as DocKind)}>
      {KINDS.map((k) => (
        <option key={k} value={k}>
          {DOC_KIND_LABEL[k]}
        </option>
      ))}
    </select>
  );
}
