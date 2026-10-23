// Drive the GUI through a whole simulated calibration by tapping its own buttons, taking stills, and recording
// each GIF segment in its own browser session (so a video is exactly its segment; no timestamp cutting).
// Usage: node docs/capture/capture.mjs http://127.0.0.1:8799/ <out-dir>   (see "Development" in the README)
// VIEWPORT=1440x900 PREFIX=mac- records a desktop-window run instead (the default is a 390x844 phone).
const pw = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = await import("node:fs");
const [url, out] = process.argv.slice(2);
const [vw, vh] = (process.env.VIEWPORT || "390x844").split("x").map(Number);
const prefix = process.env.PREFIX || "";
const browser = await pw.chromium.launch();
const opts = { viewport: { width: vw, height: vh }, deviceScaleFactor: 2, timezoneId: "America/New_York" };

async function open(video) {
  const ctx = await browser.newContext(video ? { ...opts, recordVideo: { dir: `${out}/${prefix}video`, size: { width: vw, height: vh } } } : opts);
  const page = await ctx.newPage();
  await page.goto(url);
  await page.waitForSelector("button.primary, .setups, .setup");
  await page.waitForTimeout(600);
  return { ctx, page };
}
const state = (p) => p.evaluate(() => fetch("/api/state").then((r) => r.json()));
const shot = (p, name) => p.screenshot({ path: `${out}/${prefix}${name}.png` });
async function idle(p) {
  for (;;) { const s = await state(p); if (!s.busy) return s; await p.waitForTimeout(150); }
}
async function tap(p) {
  await p.waitForFunction(() => !document.querySelector("button.primary")?.disabled);
  await p.locator("button.primary").click();
  await p.waitForTimeout(400);
}
async function recorded(name, fn) {  // one GIF = one browser session's video
  const { ctx, page } = await open(true);
  await fn(page);
  await page.waitForTimeout(400);
  const video = page.video();
  await ctx.close();
  fs.renameSync(await video.path(), `${out}/${prefix}${name}.webm`);
}

const { page } = await open(false);
const taken = new Set();
for (let i = 0; i < 300; i++) {
  const s = await idle(page);
  await page.waitForTimeout(300);
  if (s.done) break;
  const key = s.pending_action?.key || "";
  if (key === "crawl:grid_setup" && !taken.has("setup")) { taken.add("setup"); await shot(page, "crawl-setup"); }
  if (key.startsWith("crawl:mic:") && s.crawl.progress.done === 2 && !taken.has("crawl")) {
    taken.add("crawl");
    await recorded("crawl", async (p) => {
      for (;;) {
        const c = await idle(p);
        await p.waitForTimeout(1100);  // let the pulse play
        if (c.crawl.progress.done >= 8) break;
        await tap(p);
      }
    });
    continue;
  }
  if (key.startsWith("crawl:mic:") && s.crawl.progress.done === 9 && !taken.has("target")) { taken.add("target"); await shot(page, "crawl-target"); }
  if (key === "crawl:place_final" && !taken.has("place")) { taken.add("place"); await page.waitForTimeout(500); await shot(page, "crawl-place"); }
  await tap(page);
}
await page.waitForTimeout(800);

await page.getByRole("button", { name: "Status" }).click();
await page.waitForTimeout(800);
await shot(page, "status");
await page.getByRole("button", { name: "Waterfall" }).click();
await page.waitForSelector(".waterfall svg");
await page.waitForTimeout(600);
await shot(page, "waterfall");
await page.locator(".waterfall").screenshot({ path: `${out}/${prefix}waterfall-plot.png` });

await recorded("ab", async (p) => {
  await p.getByRole("button", { name: "Setups" }).click();
  await p.waitForSelector(".list li");
  await p.waitForTimeout(600);
  await p.getByRole("button", { name: "Use Calibrated as A" }).click();
  await p.waitForTimeout(500);
  await p.getByRole("button", { name: "Use Flat as B" }).click();
  await p.waitForTimeout(700);
  for (const k of [0, 1, 0]) { await p.locator(".ab-key").nth(k).click(); await p.waitForTimeout(1300); }
  await shot(p, "setups");
  await p.getByRole("button", { name: "Blind" }).click();
  await p.waitForTimeout(900);
  for (const k of [0, 1]) { await p.locator(".ab-key").nth(k).click(); await p.waitForTimeout(1200); }
  await shot(p, "setups-blind");
  await p.getByRole("button", { name: "Reveal" }).click();
  await p.waitForTimeout(1600);
});

await page.getByRole("button", { name: "Settings" }).click();
await page.waitForTimeout(800);
await shot(page, "settings");
await browser.close();
console.log(fs.readdirSync(out).filter((f) => f.startsWith(prefix)).join(" "));
