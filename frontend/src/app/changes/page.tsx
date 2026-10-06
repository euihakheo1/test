import { RequireAuth } from "@/components/RequireAuth";
import ChangesView from "./ChangesView";

export const metadata = { title: "정정·추가 자료 — 제때받기" };

export default function ChangesPage() {
  return (
    <>
      <h1>정정·추가 자료 업로드와 영향 비교</h1>
      <RequireAuth>
        <ChangesView />
      </RequireAuth>
    </>
  );
}
