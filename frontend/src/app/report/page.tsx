import { RequireAuth } from "@/components/RequireAuth";
import ReportView from "./ReportView";

export const metadata = { title: "보고서 내보내기 — 제때받기" };

export default function ReportPage() {
  return (
    <>
      <h1>보고서 내보내기</h1>
      <RequireAuth>
        <ReportView />
      </RequireAuth>
    </>
  );
}
