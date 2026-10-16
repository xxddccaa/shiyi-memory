import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
const project = process.env.WORKMEMORY_CODING_PROJECT;
test.skip(!token || !project, "Requires an isolated test server and temporary project");

test("web and pet share native programming, tool output and context compaction", async ({ page, browser, request }) => {
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.addInitScript(t => sessionStorage.setItem("workmemory-token", t!), token);
  await page.goto("/");
  await page.getByRole("button", {name: "问问记忆", exact: true}).click();
  await page.getByRole("button", {name: "工具", exact: true}).click();
  await page.getByRole("tab", {name: "编程设置"}).click();
  await page.getByLabel("添加本机目录").fill(project!);
  await page.locator(".coding-path-entry button").click();
  await page.getByRole("button", {name: "新建编程会话", exact: true}).click();
  await expect(page.getByRole("button", {name: "关闭此会话编程", exact: true})).toBeVisible();
  const cid = await page.evaluate(() => sessionStorage.getItem("workmemory-conversation"));
  expect(cid).toBeTruthy();
  await page.keyboard.press("Escape");
  await page.getByLabel("提问", {exact: true}).fill("合成网页编程：写入 browser.txt 并运行命令验证");
  await page.getByRole("button", {name: "发送问题", exact: true}).click();
  await expect(page.locator(".answer-bubble")).toContainText("文件已写入且命令验证通过", {timeout: 20000});
  await page.getByRole("button", {name: "工具", exact: true}).click();
  await page.getByRole("tab", {name: "执行记录"}).click();
  const bash = page.locator(".coding-call").filter({has: page.locator("summary", {hasText: "bash"})});
  await bash.locator("summary").click();
  await bash.getByRole("button", {name: "读取完整输出"}).click();
  await expect(bash).toContainText("synthetic-browser-success");
  const petContext = await browser.newContext({viewport: {width: 340, height: 420}});
  try {
    await petContext.addInitScript(({token, cid}) => {
      sessionStorage.setItem("workmemory-token", token!);
      sessionStorage.setItem("workmemory-conversation", cid!);
    }, {token, cid});
    const pet = await petContext.newPage();
    pet.on("pageerror", e => errors.push(e.message));
    await pet.goto("/?surface=pet");
    await expect(pet.locator(".answer-bubble")).toContainText("文件已写入且命令验证通过");
    await pet.getByRole("button", {name: "编程已开启", exact: true}).click();
    await expect(pet.getByRole("button", {name: "关闭此会话编程", exact: true})).toBeVisible();
    await pet.getByLabel("添加本机目录").fill("/synthetic/unsaved-path");
    await pet.getByRole("tab", {name: "执行记录"}).click();
    await pet.getByRole("tab", {name: "编程设置"}).click();
    await expect(pet.getByLabel("添加本机目录")).toHaveValue("/synthetic/unsaved-path");
    await pet.keyboard.press("Escape");
    const composer = await pet.getByLabel("提问", {exact: true}).boundingBox();
    expect(composer!.y + composer!.height).toBeLessThanOrEqual(420);
    expect(await pet.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
    await pet.screenshot({path: "../.cache/pi-coding-pet-340.png"});
    await pet.getByRole("button", {name: "工具", exact: true}).click();
    await expect(pet.getByLabel("添加本机目录")).toHaveValue("/synthetic/unsaved-path");
    await pet.getByRole("tab", {name: "上下文", exact: true}).click();
    const compactResponse = pet.waitForResponse(r => r.url().endsWith(`/api/conversations/${cid}/compact`) && r.request().method() === "POST");
    await pet.getByRole("button", {name: "立即压缩", exact: true}).click();
    const compacted = await (await compactResponse).json();
    await expect.poll(async () => {
      const value = await request.get(`/api/agent/runs/${compacted.run_id}`, {headers: {Authorization: `Bearer ${token}`}});
      return (await value.json()).status;
    }, {timeout: 15000}).toBe("completed");
    // A short transcript can be a successful no-op; it must not fabricate chat messages.
    await page.getByRole("tab", {name: "上下文", exact: true}).click();
    await expect(page.getByRole("button", {name: "立即压缩", exact: true})).toBeEnabled();
    await expect(pet.locator(".context-status")).not.toContainText("正在压缩");
    await expect(pet.locator(".question-bubble")).toHaveCount(1);
    await expect(pet.locator(".answer-bubble")).toHaveCount(1);
  } finally { await petContext.close(); }
  expect(errors).toEqual([]);
});
