import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface Props {
  title: string;
  options: string[];
  selected: string[];
  anchorEl: HTMLElement | null;
  mode?: "multi" | "single";
  onChange: (next: string[]) => void;
  onClose: () => void;
  footer?: ReactNode;
}

const MENU_GAP = 8;
const VIEWPORT_PAD = 8;
/** Prefer this panel height when the viewport allows it. */
const MENU_PREFERRED_HEIGHT = 720;
const MENU_MIN_HEIGHT = 320;
const MENU_MIN_WIDTH = 280;

type Placement = "right" | "left";

export function ConfigDrawer({
  title,
  options,
  selected,
  anchorEl,
  mode = "multi",
  onChange,
  onClose,
  footer,
}: Props) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{
    left: number;
    top: number;
    width: number;
    maxHeight: number;
    placement: Placement;
  } | null>(null);

  function toggle(option: string) {
    if (mode === "single") {
      onChange([option]);
      onClose();
      return;
    }
    if (selected.includes(option)) {
      onChange(selected.filter((s) => s !== option));
    } else {
      onChange([...selected, option]);
    }
  }

  function updatePosition() {
    if (!anchorEl) return;
    const rect = anchorEl.getBoundingClientRect();
    const preferredWidth = Math.max(rect.width, MENU_MIN_WIDTH);
    const preferred = Math.min(
      MENU_PREFERRED_HEIGHT,
      Math.floor(window.innerHeight - VIEWPORT_PAD * 2),
    );

    const spaceRight = Math.max(0, window.innerWidth - rect.right - MENU_GAP - VIEWPORT_PAD);
    const spaceLeft = Math.max(0, rect.left - MENU_GAP - VIEWPORT_PAD);
    const placement: Placement =
      spaceRight >= Math.min(preferredWidth, 200) || spaceRight >= spaceLeft
        ? "right"
        : "left";
    const sideSpace = placement === "right" ? spaceRight : spaceLeft;
    const width = Math.min(preferredWidth, Math.max(220, sideSpace || preferredWidth));

    const left =
      placement === "right"
        ? Math.min(rect.right + MENU_GAP, window.innerWidth - width - VIEWPORT_PAD)
        : Math.max(VIEWPORT_PAD, rect.left - MENU_GAP - width);

    // Tall side panel: prefer aligning near the anchor, then grow downward
    // and shift up only as needed so it stays inside the viewport.
    const maxHeight = Math.max(MENU_MIN_HEIGHT, preferred);
    let top = Math.min(rect.top, window.innerHeight - VIEWPORT_PAD - maxHeight);
    top = Math.max(VIEWPORT_PAD, top);

    const available = Math.max(MENU_MIN_HEIGHT, window.innerHeight - top - VIEWPORT_PAD);
    const resolvedHeight = Math.min(maxHeight, available);

    setPosition({
      left,
      top,
      width,
      maxHeight: resolvedHeight,
      placement,
    });
  }

  useLayoutEffect(() => {
    updatePosition();
  }, [anchorEl, options.length, footer]);

  useEffect(() => {
    if (!anchorEl) return;

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);

    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (anchorEl?.contains(target)) return;
      onClose();
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }

    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [anchorEl, onClose]);

  if (!position) return null;

  return createPortal(
    <div
      ref={menuRef}
      className={`config-popover is-side is-${position.placement}`}
      role="dialog"
      aria-label={title}
      style={{
        left: position.left,
        top: position.top,
        width: position.width,
        maxHeight: position.maxHeight,
      }}
    >
      <div className="config-popover-header">
        <span className="config-popover-title">{title}</span>
        <button
          type="button"
          className="config-popover-close"
          aria-label="닫기"
          onClick={onClose}
        >
          ×
        </button>
      </div>
      <div className="config-popover-list">
        {options.length === 0 ? (
          <div className="config-popover-empty">선택할 항목이 없습니다.</div>
        ) : (
          options.map((option) => {
            const isSelected = selected.includes(option);
            if (mode === "single") {
              return (
                <button
                  key={option}
                  type="button"
                  className={`config-popover-item config-popover-choice${isSelected ? " is-selected" : ""}`}
                  role="menuitemradio"
                  aria-checked={isSelected}
                  onClick={() => toggle(option)}
                >
                  <span>{option}</span>
                  {isSelected && <span className="config-popover-check" aria-hidden="true">✓</span>}
                </button>
              );
            }
            return (
              <label key={option} className="config-popover-item">
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => toggle(option)}
                />
                <span>{option}</span>
              </label>
            );
          })
        )}
      </div>
      {footer ? <div className="config-popover-footer">{footer}</div> : null}
    </div>,
    document.body,
  );
}
