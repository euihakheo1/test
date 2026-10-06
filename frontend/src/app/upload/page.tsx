import { RequireAuth } from "@/components/RequireAuth";
import UploadView from "./UploadView";

export const metadata = { title: "문서 업로드 — 제때받기" };

export default function UploadPage() {
  return (
    <>
      <h1>문서 업로드</h1>
      <RequireAuth>
        <UploadView />
      </RequireAuth>
    </>
  );
}
