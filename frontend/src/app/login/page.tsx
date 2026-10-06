import { Suspense } from "react";
import { Loading } from "@/components/Msg";
import LoginForm from "./LoginForm";

export const metadata = { title: "로그인·가입 — 제때받기" };

export default function LoginPage() {
  return (
    <>
      <h1>로그인·가입</h1>
      <Suspense fallback={<Loading />}>
        <LoginForm />
      </Suspense>
    </>
  );
}
