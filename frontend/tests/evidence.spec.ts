import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires the isolated test server");

test("capture permissions, exact excerpts, human-only history and narrow layout", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  await context.addInitScript(value => sessionStorage.setItem("workmemory-token", value), token!);
  await page.goto("/");
  const capture = page.getByRole("region", { name: "采集状态" });
  await expect(capture).toContainText("当前应用的采集权限未生效");
  await capture.getByRole("button", { name: "查看诊断与修复" }).click();
  await expect(capture).toContainText("默认定时模式无需输入监控");
  await capture.getByText("查看采集链路时间").click();
  await expect(capture).toContainText("最近OCR");
  await expect(page.locator("article.episode").first()).toContainText("已核对原文");
  await expect(page.locator("article.episode").first()).not.toContainText("未经原文确认的成果");
  await page.getByRole("button", { name: "查看隔离历史（仅供本人核对）", exact: true }).click();
  await expect(page.locator(".legacy-history")).toContainText("合成旧摘要：未经原文确认的成果");
  await page.locator("article.episode").first().getByRole("button", { name: "查看来源" }).click();
  const modal = page.getByRole("dialog");
  await expect(modal.locator(".source-excerpt")).toContainText("发布时间由周五改为周一");
  await modal.getByRole("button", { name: /核对 .* 的原文/ }).click();
  await expect(modal.locator('[id^="raw-"]').first()).toBeVisible();
  await modal.getByRole("button", { name: "关闭", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
