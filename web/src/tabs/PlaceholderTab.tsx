interface Props {
  title: string;
  note: string;
}

export default function PlaceholderTab({ title, note }: Props) {
  return (
    <div className="panel placeholder">
      <div className="panel__head">
        <h2>{title}</h2>
        <span className="badge badge--pending">Placeholder</span>
      </div>
      <p className="empty-state__title">Chưa triển khai</p>
      <p>{note}</p>
      <p className="hint">
        Không có route, không có model, không có dữ liệu mẫu. Tab này tồn tại để giữ đúng bốn tab cấp cao.
      </p>
    </div>
  );
}
