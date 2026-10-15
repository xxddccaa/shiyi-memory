import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires the isolated test server");

test("confirmed memories, explicit report drafts and maintenance work across pages", async ({
  page,
  context,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await context.addInitScript(
    (value) => sessionStorage.setItem("workmemory-token", value),
    token!,
  );
  await page.goto("/");
  await page
    .locator("article.episode")
    .first()
    .getByRole("button", { name: "查看来源" })
    .click();
  const modal = page.getByRole("dialog");
  await modal
    .getByRole("button", { name: "编辑并长期留存", exact: true })
    .first()
    .click();
  await modal
    .getByRole("textbox", { name: "长期记忆内容" })
    .fill("我确认已完成合成验收任务");
  await modal
    .locator('input[type="date"]')
    .fill(new Date().toLocaleDateString("sv-SE"));
  await modal.getByRole("button", { name: "预览确认内容" }).click();
  await expect(
    modal.getByRole("button", { name: "确认并长期保存" }),
  ).toBeVisible();
  await modal.getByRole("button", { name: "确认并长期保存" }).click();
  await expect(modal).toContainText("已确认并长期保存");
  await modal.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "每日回顾", exact: true }).click();
  await expect(page.locator(".report-material")).toContainText(
    "我确认已完成合成验收任务",
  );
  const panel = page.locator(".artifact-panel");
  await expect(panel).not.toContainText("已生成，可核对编辑");
  await panel.getByRole("button", { name: "一键生成确认材料草稿" }).click();
  await expect(panel).toContainText("已生成，可核对编辑", { timeout: 15000 });
  await panel.getByRole("button", { name: "编辑日报", exact: true }).click();
  await panel
    .getByRole("textbox", { name: "编辑日报" })
    .fill("保留这段合成人工编辑");
  await panel.getByRole("button", { name: "保存编辑" }).click();
  await expect(panel.locator(".preserve-lines")).toHaveText(
    "保留这段合成人工编辑",
  );
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  const memories = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "确认记忆与到期核对" }) })
    .last();
  await expect(memories).toContainText("我确认已完成合成验收任务");
  await expect(page.getByRole("heading", { name: "备份与维护" })).toBeVisible();
  await page.getByRole("button", { name: "立即备份", exact: true }).click();
  await page.getByText("最近维护任务", { exact: true }).click();
  await expect(
    page
      .locator(".settings-card")
      .filter({ has: page.getByRole("heading", { name: "备份与维护" }) }),
  ).toContainText("等待");
  const headers = { Authorization: `Bearer ${token}` };
  const found = await request.get("/api/memory/candidates", { headers });
  const memory = (await found.json()).items.find(
    (item: { text: string }) => item.text === "我确认已完成合成验收任务",
  );
  expect(memory.conversation_id).toBeNull();
  const origin = memory.origin.episode_id;
  const source = await request.get(`/api/episodes/${origin}/delete-preview`, {
    headers,
  });
  const preview = await source.json();
  expect(preview.memories.length).toBe(1);
  const deleted = await request.delete(
    `/api/episodes/${origin}?expected_revision=${preview.revision}&memory_hash=${preview.memory_hash}`,
    { headers },
  );
  expect(deleted.ok()).toBe(true);
  await page.getByRole("button", { name: "每日回顾", exact: true }).click();
  await expect(panel).toContainText("旧内容已隐藏");
  await expect(panel.locator(".preserve-lines")).toHaveCount(0);
  await panel.getByRole("button", { name: "核对保留的人工编辑" }).click();
  await expect(panel.getByRole("textbox", { name: "编辑日报" })).toHaveValue(
    "保留这段合成人工编辑",
  );
  await panel.getByRole("button", { name: "保存编辑" }).click();
  await expect(panel.locator(".preserve-lines")).toHaveText(
    "保留这段合成人工编辑",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
