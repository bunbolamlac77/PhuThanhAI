import type { ContextScene } from "../api";

interface Props {
  scene: ContextScene;
  onAction: (
    action: "confirm" | "edit" | "dismiss",
    labelKey: string,
    labelVi: string,
    sceneId: string,
    frameId: string | null,
  ) => void;
  onViewFrame: (frameId: string) => void;
}

export default function SceneContextCard({ scene, onAction, onViewFrame }: Props) {
  return (
    <div className="scene-context">
      <div className="scene-context__head">
        <strong>{scene.scene_id}</strong>
        <span className="hint">
          {scene.start_ms}–{scene.end_ms} ms · {scene.frame_ids.length} frame
        </span>
      </div>
      {scene.candidates.length === 0 ? <p className="hint">Không có gợi ý đủ bằng chứng.</p> : null}
      {scene.candidates.map((candidate) => (
        <div key={candidate.label_key} className="context-candidate">
          <div>
            <strong>{candidate.label_vi}</strong>
            <span className="badge">gợi ý</span>
            <p className="hint">{candidate.description_vi}</p>
            <p className="hint">{candidate.uncertainty_vi}</p>
            <div className="context-frames">
              {candidate.evidence_frames.map((frameId) => (
                <button key={frameId} type="button" className="link" onClick={() => onViewFrame(frameId)}>
                  {frameId}
                </button>
              ))}
            </div>
          </div>
          <div className="context-actions">
            <button
              type="button"
              onClick={() => onAction("confirm", candidate.label_key, candidate.label_vi, scene.scene_id, null)}
            >
              Xác nhận nhãn
            </button>
            <button
              type="button"
              onClick={() => onAction("edit", candidate.label_key, candidate.label_vi, scene.scene_id, null)}
            >
              Sửa nhãn
            </button>
            <button
              type="button"
              onClick={() => onAction("dismiss", candidate.label_key, candidate.label_vi, scene.scene_id, null)}
            >
              Bỏ nhãn gợi ý
            </button>
          </div>
        </div>
      ))}
      {scene.user_labels.map((label) => (
        <div key={`${label.label_key}-${label.annotation_id}`} className="context-label">
          <strong>{label.label_vi}</strong>
          <span className="badge badge--ok">người dùng xác nhận</span>
          <span className="hint">annotation {label.annotation_id.slice(0, 8)}</span>
        </div>
      ))}
    </div>
  );
}
