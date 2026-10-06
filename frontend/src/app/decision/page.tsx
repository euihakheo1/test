import { Suspense } from "react";
import { Loading } from "@/components/Msg";
import { RequireAuth } from "@/components/RequireAuth";
import DecisionView from "./DecisionView";

export const metadata = { title: "결과 상세 — 제때받기" };

export default function DecisionPage() {
  return (
    <>
      <h1>결과 상세</h1>
      <RequireAuth>
        <Suspense fallback={<Loading />}>
          <DecisionView />
        </Suspense>
      </RequireAuth>
    </>
  );
}
