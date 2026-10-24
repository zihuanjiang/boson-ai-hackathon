// @ts-check
/**
 * Presenter keys of the local deck page (unused in a Zoom run: the bot is the deck, the page is an observer). Space (keydown) = space; in hold mode its keyup = space_up;
 * Esc; → / ← and the clicker's PageDown / PageUp = next / prev; P pause; H human; R relay; T focuses the typed box.
 * Auto-repeat is ignored. While a text box (the typed question, the chat) has focus nothing is a presenter key except
 * Esc, which leaves it. Before Arm (the join) nothing is handled, so Space/Enter on the focused Join button keep their
 * native meaning. I toggles the meeting info panel: a view key, not a presenter key, so it works before joining too.
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
 * @property {() => void} [onInfo]  the I key
 */

/** @param {KeyboardEvent} e */
function isInfoKey(e) {
  return !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === "i" || e.key === "I");
}

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
    if (!action) {
      if (opts.onInfo && isInfoKey(e)) {
        e.preventDefault();
        if (!e.repeat) opts.onInfo();
      }
      return;
    }
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
