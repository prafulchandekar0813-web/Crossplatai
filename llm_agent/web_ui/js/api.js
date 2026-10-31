export class ApiError extends Error {
  constructor(message, status = 0, data = null) { super(message); this.status = status; this.data = data; }
}

let unauthorizedHandler = null;
export const onUnauthorized = (fn) => { unauthorizedHandler = fn; };

async function readError(res) {
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  return new ApiError(data?.error || `The server answered ${res.status}`, res.status, data);
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const init = { method, credentials: "same-origin", headers: {}, signal };
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, init); }
  catch (e) { if (e.name === "AbortError") throw e; throw new ApiError("Cannot reach the server. Is it still running?"); }
  if (!res.ok) {
    const err = await readError(res);
    if (res.status === 401 && unauthorizedHandler && !path.startsWith("/api/auth/")) unauthorizedHandler();
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

export async function upload(path, file) {
  let res;
  try { res = await fetch(path, { method: "PUT", body: file, credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" } }); }
  catch { throw new ApiError("Cannot reach the server. Is it still running?"); }
  if (!res.ok) throw await readError(res);
  return res.json();
}

/** Server-sent events over fetch (EventSource cannot POST). Yields parsed event objects; ignores keep-alives. */
export async function* sse(path, { method = "GET", body, signal } = {}) {
  const init = { method, credentials: "same-origin", headers: { Accept: "text/event-stream" }, signal };
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, init); }
  catch (e) { if (e.name === "AbortError") throw e; throw new ApiError("Cannot reach the server. Is it still running?"); }
  if (!res.ok) throw await readError(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data: ")) { try { yield JSON.parse(line.slice(6)); } catch { /* skip malformed */ } }
      }
    }
  }
}
