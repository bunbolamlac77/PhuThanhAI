interface Props {
  value: number | null;
  onChange: (rating: number | null) => void;
  disabled?: boolean;
}

export default function StarRating({ value, onChange, disabled = false }: Props) {
  return (
    <div className="stars" role="group" aria-label="Đánh giá 1 đến 5 sao">
      {[1, 2, 3, 4, 5].map((star) => {
        const active = value !== null && star <= value;
        return (
          <button
            key={star}
            type="button"
            className={active ? "star star--active" : "star"}
            aria-label={`${star} sao`}
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(value === star ? null : star)}
          >
            ★
          </button>
        );
      })}
    </div>
  );
}
