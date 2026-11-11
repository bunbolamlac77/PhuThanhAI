import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { fetchHealth, fetchSession, setSession, type Health, type SessionInfo } from "./api";
import LibraryTab from "./tabs/LibraryTab";
import PovTab from "./tabs/PovTab";
import PlaceholderTab from "./tabs/PlaceholderTab";

const TABS = [
  { id: "library", label: "Thư viện & Analyzer" },
  { id: "pov", label: "AI Video POV" },
  { id: "part3", label: "Phần 3" },
  { id: "part4", label: "Phần 4" },
] as const;
type TabId = (typeof TABS)[number]["id"];

type Connection =
  | { state: "connecting" }
  | { state: "offline"; message: string }
  | { state: "online"; health: Health; session: SessionInfo | null };

function ConnectionIndicator({ connection }: { connection: Connection }) {
  if (connection.state === "connecting") {
    return (
      <span className="studio-connection studio-connection--pending" role="status">
        <span className="studio-connection__dot" /> Đang kết nối
      </span>
    );
  }
  if (connection.state === "offline") {
    return (
      <span className="studio-connection studio-connection--error" role="status" title={connection.message}>
        <span className="studio-connection__dot" /> Backend chưa kết nối
      </span>
    );
  }
  const { health, session } = connection;
  const ready = health.loopback_only && health.offline_enforced && Boolean(session);
  const details = [
    health.loopback_only ? "Loopback: có" : "Loopback: cần kiểm tra",
    health.offline_enforced ? "Offline: enforced" : "Offline: chưa xác nhận",
    health.heavy_slot_busy ? "AI_HEAVY: đang dùng" : "AI_HEAVY: trống",
    session ? "Session: hợp lệ" : "Session: chưa có",
  ].join(" · ");
  return (
    <div className="studio-health">
      <span
        className={ready ? "studio-connection studio-connection--ok" : "studio-connection studio-connection--pending"}
        role="status"
        title={details}
      >
        <span className="studio-connection__dot" /> {ready ? "Local · Offline" : "Cần kiểm tra"}
      </span>
      <span className="studio-ai-state" title={details}>
        {health.heavy_slot_busy ? "AI đang xử lý" : "AI slot trống"}
      </span>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<TabId>("library");
  const [connection, setConnection] = useState<Connection>({ state: "connecting" });

  const bootstrap = useCallback(async () => {
    try {
      const health = await fetchHealth();
      let session: SessionInfo | null = null;
      try {
        session = await fetchSession();
      } catch {
        session = null;
      }
      setSession(session);
      setConnection({ state: "online", health, session });
    } catch (error) {
      setSession(null);
      setConnection({
        state: "offline",
        message: error instanceof Error ? error.message : "Lỗi kết nối không xác định",
      });
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const online = connection.state === "online";
  const health = online ? connection.health : null;
  const session = online ? connection.session : null;

  const onTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const index = TABS.findIndex((entry) => entry.id === tab);
    const delta = event.key === "ArrowRight" ? 1 : -1;
    const nextIndex = (index + delta + TABS.length) % TABS.length;
    const next = TABS[nextIndex];
    if (!next) return;
    setTab(next.id);
    event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex]?.focus();
  };

  const body = useMemo(() => {
    switch (tab) {
      case "library":
        return <LibraryTab online={online} health={health} session={session} />;
      case "pov":
        return <PovTab />;
      case "part3":
        return <PlaceholderTab title="Phần 3" note="Module tương lai. Chưa triển khai." />;
      case "part4":
        return <PlaceholderTab title="Phần 4" note="Module tương lai. Chưa triển khai." />;
    }
  }, [tab, online, health, session]);

  return (
    <div className="app-shell">
      <header className="app-header studio-topbar">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">P</span>
          <span className="brand__text">
            <strong>PhuThanhAI</strong>
            <small>Studio · v{health?.product_version ?? "3.0.0"}</small>
          </span>
        </div>

        <div className="tabs" role="tablist" aria-label="Khu vực ứng dụng" onKeyDown={onTabKeyDown}>
          {TABS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={tab === entry.id}
              className={tab === entry.id ? "tab tab--active" : "tab"}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>

        <ConnectionIndicator connection={connection} />
      </header>

      <main className="app-body studio-main">{body}</main>

      <footer className="app-footer studio-footer">
        <span>Local · Offline · macOS · Footage nguồn chỉ đọc · Universal Video Analyzer</span>
        <button type="button" className="link-button" onClick={() => void bootstrap()}>
          Kiểm tra kết nối
        </button>
      </footer>
    </div>
  );
}
