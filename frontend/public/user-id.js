// Resolves a stable identifier for "the current user" so generated reports
// can be listed/downloaded later. There is no real server-side session yet
// (see CA-StaffPortal/src/lib/portal/portal-store.tsx - "sap_user" there is
// a client-side-only stub, not a verified login), so this is deliberately
// lightweight: reuse the portal's stub user when this tool is opened inside
// the staff portal (same origin, reverse-proxied), otherwise fall back to a
// per-browser anonymous id so the tool still works standalone.
function resolveUserId() {
  try {
    const portalUser = JSON.parse(localStorage.getItem("sap_user") || "null");
    if (portalUser && portalUser.email) {
      return portalUser.email;
    }
  } catch (e) {}

  let anonId = localStorage.getItem("itr_anon_user_id");
  if (!anonId) {
    anonId = (crypto.randomUUID ? crypto.randomUUID() : `anon-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    localStorage.setItem("itr_anon_user_id", anonId);
  }
  return anonId;
}
