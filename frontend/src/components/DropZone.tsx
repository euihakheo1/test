"use client";

import { useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { UPLOAD_EXTENSIONS, bytes, checkUploadFile } from "@/lib/fmt";

export const MAX_UPLOAD_BYTES =
  Math.max(1, Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? "20") || 20) * 1024 * 1024;

/** 끌어다 놓기 + 클릭 선택. 형식·크기를 미리 검사한다(서버도 다시 검사함). */
export function DropZone({
  file,
  onFile,
  disabled,
}: {
  file: File | null;
  onFile: (f: File | null, problem: string | null) => void;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  function pick(f: File | undefined) {
    if (!f) return;
    onFile(f, checkUploadFile(f.name, f.size, MAX_UPLOAD_BYTES));
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    if (disabled) return;
    const files = e.dataTransfer.files;
    if (files.length > 1) {
      onFile(null, "한 번에 파일 하나만 올릴 수 있습니다.");
      return;
    }
    pick(files[0]);
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.current?.click();
    }
  }

  return (
    <div>
      <div
        className={`drop${over ? " over" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="파일 선택 또는 끌어다 놓기"
        aria-disabled={disabled || undefined}
        onClick={() => !disabled && input.current?.click()}
        onKeyDown={onKey}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        {file ? (
          <>
            <b>{file.name}</b> <span className="muted">({bytes(file.size)})</span>
            <div className="hint">다른 파일을 놓거나 눌러서 바꿀 수 있습니다.</div>
          </>
        ) : (
          <>
            <b>파일을 여기에 끌어다 놓거나 눌러서 고르세요</b>
            <div className="hint">
              형식: {UPLOAD_EXTENSIONS.join(", ")} · 최대 {bytes(MAX_UPLOAD_BYTES)}
            </div>
          </>
        )}
      </div>
      <input
        ref={input}
        type="file"
        hidden
        accept={UPLOAD_EXTENSIONS.join(",")}
        onChange={(e) => {
          pick(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      <ul className="plain hint">
        <li>CSV(.csv, .txt): UTF-8과 CP949(엑셀 기본 저장) 모두 읽습니다. 은행 거래내역, 홈택스 목록 등.</li>
        <li>엑셀(.xlsx): 홈택스 세금계산서 목록, 유통사 정산서. 매크로 파일과 옛 .xls 형식은 받지 않습니다.</li>
        <li>
          PDF(.pdf): 글자를 선택할 수 있는 텍스트 PDF만 읽습니다. 스캔(이미지) PDF는 OCR이 설정되지 않으면
          &lsquo;처리 실패&rsquo;로 표시되며, 거래가 없다는 뜻이 아닙니다.
        </li>
        <li>최대 크기는 서버 설정(JETTAE_MAX_UPLOAD_BYTES, 기본 20MB)을 따릅니다.</li>
        <li>같은 내용의 파일을 다시 올리면 새로 저장하지 않고 기존 문서를 알려 줍니다.</li>
      </ul>
    </div>
  );
}
