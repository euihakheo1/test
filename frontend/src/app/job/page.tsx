import { Suspense } from "react";
import { Loading } from "@/components/Msg";
import { RequireAuth } from "@/components/RequireAuth";
import JobView from "./JobView";

export const metadata = { title: "작업 상태 — 제때받기" };

export default function JobPage() {
  return (
    <>
      <h1>작업 상태</h1>
      <RequireAuth>
        <Suspense fallback={<Loading />}>
          <JobView />
        </Suspense>
      </RequireAuth>
    </>
  );
}
