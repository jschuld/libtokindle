// Shared by the upload and settings pages.
const $ = (id) => document.getElementById(id);

function getToken() { try { return localStorage.getItem("token") || ""; } catch { return ""; } }
function setToken(t) { try { t ? localStorage.setItem("token", t) : localStorage.removeItem("token"); } catch {} }

// Called when the server says the token is wrong; each page sets its own.
let onUnauthorized = () => {};

async function api(path, { json, ...options } = {}) {
  const headers = { Authorization: "Bearer " + getToken() };
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(json);
  }
  const res = await fetch(path, { ...options, headers });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { onUnauthorized(); throw new Error("unauthorised"); }
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : res.statusText);
  return body;
}
