/**
 * Full-page navigations that are deliberately hard reloads.
 *
 * These are not interchangeable with react-router's `navigate()`:
 *
 * - OAuth hand-off leaves the app entirely for the identity provider, so a
 *   document load is required.
 * - Post-authentication redirects must re-bootstrap the SPA so `App` re-reads
 *   the freshly stored access token. A client-side route change would leave the
 *   already-mounted shell signed out.
 *
 * Routing every such call through one function keeps that reasoning in a single
 * place and gives tests a seam to stub, since jsdom cannot perform real
 * navigations.
 */
export function redirectTo(url: string): void {
  window.location.assign(url)
}