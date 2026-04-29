import { useEffect, useRef } from "react";

type Props = {
  value: string;
  onChange: (v: string) => void;
};

export function SearchBar({ value, onChange }: Props) {
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // "/" focuses the search box (unless we're already typing in an input).
      const target = e.target as HTMLElement | null;
      const isInput =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (e.key === "/" && !isInput) {
        e.preventDefault();
        ref.current?.focus();
      } else if (e.key === "Escape" && document.activeElement === ref.current) {
        onChange("");
        ref.current?.blur();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onChange]);

  return (
    <div className="search">
      <span className="search-icon">⌕</span>
      <input
        ref={ref}
        type="search"
        placeholder="Takım ara…"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Takım ara"
      />
      {value ? (
        <button
          className="search-clear"
          onClick={() => onChange("")}
          aria-label="Aramayı temizle"
        >
          ✕
        </button>
      ) : (
        <kbd className="search-shortcut">/</kbd>
      )}
    </div>
  );
}
