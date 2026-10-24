// Records the Status page switching from Response to Waterfall (docs/case-study/img/04-response-to-waterfall.gif).
// Usage: uv run python docs/capture/serve.py /tmp/rewdio-capture 8795 &
//        node docs/case-study/make/toggle.mjs http://127.0.0.1:8795/ /tmp/rewdio-toggle
//        ffmpeg -ss 0.9 -i /tmp/rewdio-toggle/toggle.webm -vf "fps=12,crop=600:900:420:0,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" docs/case-study/img/04-response-to-waterfall.gif
const pw = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = await import("node:fs");
const [url, out] = process.argv.slice(2);
const b = await pw.chromium.launch();
const opts = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, timezoneId: "America/New_York" };
const p = await (await b.newContext(opts)).newPage();
const state = () => p.evaluate(() => fetch("/api/state").then((r) => r.json()));
await p.goto(url); await p.waitForSelector("button.primary");
for (let i = 0; i < 300; i++) {
  let s; for (;;) { s = await state(); if (!s.busy) break; await p.waitForTimeout(150); }
  if (s.done) break;
  await p.waitForTimeout(200);
  await p.waitForFunction(() => !document.querySelector("button.primary")?.disabled);
  await p.locator("button.primary").click(); await p.waitForTimeout(300);
}
const ctx = await b.newContext({ ...opts, recordVideo: { dir: `${out}/v`, size: { width: 1440, height: 900 } } });
const v = await ctx.newPage();
await v.goto(url); await v.waitForSelector("button.primary");
await v.getByRole("button", { name: "Status" }).click(); await v.waitForTimeout(1500);
for (let k = 0; k < 2; k++) {
  await v.getByRole("button", { name: "Waterfall" }).click(); await v.waitForSelector(".waterfall svg"); await v.waitForTimeout(2600);
  await v.getByRole("button", { name: "Response" }).click(); await v.waitForTimeout(1800);
}
await v.getByRole("button", { name: "Waterfall" }).click(); await v.waitForTimeout(2200);
const vid = v.video(); await ctx.close();
fs.renameSync(await vid.path(), `${out}/toggle.webm`);
await b.close(); console.log("ok");
