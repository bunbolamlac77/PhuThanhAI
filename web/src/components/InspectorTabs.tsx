import { useId, useState, type KeyboardEvent, type ReactNode } from "react";

type InspectorTab = "notes" | "analysis";

interface Props {
  filename: string | null;
  notes: ReactNode;
  analysis: ReactNode;
}

/**
 * Both panels stay mounted while switching tabs, so note drafts, autosave state
 * and sampling/analysis polling survive Inspector navigation.
 */
export default function InspectorTabs({ filename, notes, analysis }: Props) {
  const [active, setActive] = useState<InspectorTab>("notes");
  const uid = useId();
  const notesId = `${uid}-notes`;
  const analysisId = `${uid}-analysis`;

  const handleKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next: InspectorTab = active === "notes" ? "analysis" : "notes";
    setActive(next);
    event.currentTarget
      .querySelector<HTMLButtonElement>(`[data-inspector-tab="${next}"]`)
      ?.focus();
  };

  return (
    <aside className="studio-inspector" aria-label="Inspector">
      <header className="studio-inspector__header">
        <div>
          <span className="studio-eyebrow">WORKSPACE</span>
          <h2>Inspector</h2>
        </div>
        <span className="studio-inspector__status">
          {filename ? "Clip đang chọn" : "Chưa chọn clip"}
        </span>
      </header>

      {filename ? (
        <div className="studio-inspector__filename" title={filename}>
          {filename}
        </div>
      ) : null}

      <div
        className="studio-inspector__tabs"
        role="tablist"
        aria-label="Chế độ Inspector"
        onKeyDown={handleKeys}
      >
        <button
          type="button"
          data-inspector-tab="notes"
          role="tab"
          aria-controls={notesId}
          aria-selected={active === "notes"}
          tabIndex={active === "notes" ? 0 : -1}
          className={active === "notes" ? "studio-inspector__tab is-active" : "studio-inspector__tab"}
          onClick={() => setActive("notes")}
        >
          Ghi chú
        </button>
        <button
          type="button"
          data-inspector-tab="analysis"
          role="tab"
          aria-controls={analysisId}
          aria-selected={active === "analysis"}
          tabIndex={active === "analysis" ? 0 : -1}
          className={active === "analysis" ? "studio-inspector__tab is-active" : "studio-inspector__tab"}
          onClick={() => setActive("analysis")}
        >
          Phân tích AI
        </button>
      </div>

      <div
        id={notesId}
        role="tabpanel"
        aria-labelledby={`${uid}-tab-notes`}
        className="studio-inspector__content"
        hidden={active !== "notes"}
      >
        {notes}
      </div>
      <div
        id={analysisId}
        role="tabpanel"
        aria-labelledby={`${uid}-tab-analysis`}
        className="studio-inspector__content"
        hidden={active !== "analysis"}
      >
        {analysis}
      </div>
    </aside>
  );
}
