import { useState } from "react";

const SUB_TABS = [
  { id: "facts", label: "Dữ kiện" },
  { id: "script", label: "Kịch bản" },
  { id: "product", label: "Sản phẩm" },
] as const;

type SubTab = (typeof SUB_TABS)[number]["id"];

const CONTENT: Record<SubTab, { title: string; body: string[] }> = {
  facts: {
    title: "Dữ kiện",
    body: [
      "Nhận WorkspacePackage từ Analyzer hoặc import portable đã kiểm checksum.",
      "Chọn clip được phép dùng, đặt In/Out, pin khoảnh khắc bắt buộc.",
      "Dữ kiện người dùng được lưu tách lớp khỏi bằng chứng thị giác.",
    ],
  },
  script: {
    title: "Kịch bản",
    body: [
      "Qwen3-8B offline sinh đúng ba phương án khác nhau về cấu trúc kể chuyện.",
      "Mỗi beat có source span trỏ tới asset/scene/frame có thật.",
      "Claim được phân loại: evidence · user_note · creative_opinion.",
    ],
  },
  product: {
    title: "Sản phẩm",
    body: [
      "Kokoro-Vietnamese lồng tiếng theo từng beat; thời lượng đo từ WAV thật.",
      "Shot planner + timeline preview chỉnh được, không chạy lại LLM.",
      "FFmpeg render MP4 9:16 và QA bằng ffprobe + decode đầu/giữa/cuối.",
    ],
  },
};

export default function PovTab() {
  const [sub, setSub] = useState<SubTab>("facts");
  const content = CONTENT[sub];

  return (
    <div className="pov">
      <div className="pov__lock" role="note">
        <strong>POV bị khoá đến khi G0–G5 PASS.</strong>
        <span>
          Backend từ chối mọi yêu cầu POV bằng mã <code>POV_LOCKED_UNTIL_G5</code>. Không có engine, không có
          kịch bản mẫu, không có nút giả.
        </span>
      </div>

      <div className="sub-tabs" role="tablist" aria-label="Các bước POV">
        {SUB_TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={sub === entry.id}
            className={sub === entry.id ? "sub-tab sub-tab--active" : "sub-tab"}
            onClick={() => setSub(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <section className="panel">
        <div className="panel__head">
          <h2>{content.title}</h2>
          <span className="badge badge--locked">S06–S08</span>
        </div>
        <ul className="feature-list">
          {content.body.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p className="hint">Spec tham chiếu: docs/02_SPEC_POV_PIPELINE.md</p>
      </section>
    </div>
  );
}
