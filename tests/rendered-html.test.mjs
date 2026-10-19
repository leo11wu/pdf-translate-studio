import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const templateRoot = new URL("../", import.meta.url);
const previewRoot = new URL("../app/_sites-preview/", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the PDF translation interface", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>纸间 · PDF 译制台<\/title>/);
  assert.match(html, /LAYOUT-PRESERVING TRANSLATION/);
  assert.match(html, /源语言自动识别/);
  assert.match(html, /OpenAI 兼容接口/);
  assert.match(html, /开始译制/);
  assert.doesNotMatch(html, /codex-preview|SkeletonPreview|react-loading-skeleton/i);
});

test("removes starter-only preview code and records the local-worker design", async () => {
  const [page, layout, packageJson, readme, backend] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../README.md", import.meta.url), "utf8"),
    readFile(new URL("../backend/app.py", import.meta.url), "utf8"),
  ]);

  assert.match(page, /startTranslation/);
  assert.match(page, /EventSource/);
  assert.match(layout, /纸间 · PDF 译制台/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.match(readme, /PDFMathTranslate-next/);
  assert.match(backend, /do_translate_async_stream/);
  assert.match(backend, /AUTO_DETECT_PROMPT/);

  await assert.rejects(access(new URL("SkeletonPreview.tsx", previewRoot)));
  await assert.rejects(access(new URL("preview.css", previewRoot)));
  await assert.rejects(access(new URL("public/_sites-preview", templateRoot)));
});
