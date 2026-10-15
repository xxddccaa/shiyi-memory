import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
const surface = process.env.WORKMEMORY_PROJECT_SURFACE;
test.skip(!token || !surface, "Requires isolated synthetic projects.");

test("favicon loads and 32 projects use one confirmation", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${token}` };
  const created = await request.post("/api/conversations", { headers, data: { title: "合成项目清理验收" } });
  const cid = (await created.json()).id;
  await page.addInitScript(({ token, cid }) => {
    sessionStorage.setItem("workmemory-token", token!);
    sessionStorage.setItem("workmemory-conversation", cid);
  }, { token, cid });
  if (surface === "pet") await page.setViewportSize({ width: 420, height: 650 });
  await page.goto(surface === "pet" ? "/?surface=pet" : "/");
  const favicon = await page.locator('link[rel="icon"]').getAttribute("href");
  expect(favicon).toBeTruthy();
  expect((await request.get(favicon!)).headers()["content-type"]).toContain("image/svg+xml");
  expect(await page.evaluate(async (src) => {
    const image = new Image(); image.src = src!;
    await image.decode(); return image.naturalWidth > 0;
  }, favicon)).toBe(true);
  if (surface !== "pet") await expect(page.locator(".brand-icon img")).toBeVisible();
  if (surface === "projects") {
    await page.getByRole("button", { name: "项目空间", exact: true }).click();
    await page.getByRole("button", { name: "预览删除全部项目", exact: true }).click();
  } else {
    if (surface === "web") await page.getByRole("button", { name: "问问记忆", exact: true }).click();
    await page.getByLabel("提问", { exact: true }).fill("删除我的所有项目");
    await page.getByRole("button", { name: "发送问题", exact: true }).click();
  }
  const card = page.locator(".operation-card").filter({ has: page.getByRole("heading", { name: "批量删除 32 个项目", exact: true }) });
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(card.locator(".operation-target")).toHaveCount(32);
  await expect(card).toContainText("合成项目031");
  await expect(card).toContainText("工作记录、待办提醒和确认记忆保留");
  await expect(page.getByRole("button", { name: "确认删除 32 个项目", exact: true })).toHaveCount(1);
  expect((await (await request.get("/api/projects", { headers })).json()).length).toBe(32);
  if (surface === "web") await page.screenshot({ path: "../.cache/projects-confirmation.png", fullPage: true });
  await card.getByRole("button", { name: "确认删除 32 个项目", exact: true }).click();
  await expect(page.locator(".operation-list")).toContainText("已执行：批量删除 32 个项目");
  expect(await (await request.get("/api/projects", { headers })).json()).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
