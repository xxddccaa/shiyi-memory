import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires the isolated test server");

test("PNG preview, shared selection, motion, reload and corrupt upload", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.addInitScript((value) => sessionStorage.setItem("workmemory-token", value), token!);
  await page.goto("/");
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  await page.getByRole("navigation", { name: "设置分类" }).getByRole("button", { name: "桌宠与陪伴" }).click();
  const png = await page.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 100; canvas.height = 140;
    const ctx = canvas.getContext("2d")!; ctx.fillStyle = "#438c98"; ctx.fillRect(35, 20, 30, 100);
    return canvas.toDataURL("image/png").split(",")[1];
  });
  await page.getByLabel("上传 PNG 形象").setInputFiles({ name: "合成蓝色角色.png", mimeType: "image/png", buffer: Buffer.from(png, "base64") });
  await expect(page.getByAltText("待导入形象预览")).toBeVisible();
  await page.getByRole("button", { name: "应用此形象", exact: true }).click();
  const choice = page.getByRole("button", { name: /合成蓝色角色/ });
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  await expect(choice.locator("img")).toBeVisible();
  expect(await choice.locator("img").evaluate((image) => {
    const box = image.getBoundingClientRect(), parent = image.parentElement!.getBoundingClientRect();
    return box.height <= parent.height && box.width <= parent.width;
  })).toBe(true);
  await page.getByLabel("单图形象轻微动效").uncheck();
  await expect(page.getByLabel("单图形象轻微动效")).not.toBeChecked();
  const other = await context.newPage();
  await other.goto("/");
  await other.getByRole("button", { name: "偏好与状态", exact: true }).click();
  await other.getByRole("navigation", { name: "设置分类" }).getByRole("button", { name: "桌宠与陪伴" }).click();
  await expect(other.getByRole("button", { name: /合成蓝色角色/ })).toHaveAttribute("aria-pressed", "true");
  await expect(other.getByLabel("单图形象轻微动效")).not.toBeChecked();
  await other.getByRole("button", { name: /默认猫咪/ }).click();
  await expect(page.getByRole("button", { name: /默认猫咪/ })).toHaveAttribute("aria-pressed", "true");
  // Preview failures preserve the active skin and do not issue a partial upload.
  await page.getByLabel("上传 PNG 形象").setInputFiles({ name: "损坏.png", mimeType: "image/png", buffer: Buffer.from("not an image") });
  await expect(page.getByRole("alert").filter({ hasText: "无法读取这张 PNG" })).toBeVisible();
  await expect(page.getByRole("button", { name: /默认猫咪/ })).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  await page.getByRole("navigation", { name: "设置分类" }).getByRole("button", { name: "桌宠与陪伴" }).click();
  await expect(choice).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../.cache/pet-settings.png", fullPage: true });
  await other.close();
  expect(errors).toEqual([]);
});
