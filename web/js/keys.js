// @ts-check
/**
 * Presenter keys (doc/design.md §4, brief Appendix C). Space (keydown) = space; in hold mode its keyup = space_up;
 * Esc; → / ← and the clicker's PageDown / PageUp = next / prev; P pause; H human; R relay; T focuses the typed box.
 * Auto-repeat is ignored. While the typed box has focus nothing is a presenter key except Esc, which leaves it.
 * Before Arm nothing is handled, so Space/Enter on the focused Arm button keep their native meaning.
 */
import { presenterKey } from "./util.js";

/** @typedef {import("./util.js").KeyAction} KeyAction */

/**
 * @typedef {object} KeyOptions
 * @property {() => string} micMode  "tap" | "hold"
 * @property {() => boolean} armed
 * @property {(action: Exclude<KeyAction, "type">, event: KeyboardEvent|null) => void} onAction
 * @property {() => void} onFocusTyped
 * @property {(action: KeyAction) => void} [onNotArmed]
 */

/** @param {EventTarget|null} target */
function isEditable(target) {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT";
}

/** @param {KeyboardEvent} e */
function isSpace(e) {
  return e.code === "Space" || e.key === " ";
}

/** @param {KeyOptions} opts */
export function installKeys(opts) {
  let spaceHeld = false; // a Space keydown was sent and its keyup is still to come (hold mode)

  /** @param {KeyboardEvent} e */
  const onKeyDown = (e) => {
    if (isEditable(e.target)) {
      if (e.key === "Escape") {
        e.preventDefault();
        /** @type {HTMLElement} */ (e.target).blur();
      }
      return;
    }
    const action = presenterKey(e, opts.micMode());
    if (!action) return;
    if (!opts.armed()) {
      if (!e.repeat) opts.onNotArmed?.(action);
      return;
    }
    e.preventDefault(); // no scrolling, no button activation, no "t" typed into the box we focus
    if (e.repeat) return;
    if (action === "type") {
      opts.onFocusTyped();
      return;
    }
    if (action === "space") spaceHeld = true;
    if (action === "space_up") return; // keyup only
    opts.onAction(action, e);
  };

  /** @param {KeyboardEvent} e */
  const onKeyUp = (e) => {
    if (isEditable(e.target) || !isSpace(e)) return;
    if (opts.armed()) e.preventDefault();
    const held = spaceHeld;
    spaceHeld = false;
    if (held && presenterKey(e, opts.micMode()) === "space_up") opts.onAction("space_up", e);
  };

  // Hold mode: if the window loses focus while Space is down, its keyup never comes; release the floor now.
  const onBlur = () => {
    const held = spaceHeld;
    spaceHeld = false;
    if (held && opts.armed() && opts.micMode() === "hold") opts.onAction("space_up", null);
  };

  window.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("keyup", onKeyUp, true);
  window.addEventListener("blur", onBlur);
  return () => {
    window.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("keyup", onKeyUp, true);
    window.removeEventListener("blur", onBlur);
  };
}
