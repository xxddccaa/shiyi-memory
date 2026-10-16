import { test, expect, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires the isolated PostgreSQL test server");
const artifacts = resolve(import.meta.dirname, "../../.cache/ui-redesign");
async function category(page: Page, name: string, value: string) {
  const mobile = page.getByRole("combobox", { name: "设置分类", exact: true });
  if (await mobile.isVisible()) await mobile.selectOption(value);
  else
    await page
      .getByRole("navigation", { name: "设置分类" })
      .getByRole("button", { name, exact: true })
      .click();
}

test("settings categories preserve drafts and serialize shared preferences", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const mutations: string[] = [];
  page.on("request", (r) => {
    if (["POST", "PUT", "PATCH", "DELETE"].includes(r.method()))
      mutations.push(r.url());
  });
  await page.addInitScript(
    (value) => sessionStorage.setItem("workmemory-token", value),
    token!,
  );
  await page.goto("/");
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  const model = page.getByLabel("模型名称", { exact: true });
  await model.fill("synthetic-unsaved-model");
  await category(page, "联网与工具", "tools");
  await page.getByLabel("常用城市").fill("合成城市草稿");
  await category(page, "模型设置", "models");
  await expect(model).toHaveValue("synthetic-unsaved-model");
  await expect(page.getByLabel("常用城市")).toBeHidden();
  await category(page, "联网与工具", "tools");
  await expect(page.getByLabel("常用城市")).toHaveValue("合成城市草稿");
  expect(mutations).toEqual([]);
  const headers = { Authorization: `Bearer ${token}` };
  const initial = await (
    await request.get("/api/client/state", { headers })
  ).json();
  await category(page, "桌宠与陪伴", "pet");
  const patch = () =>
    page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/client/state") &&
        r.request().method() === "PATCH",
    );
  let saved = patch();
  await page.getByLabel("全屏应用时隐藏").setChecked(!initial.hide_fullscreen);
  expect((await saved).ok()).toBe(true);
  await category(page, "采集与记忆", "memory");
  saved = patch();
  await page
    .getByLabel("记录 Safari / Chrome 网页地址")
    .setChecked(!initial.resource_capture);
  expect((await saved).ok()).toBe(true);
  await category(page, "桌宠与陪伴", "pet");
  await expect(page.getByLabel("全屏应用时隐藏")).toBeChecked({
    checked: !initial.hide_fullscreen,
  });
  saved = patch();
  await page.getByLabel("全屏应用时隐藏").setChecked(initial.hide_fullscreen);
  expect((await saved).ok()).toBe(true);
  await category(page, "采集与记忆", "memory");
  saved = patch();
  await page
    .getByLabel("记录 Safari / Chrome 网页地址")
    .setChecked(initial.resource_capture);
  expect((await saved).ok()).toBe(true);
  for (const width of [1440, 1280, 390]) {
    await page.setViewportSize({ width, height: 960 });
    for (const [value, name] of Object.entries({
      models: "模型设置",
      pet: "桌宠与陪伴",
      memory: "采集与记忆",
      tools: "联网与工具",
      maintenance: "数据维护",
      status: "服务状态",
    })) {
      await category(page, name, value);
      await expect(page.locator(".settings-content > div:visible")).toHaveCount(
        1,
      );
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        animations: "disabled",
        path: resolve(artifacts, `settings-${value}-${width}.png`),
        fullPage: true,
      });
    }
    await category(page, "模型设置", "models");
    await expect(model).toHaveValue("synthetic-unsaved-model");
  }
  expect(errors).toEqual([]);
});

test("chat tools preserve working space, draft and keyboard focus at all supported sizes", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const headers = { Authorization: `Bearer ${token}` };
  const cid = (
    await (
      await request.post("/api/conversations", {
        headers,
        data: { title: "合成界面验收" },
      })
    ).json()
  ).id;
  await page.addInitScript(
    ({ token, cid }) => {
      sessionStorage.setItem("workmemory-token", token!);
      sessionStorage.setItem("workmemory-conversation", cid);
    },
    { token, cid },
  );
  const measurements: Record<string, unknown>[] = [];
  for (const surface of ["pet", "web"]) {
    await page.goto(surface === "pet" ? "/?surface=pet" : "/");
    if (surface === "web")
      await page.getByRole("button", { name: "问问记忆", exact: true }).click();
    const sizes =
      surface === "pet"
        ? [
            [340, 420],
            [420, 650],
            [560, 760],
          ]
        : [
            [1440, 1000],
            [1280, 900],
            [390, 844],
          ];
    for (const [width, height] of sizes) {
      await page.setViewportSize({ width, height });
      const input = page.getByLabel("提问", { exact: true });
      await input.fill("");
      const cancel = page.getByRole("button", { name: "取消选中对象" });
      if (await cancel.isVisible()) await cancel.click();
      const transcript = page.getByLabel("聊天记录", { exact: true });
      const normal = (await transcript.boundingBox())!.height;
      expect(normal).toBeGreaterThanOrEqual(200);
      await page.getByRole("button", { name: "选择对象", exact: true }).click();
      await page.getByLabel("对象类型").selectOption("episode");
      await page.locator(".workspace-object").first().click();
      await expect(page.locator(".chat-selection")).toBeVisible();
      await input.fill("第一行草稿\n第二行草稿\n第三行草稿\n第四行草稿");
      const compact = (await transcript.boundingBox())!.height;
      expect(compact).toBeGreaterThanOrEqual(140);
      const composer = await page.locator(".chat-composer").boundingBox();
      expect(composer!.y + composer!.height).toBeLessThanOrEqual(height);
      await page.screenshot({
        animations: "disabled",
        path: resolve(artifacts, `${surface}-draft-${width}.png`),
      });
      await page.getByRole("button", { name: "工具", exact: true }).click();
      await expect(page.getByRole("dialog")).toHaveCount(1);
      await page.getByRole("tab", { name: "编程设置" }).click();
      await page
        .getByLabel("添加本机目录")
        .fill("/synthetic/unsaved-workspace");
      await page.getByRole("tab", { name: "编程设置" }).focus();
      await page.keyboard.press("ArrowRight");
      await expect(page.getByRole("tab", { name: "执行记录" })).toBeFocused();
      await expect(page.getByRole("tab", { name: "执行记录" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.getByRole("tab", { name: "编程设置" }).click();
      await page.screenshot({
        animations: "disabled",
        path: resolve(artifacts, `${surface}-tools-${width}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", { name: "工具", exact: true }),
      ).toBeFocused();
      await expect(input).toHaveValue(
        "第一行草稿\n第二行草稿\n第三行草稿\n第四行草稿",
      );
      await page.getByRole("button", { name: "工具", exact: true }).click();
      await expect(page.getByLabel("添加本机目录")).toHaveValue(
        "/synthetic/unsaved-workspace",
      );
      await page.keyboard.press("Escape");
      expect((await transcript.boundingBox())!.height).toBe(compact);
      expect(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth <= innerWidth &&
            document.documentElement.scrollHeight <= innerHeight,
        ),
      ).toBe(true);
      measurements.push({
        surface,
        width,
        height,
        normal,
        withDraftAndObject: compact,
      });
    }
  }
  mkdirSync(artifacts, { recursive: true });
  writeFileSync(
    resolve(artifacts, "geometry.json"),
    JSON.stringify(measurements, null, 2),
  );
  expect(errors).toEqual([]);
});
