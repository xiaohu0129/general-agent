import "./ClarifyOptions.css";

export interface ClarifyOptionsProps {
  options: { label: string; value: string }[];
  selected?: string;
  disabled?: boolean;
  streaming?: boolean;
  onSelect: (value: string, label: string) => void;
}

export default function ClarifyOptions({
  options,
  selected,
  disabled,
  streaming,
  onSelect,
}: ClarifyOptionsProps) {
  const allDisabled = disabled === true || streaming === true || selected != null;

  return (
    <div className="clarify-options">
      {options.map((o) => {
        const isSelected = selected != null && o.value === selected;
        return (
          <button
            key={o.value}
            type="button"
            className={isSelected ? "clarify-option selected" : "clarify-option"}
            disabled={allDisabled}
            aria-pressed={isSelected}
            onClick={() => onSelect(o.value, o.label)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
