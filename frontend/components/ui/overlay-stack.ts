/**
 * Which overlay is on top.
 *
 * Every open overlay — a modal, a popover — used to listen for Escape on the
 * window independently, so a "see more" list opened from inside an editor closed
 * BOTH when Escape was pressed. The original app hit the same thing and dodged
 * it by hand-rolling a second overlay at a higher z-index; a stack is the actual
 * fix, and it means a new overlay never has to think about what is beneath it.
 *
 * Deliberately module-level rather than React context: an overlay's position in
 * the stack is a fact about the whole document, and threading a provider through
 * every screen to say so would be ceremony around a list of three things.
 */

let stack: symbol[] = [];

export function pushOverlay(): symbol {
  const token = Symbol('overlay');
  stack = [...stack, token];
  return token;
}

export function popOverlay(token: symbol): void {
  stack = stack.filter((entry) => entry !== token);
}

export function isTopmost(token: symbol): boolean {
  return stack[stack.length - 1] === token;
}
