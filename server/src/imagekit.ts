// Thin ImageKit client (free tier: 3 GB DAM storage / 20 GB bandwidth per month).
// Screenshots are uploaded as *private* files, so they can only be fetched via a
// signed URL, which only this Worker (holding the private key) can produce.
export interface IkEnv { IMAGEKIT_PRIVATE_KEY: string; IMAGEKIT_URL_ENDPOINT: string }

const UPLOAD = "https://upload.imagekit.io/api/v1/files/upload";
const API = "https://api.imagekit.io/v1";
const enc = new TextEncoder();

const basic = (env: IkEnv) => "Basic " + btoa(env.IMAGEKIT_PRIVATE_KEY + ":");
const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");

export interface IkUpload { fileId: string; filePath: string }

export async function uploadJpeg(env: IkEnv, bytes: Uint8Array, fileName: string, folder: string): Promise<IkUpload> {
  const form = new FormData();
  form.set("file", new Blob([bytes], { type: "image/jpeg" }), fileName);
  form.set("fileName", fileName);
  form.set("folder", folder);
  form.set("useUniqueFileName", "true");
  form.set("isPrivateFile", "true");
  const r = await fetch(UPLOAD, { method: "POST", headers: { Authorization: basic(env) }, body: form });
  if (!r.ok) throw new Error(`imagekit upload ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = (await r.json()) as { fileId: string; filePath: string };
  return { fileId: j.fileId, filePath: j.filePath };
}

/** ImageKit signed URL: ik-s = HMAC-SHA1(privateKey, url + expiryUnixSeconds), hex. */
export async function signedUrl(env: IkEnv, filePath: string, ttlSeconds = 60): Promise<string> {
  const url = env.IMAGEKIT_URL_ENDPOINT.replace(/\/+$/, "") + "/" + filePath.replace(/^\/+/, "");
  const expiry = Math.floor(Date.now() / 1000) + ttlSeconds;
  const key = await crypto.subtle.importKey("raw", enc.encode(env.IMAGEKIT_PRIVATE_KEY), { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const sig = hex(await crypto.subtle.sign("HMAC", key, enc.encode(url + expiry)));
  return `${url}?ik-s=${sig}&ik-t=${expiry}`;
}

/** Bulk-delete (<=100 ids per call). Ids ImageKit says are already gone count as deleted. */
export async function deleteFiles(env: IkEnv, fileIds: string[]): Promise<void> {
  for (let i = 0; i < fileIds.length; i += 100) {
    let ids = fileIds.slice(i, i + 100);
    for (let attempt = 0; attempt < 2 && ids.length; attempt++) {
      const r = await fetch(`${API}/files/batch/deleteByFileIds`, {
        method: "POST",
        headers: { Authorization: basic(env), "Content-Type": "application/json" },
        body: JSON.stringify({ fileIds: ids }),
      });
      if (r.ok) break;
      const j = (await r.json().catch(() => ({}))) as { missingFileIds?: string[] };
      if (!j.missingFileIds?.length || attempt === 1) throw new Error(`imagekit delete ${r.status}`);
      ids = ids.filter((x) => !j.missingFileIds!.includes(x));
    }
  }
}
