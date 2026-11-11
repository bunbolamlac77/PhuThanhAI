import { useState } from "react";
import type { Annotation, Asset } from "../api";
import { formatShort, formatTimecode } from "../lib/format";

export type SaveStatus = "idle" | "dirty" | "saving" | "saved" | "error" | "conflict";

interface Props {
  asset: Asset | null;
  projectNotes: Annotation[];
  clipNote: Annotation | null;
  rangeNotes: Annotation[];
  draft: string;
  status: SaveStatus;
  errorMessage: string | null;
  suggestion: { time_of_day: string } | null;
  inMs: number | null;
  outMs: number | null;
  onChangeDraft: (text: string) => void;
  onFlush: () => void;
  onReload: () => void;
  onUseSuggestion: () => void;
  onCreateRange: (text: string) => void;
  onCreateProjectNote: (text: string) => void;
  onDeleteNote: (note: Annotation) => void;
  onSeek: (ms: number) => void;
  onSetIn: () => void;
  onSetOut: () => void;
}

const STATUS_TEXT: Record<SaveStatus, string> = {
  idle: "Chưa thay đổi",
  dirty: "Đang chờ lưu…",
  saving: "Đang lưu…",
  saved: "Đã lưu",
  error: "Lỗi lưu",
  conflict: "Xung đột phiên bản",
};

export default function NotesPanel({
  asset,
  projectNotes,
  clipNote,
  rangeNotes,
  draft,
  status,
  errorMessage,
  suggestion,
  inMs,
  outMs,
  onChangeDraft,
  onFlush,
  onReload,
  onUseSuggestion,
  onCreateRange,
  onCreateProjectNote,
  onDeleteNote,
  onSeek,
  onSetIn,
  onSetOut,
}: Props) {
  const [rangeText, setRangeText] = useState("");
  const [projectText, setProjectText] = useState("");

  if (!asset) {
    return (
      <section className="panel" aria-label="Ghi chú">
        <div className="panel__head">
          <h2>3 · Ghi chú</h2>
        </div>
        <div className="empty-state">
          <p className="empty-state__title">Chọn một clip để ghi chú</p>
          <p>Ghi chú thuộc project, clip hoặc một đoạn [In, Out). Giờ thực tế luôn tách khỏi timecode.</p>
        </div>
        <ProjectNotes notes={projectNotes} onCreate={onCreateProjectNote} text={projectText} setText={setProjectText} />
      </section>
    );
  }

  return (
    <section className="panel" aria-label="Ghi chú">
      <div className="panel__head">
        <h2>3 · Ghi chú</h2>
        <span className={`badge ${status === "saved" ? "badge--ok" : status === "conflict" || status === "error" ? "badge--danger" : ""}`}>
          {STATUS_TEXT[status]}
        </span>
      </div>

      <div className="note-target" title={asset.filename}>
        {asset.filename}
      </div>

      {clipNote?.wall_clock ? (
        <div className="wall-clock">
          Giờ thực tế do bạn xác nhận: <strong>{clipNote.wall_clock.time_of_day}</strong>
          <span className="hint"> — không phải timecode video.</span>
        </div>
      ) : null}

      <textarea
        id="note-input"
        className="note-input"
        value={draft}
        placeholder="Ví dụ: Lên phà lúc 5 giờ sáng, đợi khoảng 30 phút…"
        aria-label="Ghi chú cho clip"
        onChange={(event) => onChangeDraft(event.target.value)}
        onBlur={onFlush}
      />

      {suggestion ? (
        <div className="suggestion">
          Gợi ý giờ thực tế: <strong>{suggestion.time_of_day}</strong>
          <button type="button" onClick={onUseSuggestion}>
            Dùng {suggestion.time_of_day}
          </button>
        </div>
      ) : null}

      {status === "conflict" ? (
        <div className="result result--bad">
          {errorMessage ?? "Ghi chú đã bị thay đổi ở nơi khác."}
          <button type="button" onClick={onReload}>
            Tải bản mới nhất
          </button>
        </div>
      ) : null}
      {status === "error" ? (
        <div className="result result--bad">
          {errorMessage ?? "Không lưu được ghi chú."}
          <button type="button" onClick={onFlush}>
            Thử lại
          </button>
        </div>
      ) : null}

      <div className="range-editor">
        <div className="range-editor__marks">
          <button type="button" onClick={onSetIn}>
            In {inMs !== null ? formatShort(inMs) : "—"}
          </button>
          <button type="button" onClick={onSetOut}>
            Out {outMs !== null ? formatShort(outMs) : "—"}
          </button>
        </div>
        <div className="range-editor__row">
          <input
            type="text"
            value={rangeText}
            placeholder="Ghi chú cho đoạn [In, Out)…"
            aria-label="Nội dung ghi chú cho đoạn"
            onChange={(event) => setRangeText(event.target.value)}
          />
          <button
            type="button"
            disabled={!rangeText.trim() || inMs === null || outMs === null || outMs <= inMs}
            onClick={() => {
              onCreateRange(rangeText.trim());
              setRangeText("");
            }}
          >
            Lưu đoạn
          </button>
        </div>
      </div>

      {rangeNotes.length > 0 ? (
        <ul className="note-list">
          {rangeNotes.map((note) => (
            <li key={note.annotation_id}>
              <button type="button" className="link-button" onClick={() => onSeek(note.start_ms ?? 0)}>
                {formatTimecode(note.start_ms)}–{formatTimecode(note.end_ms)}
              </button>
              <span>{note.text}</span>
              <button type="button" className="link-button" onClick={() => onDeleteNote(note)} aria-label="Xoá ghi chú đoạn">
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <ProjectNotes notes={projectNotes} onCreate={onCreateProjectNote} text={projectText} setText={setProjectText} />
    </section>
  );
}

function ProjectNotes({
  notes,
  onCreate,
  text,
  setText,
}: {
  notes: Annotation[];
  onCreate: (text: string) => void;
  text: string;
  setText: (value: string) => void;
}) {
  return (
    <details className="project-notes">
      <summary>Ghi chú dự án ({notes.length})</summary>
      <ul className="note-list">
        {notes.map((note) => (
          <li key={note.annotation_id}>
            <span>{note.text}</span>
          </li>
        ))}
      </ul>
      <div className="range-editor__row">
        <input
          type="text"
          value={text}
          placeholder="Ghi chú cho toàn dự án…"
          onChange={(event) => setText(event.target.value)}
        />
        <button
          type="button"
          disabled={!text.trim()}
          onClick={() => {
            onCreate(text.trim());
            setText("");
          }}
        >
          Thêm
        </button>
      </div>
    </details>
  );
}
