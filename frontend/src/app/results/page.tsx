import { Suspense } from "react";
import { Loading } from "@/components/Msg";
import { RequireAuth } from "@/components/RequireAuth";
import ResultsView from "./ResultsView";

export const metadata = { title: "결과 목록 — 제때받기" };

export default function ResultsPage() {
  return (
    <>
      <h1>결과 목록</h1>
      <RequireAuth>
        <Suspense fallback={<Loading />}>
          <ResultsView />
        </Suspense>
      </RequireAuth>
    </>
  );
}
