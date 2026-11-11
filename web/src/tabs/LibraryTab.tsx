import { useState } from "react";
import { fetchContracts, recheckContracts, type ContractCheck, type Health, type SessionInfo } from "../api";

interface Props {
  online: boolean;
  health: Health | null;
  session: SessionInfo | null;
}

function Pending({ children }: { children: React.ReactNode }) {
  return <span className="badge badge--pending">{children}</span>;
}

export default function LibraryTab({ online, health, session }: Props) {
  const [check, setCheck] = useState<ContractCheck | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function runCheck(useCapability: boolean) {
    setBusy(true);
    setCheckError(null);
    try {
      const result = useCapability && session ? await recheckContracts(session) : await fetchContracts();
      setCheck(result);
    } catch (error) {
      setCheckError(error instanceof Error ? error.message : "UNKNOWN");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="library-grid">
      <section className="panel" aria-label="Thư viện media">
        <div className="panel__head">
          <h2>1 · Thư viện</h2>
          <Pending>S01</Pending>
        </div>
        <input
          className="search"
          type="search"
          placeholder="Tìm video, ghi chú, tag…"
          disabled
          aria-label="Tìm video (chưa khả dụng ở S00)"
        />
        <div className="toolbar toolbar--muted">
          <button type="button" disabled>
            + Thư mục
          </button>
          <button type="button" disabled>
            + Video
          </button>
          <button type="button" disabled>
            Chọn nhiều
          </button>
        </div>
        <div className="empty-state">
          <p className="empty-state__title">Chưa có asset nào được index</p>
          <p>
            Quét thư mục, thumbnail, lưới ảo hoá và tìm kiếm thuộc <strong>S01</strong>. Ở S00 không có endpoint
            asset nào được expose để tránh hiển thị dữ liệu giả.
          </p>
        </div>
        <ul className="legend">
          <li>
            <span className="dot dot--reviewed" /> đã review
          </li>
          <li>
            <span className="dot dot--attention" /> cần xem lại
          </li>
          <li>
            <span className="dot dot--missing" /> nguồn đã mất
          </li>
        </ul>
      </section>

      <section className="panel panel--center" aria-label="Video player">
        <div className="panel__head">
          <h2>2 · Video player</h2>
          <Pending>S01</Pending>
        </div>
        <div className="player-frame">
          <div className="player-frame__inner">
            <span className="player-frame__placeholder">Chưa có clip</span>
            <span className="player-frame__hint">
              Một player active · HTTP Range 206 · proxy on-demand · ánh xạ PTS về nguồn
            </span>
          </div>
        </div>
        <div className="transport" aria-hidden="true">
          <button type="button" disabled>
            ⏮
          </button>
          <button type="button" disabled>
            ▶
          </button>
          <button type="button" disabled>
            ⏭
          </button>
          <span className="transport__time">00:00:00.000 / 00:00:00.000</span>
        </div>
        <p className="hint">
          Phím tắt sẽ bỏ qua khi con trỏ đang ở ô nhập hoặc IME tiếng Việt đang bật — quy tắc này được kiểm
          bằng test ở S01.
        </p>
      </section>

      <section className="panel" aria-label="Ghi chú và bằng chứng">
        <div className="panel__head">
          <h2>3 · Ghi chú / AI</h2>
          <Pending>S01–S03</Pending>
        </div>
        <div className="note-kinds">
          <div>
            <span className="tag tag--user">Người dùng</span>
            <p>Ghi chú project / clip / time-range; giờ thực tế tách khỏi timecode video.</p>
          </div>
          <div>
            <span className="tag tag--observed">AI quan sát</span>
            <p>Chỉ bằng chứng từ frame đã lấy mẫu, kèm frame ID và PTS nguồn.</p>
          </div>
          <div>
            <span className="tag tag--hypothesis">Giả thuyết</span>
            <p>Suy luận chưa đủ bằng chứng, luôn tách khỏi quan sát trực tiếp.</p>
          </div>
          <div>
            <span className="tag tag--correction">Hiệu chỉnh</span>
            <p>Overlay có audit, không ghi đè mô tả gốc của model.</p>
          </div>
        </div>

        <div className="system-card">
          <h3>Trạng thái hệ thống</h3>
          <dl>
            <div>
              <dt>Backend</dt>
              <dd>{online ? "online" : "offline"}</dd>
            </div>
            <div>
              <dt>Data root</dt>
              <dd>{health?.data_root_configured ? "đã cấu hình" : "chưa sẵn sàng"}</dd>
            </div>
            <div>
              <dt>Model trong process API</dt>
              <dd>{health ? (health.model_loaded_in_process ? "có" : "không") : "—"}</dd>
            </div>
            <div>
              <dt>AI_HEAVY slot</dt>
              <dd>{health ? (health.heavy_slot_busy ? "bận" : "trống") : "—"}</dd>
            </div>
          </dl>

          <div className="toolbar">
            <button type="button" disabled={!online || busy} onClick={() => void runCheck(false)}>
              Kiểm tra contract V2/V3
            </button>
            <button type="button" disabled={!online || busy || !session} onClick={() => void runCheck(true)}>
              Kiểm tra lại (có capability)
            </button>
          </div>

          {check ? (
            <div className={check.status === "pass" ? "result result--ok" : "result result--bad"}>
              <strong>{check.status === "pass" ? "PASS" : "FAIL"}</strong>
              <ul>
                {check.kinds.map((kind) => (
                  <li key={kind.name}>
                    {kind.name}: schema {kind.schema_valid ? "ok" : "fail"} · semantic{" "}
                    {kind.semantic_valid ? "ok" : "fail"}
                    {kind.negative_fixtures_rejected.length > 0
                      ? ` · negative bị từ chối: ${kind.negative_fixtures_rejected.length}`
                      : ""}
                  </li>
                ))}
                <li>portable roundtrip: {check.portable_roundtrip_ok ? "ok" : "fail"}</li>
                <li>checksum giả bị từ chối: {check.checksum_tamper_rejected ? "ok" : "fail"}</li>
              </ul>
            </div>
          ) : null}
          {checkError ? <p className="result result--bad">Lỗi: {checkError}</p> : null}
        </div>
      </section>
    </div>
  );
}
