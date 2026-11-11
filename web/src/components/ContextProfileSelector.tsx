import { useEffect, useRef, useState } from "react";
import type { Project } from "../api";
import { fetchProjectProfile, updateProjectProfile } from "../api";

const OPTIONS = [
  { key: "general", label: "Tự động / Tổng quát" },
  { key: "wedding_traditional", label: "Đám cưới truyền thống Việt Nam" },
  { key: "event", label: "Sự kiện" },
  { key: "vlog_travel", label: "Đời sống / Du lịch / Vlog" },
  { key: "documentary", label: "Phóng sự / Tài liệu" },
  { key: "pov_footage", label: "POV" },
  { key: "custom", label: "Tùy chỉnh" },
];

interface Props {
  project: Project;
  online: boolean;
  onChanged: (project: Project) => void;
}

export default function ContextProfileSelector({ project, online, onChanged }: Props) {
  const [profile, setProfile] = useState(project.analysis_profile || "general");
  const [revision, setRevision] = useState(project.profile_revision ?? 1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const projectRef = useRef(project.project_id);

  useEffect(() => {
    projectRef.current = project.project_id;
    setProfile(project.analysis_profile || "general");
    setRevision(project.profile_revision ?? 1);
    void (async () => {
      try {
        const fresh = await fetchProjectProfile(project.project_id);
        if (projectRef.current !== project.project_id) return;
        setProfile(fresh.analysis_profile);
        setRevision(fresh.profile_revision);
        onChanged({ ...project, analysis_profile: fresh.analysis_profile, profile_revision: fresh.profile_revision });
      } catch {
        // keep the cached value; the next save will surface a conflict
      }
    })();
    // The project identity is the only trigger; profile changes come from the server.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.project_id]);

  async function save(next: string) {
    if (next === profile || !online || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateProjectProfile(project.project_id, next, revision);
      if (projectRef.current !== project.project_id) return;
      setProfile(updated.analysis_profile);
      setRevision(updated.profile_revision);
      onChanged({
        ...project,
        analysis_profile: updated.analysis_profile,
        profile_revision: updated.profile_revision,
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không đổi được môi trường");
      try {
        const fresh = await fetchProjectProfile(project.project_id);
        if (projectRef.current === project.project_id) {
          setProfile(fresh.analysis_profile);
          setRevision(fresh.profile_revision);
        }
      } catch {
        // leave the conflict message visible
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="context-profile">
      <label htmlFor="context-profile-select">Môi trường phân tích</label>
      <select
        id="context-profile-select"
        value={profile}
        disabled={!online || busy}
        onChange={(event) => void save(event.target.value)}
      >
        {OPTIONS.map((option) => (
          <option key={option.key} value={option.key}>
            {option.label}
          </option>
        ))}
      </select>
      <span className="hint">lưu theo project · rev {revision}</span>
      {error ? <span className="result result--bad">{error}</span> : null}
    </div>
  );
}
