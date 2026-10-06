import Link from "next/link";

export default function Home() {
  return (
    <>
      <h1>제때받기</h1>
      <p>
        정산서·세금계산서·입금 내역·약정서를 올리면 거래별로 <b>금액 차이</b>, <b>지급기한·지연일수·지연이자</b>
        (기준일이 확인된 거래만), <b>계산에 필요한 서류</b>, 각 숫자의 <b>원문 위치</b>를 보여 줍니다.
      </p>
      <div className="grid2">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>순서</h2>
          <ol className="plain">
            <li>
              <Link href="/upload">문서 업로드</Link> — CSV·XLSX·텍스트 PDF
            </li>
            <li>열 이름을 알아보지 못한 문서는 열 매핑 확인</li>
            <li>
              <Link href="/analysis">분석 실행</Link> → 작업 상태 확인
            </li>
            <li>
              <Link href="/results">결과 목록</Link>과 상세(사실·원문 위치·적용 규칙)
            </li>
            <li>
              <Link href="/changes">정정·추가 자료</Link>를 올리고 이전/현재 비교
            </li>
            <li>확인(승인) 후 <Link href="/report">보고서 내보내기</Link></li>
          </ol>
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>로그인 없이</h2>
          <p>
            <Link href="/check">무료 검산</Link>: 거래 유형·기준일·지급일·금액만 넣어 법정 지급기한과 지연이자 계산
            방식을 확인합니다.
          </p>
          <h3>이 서비스가 하지 않는 것</h3>
          <p className="muted small">
            법적 판단, 권리 확정, 입금 여부 예측, 외부 청구·독촉 대리. 결과는 차이 / 필요 서류 / 확인이 필요한 조건 /
            관련 근거로만 표시됩니다.
          </p>
        </div>
      </div>
    </>
  );
}
