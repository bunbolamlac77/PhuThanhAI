import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchHealth, fetchSession, type Health, type SessionInfo } from "./api";
import LibraryTab from "./tabs/LibraryTab";
import PovTab from "./tabs/PovTab";
import PlaceholderTab from "./tabs/PlaceholderTab";

const TABS = [
  { id: "library", label: "Thư viện & Video Analyzer" },
  { id: "pov", label: "AI Video POV" },
  { id: "part3", label: "Phần 3" },
  { id: "part4", label: "Phần 4" },
] as const;

type TabId = (typeof TABS)[number]["id"];

type Connection =
  | { state: "connecting" }
  | { state: "online"; health: Health; session: SessionInfo | null }
  | { state: "offline"; code: string };

function StatusPill({ connection }: { connection: Connection }) {
  if (connection.state === "connecting") {
    return (
      <span className="pill pill--pending" role="status">
        Đang kết nối backend…
      </span>
    );
  }
  if (connection.state === "offline") {
    return (
      <span className="pill pill--error" role="status">
        Backend ngoại tuyến · {connection.code}
      </span>
    );
  }
  const { health, session } = connection;
  return (
    <span className="status-group">
      <span className="pill pill--ok" role="status">
        {health.loopback_only ? "Local 127.0.0.1" : "Cảnh báo: không phải loopback"}
      </span>
      <span className={health.offline_enforced ? "pill pill--ok" : "pill pill--warn"}>
        {health.offline_enforced ? "Offline enforced" : "Offline chưa xác nhận"}
      </span>
      <span className={health.heavy_slot_busy ? "pill pill--busy" : "pill pill--idle"}>
        {health.heavy_slot_busy ? "AI slot: bận" : "AI slot: trống"}
      </span>
      {session ? (
        <span className="pill pill--ok" title="Capability nhận qua body, không nằm trong URL">
          Phiên đã xác thực
        </span>
      ) : (
        <span className="pill pill--warn">Chưa có capability</span>
      )}
    </span>
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
      setConnection({ state: "online", health, session });
    } catch (error) {
      setConnection({ state: "offline", code: error instanceof Error ? error.message : "UNKNOWN" });
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const online = connection.state === "online";
  const health = online ? connection.health : null;
  const session = online ? connection.session : null;

  const onTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const currentIndex = TABS.findIndex((entry) => entry.id === tab);
      const delta = event.key === "ArrowRight" ? 1 : -1;
      const nextIndex = (currentIndex + delta + TABS.length) % TABS.length;
      const next = TABS[nextIndex];
      if (next) {
        event.preventDefault();
        setTab(next.id);
      }
    },
    [tab],
  );

  const body = useMemo(() => {
    switch (tab) {
      case "library":
        return <LibraryTab online={online} health={health} session={session} />;
      case "pov":
        return <PovTab />;
      case "part3":
        return <PlaceholderTab title="Phần 3" note="Module tương lai. Chưa có nghiệp vụ nào được triển khai." />;
      case "part4":
        return <PlaceholderTab title="Phần 4" note="Module tương lai. Chưa có nghiệp vụ nào được triển khai." />;
    }
  }, [tab, online, health, session]);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            P
          </span>
          <span className="brand__text">
            <strong>PhuThanhAI</strong>
            <small>v{health?.product_version ?? "3.0.0"} · contract {health?.contract_version ?? "1.0.0"}</small>
          </span>
        </div>

        <div className="tabs" role="tablist" aria-label="Khu vực chính" onKeyDown={onTabKeyDown}>
          {TABS.map((entry) => (
            <button
              key={entry.id}
              role="tab"
              type="button"
              aria-selected={tab === entry.id}
              className={tab === entry.id ? "tab tab--active" : "tab"}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>

        <StatusPill connection={connection} />
      </header>

      <main className="app-body">{body}</main>

      <footer className="app-footer">
        <span>S00 · Foundation &amp; Doctor — thư viện, player và ghi chú thuộc S01.</span>
        <button type="button" className="link-button" onClick={() => void bootstrap()}>
          Kiểm tra lại backend
        </button>
      </footer>
    </div>
  );
}
