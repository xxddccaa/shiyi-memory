import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
const root = resolve(import.meta.dirname, "../..");
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires an isolated PostgreSQL server; never changes the normal workspace.");

test("manual record, source correction, pages, deletion and mobile layout", async ({
  page,
  request,
}) => {
  const project = "界面验收-" + Date.now();
  let episodeId: string | undefined;
  await page.addInitScript(
    (token) => sessionStorage.setItem("workmemory-token", token),
    token!,
  );
  const browserErrors: string[] = [];
  page.on("pageerror", (e) => browserErrors.push(e.message));
  try {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "工作脉络，一目了然。" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "添加记录" }).click();
    await page.getByLabel("项目名称").fill(project);
    await page
      .getByLabel("今天的进展")
      .fill("这是合成的界面验收记录：蓝鲸项目在周二完成验收。");
    await page.getByRole("button", { name: "保存记录" }).click();
    await expect(page.getByRole("dialog")).not.toBeVisible();
    const card = page.locator("article.episode").filter({ hasText: project });
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "查看来源" }).click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByText("这是合成的界面验收记录：蓝鲸项目在周二完成验收。"),
    ).toBeVisible();
    await dialog.getByRole("button", { name: "纠正记忆" }).click();
    await page
      .getByLabel("纠正后的摘要")
      .fill("这是纠正后的合成验收记录：蓝鲸项目计划在周三验收。");
    await dialog.getByRole("button", { name: "保存纠正" }).click();
    await expect(card).toContainText("周三");
    const timeline = await request.get("/api/timeline", {
      headers: { Authorization: "Bearer " + token },
    });
    const row = (await timeline.json()).items.find(
      (e: { project: string }) => e.project === project,
    );
    episodeId = row?.id;
    mkdirSync(resolve(root, ".cache/ui"), { recursive: true });
    await page.screenshot({
      path: resolve(root, ".cache/ui/desktop.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "项目空间", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "让项目接着往前走。" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "每日回顾", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "回看今天，轻装向前。" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "问问记忆", exact: true }).click();
    await expect(page.getByLabel("提问")).toBeVisible();
    await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
    await expect(page.getByRole("heading", { name: "记忆后端" })).toBeVisible();
    await expect(page.getByText("自动采集", { exact: true })).toBeVisible();
    await expect(page.getByText("PaddleOCR", { exact: true })).toBeVisible();
    await expect(page.getByText("最近一次 OCR", { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "工作时间线", exact: true }).click();
    await page.screenshot({
      path: resolve(root, ".cache/ui/mobile.png"),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await card.getByRole("button", { name: "查看来源" }).click();
    await page.getByRole("button", { name: "删除片段" }).click();
    await page.getByRole("button", { name: "确认删除" }).click();
    await expect(card).toHaveCount(0);
    expect(browserErrors).toEqual([]);
  } finally {
    if (episodeId)
      await request.delete("/api/episodes/" + episodeId, {
        headers: { Authorization: "Bearer " + token },
      });
  }
});
