interface Props {
  title: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  onClose: () => void;
}

export function ConfigDrawer({ title, options, selected, onChange, onClose }: Props) {
  function toggle(option: string) {
    if (selected.includes(option)) {
      onChange(selected.filter((s) => s !== option));
    } else {
      onChange([...selected, option]);
    }
  }

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <div className="drawer">
        <h3>{title}</h3>
        <div className="checkbox-list">
          {options.map((option) => (
            <label key={option} className="checkbox-item">
              <input
                type="checkbox"
                checked={selected.includes(option)}
                onChange={() => toggle(option)}
              />
              {option}
            </label>
          ))}
        </div>
        <button type="button" className="drawer-close" onClick={onClose}>
          닫기
        </button>
      </div>
    </>
  );
}
