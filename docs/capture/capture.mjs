// Drive the GUI through a whole simulated calibration by tapping its own buttons, taking stills and a video.
// Usage: node docs/capture/capture.mjs http://127.0.0.1:8799/ <out-dir>   (see "Development" in the README)
const pw = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const [url, out] = process.argv.slice(2);
const browser = await pw.chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, timezoneId: "America/New_York",
  recordVideo: { dir: `${out}/video`, size: { width: 390, height: 844 } },
});
const page = await ctx.newPage();
const t0 = Date.now();
const marks = {};
const mark = (k) => { marks[k] = (Date.now() - t0) / 1000; };
const state = () => page.evaluate(() => fetch("/api/state").then((r) => r.json()));
const shot = (name) => page.screenshot({ path: `${out}/${name}.png` });
const pause = (ms) => page.waitForTimeout(ms);
async function idle() {
  for (;;) { const s = await state(); if (!s.busy) return s; await pause(150); }
}
async function tap() {
  await page.waitForFunction(() => !document.querySelector("button.primary")?.disabled);
  await page.locator("button.primary").click();
  await pause(400);
}

await page.goto(url);
await page.waitForSelector("button.primary");
await pause(800);
await shot("start");
const taken = new Set();
for (let i = 0; i < 300; i++) {
  const s = await idle();
  await pause(300);
  if (s.done) break;
  const key = s.pending_action?.key || "";
  if (key === "crawl:grid_setup" && !taken.has("setup")) { taken.add("setup"); await shot("crawl-setup"); }
  if (key.startsWith("crawl:mic:")) {
    const done = s.crawl.progress.done;
    if (done === 2 && !marks.crawl_start) mark("crawl_start");
    if (done === 8 && !marks.crawl_end) mark("crawl_end");
    if (done === 9 && !taken.has("target")) { taken.add("target"); await shot("crawl-target"); }
    await pause(1100); // let the pulse play in the video
  }
  if (key === "crawl:place_final" && !taken.has("place")) { taken.add("place"); await pause(500); await shot("crawl-place"); }
  await tap();
}
await pause(800);

await page.getByRole("button", { name: "Status" }).click();
await pause(800);
await shot("status");

await page.getByRole("button", { name: "Setups" }).click();
await page.waitForSelector(".list li");
await pause(600);
mark("ab_start");
await page.getByRole("button", { name: "Use Calibrated as A" }).click();
await pause(500);
await page.getByRole("button", { name: "Use Flat as B" }).click();
await pause(700);
for (const k of [0, 1, 0]) { await page.locator(".ab-key").nth(k).click(); await pause(1300); }
await shot("setups");
await page.getByRole("button", { name: "Blind" }).click();
await pause(900);
for (const k of [0, 1]) { await page.locator(".ab-key").nth(k).click(); await pause(1200); }
await shot("setups-blind");
await page.getByRole("button", { name: "Reveal" }).click();
await pause(1600);
mark("ab_end");

await page.getByRole("button", { name: "Settings" }).click();
await pause(800);
await shot("settings");

const video = await page.video().path();
await ctx.close();

const desk = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2, timezoneId: "America/New_York" });
const dp = await desk.newPage();
await dp.goto(url);
await dp.getByRole("button", { name: "Status" }).click();
await dp.waitForTimeout(800);
await dp.screenshot({ path: `${out}/desktop-status.png` });
await browser.close();

const fs = await import("node:fs");
fs.writeFileSync(`${out}/marks.json`, JSON.stringify({ video, ...marks }, null, 1));
console.log(JSON.stringify({ video, ...marks }));
