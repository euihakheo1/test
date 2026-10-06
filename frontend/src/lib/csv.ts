/**
 * 열 매핑 화면의 미리보기용 최소 CSV 파서(RFC 4180 따옴표 처리). 처음 maxRows 행만 읽는다.
 * 실제 읽기(사실·원문 위치 생성)는 서버의 수집 모듈이 한다. 여기서는 표시만 한다.
 */
export function parseCsvPreview(text: string, maxRows = 6, delimiter?: string): string[][] {
  const delim = delimiter ?? guessDelimiter(text);
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let i = 0;
  while (i < text.length && rows.length < maxRows) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i++;
        continue;
      }
      cell += ch;
      i++;
      continue;
    }
    if (ch === '"' && cell === "") {
      quoted = true;
      i++;
      continue;
    }
    if (ch === delim) {
      row.push(cell);
      cell = "";
      i++;
      continue;
    }
    if (ch === "\r" || ch === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      i += ch === "\r" && text[i + 1] === "\n" ? 2 : 1;
      continue;
    }
    cell += ch;
    i++;
  }
  if (rows.length < maxRows && (cell !== "" || row.length > 0)) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export function guessDelimiter(text: string): string {
  const first = text.split(/\r?\n/, 1)[0] ?? "";
  const counts: [string, number][] = [",", "\t", ";", "|"].map((d) => [d, first.split(d).length - 1]);
  counts.sort((a, b) => b[1] - a[1]);
  return counts[0][1] > 0 ? counts[0][0] : ",";
}
