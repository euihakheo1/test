import CheckView from "./CheckView";

export const metadata = {
  title: "무료 검산 — 제때받기",
  description: "거래 유형·기준일·지급일·금액으로 법정 지급기한과 지연이자 계산 방식을 확인합니다(로그인 없음).",
};

export default function CheckPage() {
  return (
    <>
      <h1>무료 검산</h1>
      <p>
        로그인 없이 거래 하나의 법정 지급기한·지연일수·지연이자를 계산해 봅니다. 입력값은 계산에만 쓰이며 이 화면은
        저장하지 않습니다.
      </p>
      <CheckView />
    </>
  );
}
