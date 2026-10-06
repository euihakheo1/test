import { Suspense } from "react";
import { Loading } from "@/components/Msg";
import { RequireAuth } from "@/components/RequireAuth";
import MappingView from "./MappingView";

export const metadata = { title: "열 매핑 확인 — 제때받기" };

export default function MappingPage() {
  return (
    <>
      <h1>열 매핑 확인</h1>
      <RequireAuth>
        <Suspense fallback={<Loading />}>
          <MappingView />
        </Suspense>
      </RequireAuth>
    </>
  );
}
