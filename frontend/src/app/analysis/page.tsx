import { RequireAuth } from "@/components/RequireAuth";
import AnalysisView from "./AnalysisView";

export const metadata = { title: "분석 — 제때받기" };

export default function AnalysisPage() {
  return (
    <>
      <h1>분석 실행</h1>
      <RequireAuth>
        <AnalysisView />
      </RequireAuth>
    </>
  );
}
