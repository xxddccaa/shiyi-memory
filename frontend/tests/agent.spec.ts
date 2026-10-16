import { test, expect } from "@playwright/test";

// This spec only writes to the isolated PostgreSQL app created by test_agent_web.py.
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(
  !token,
  "Requires the isolated test server; never writes to the normal workspace.",
);

test("embedded agent shares persistent conversations, sources and confirmed memories", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(
    (t) => sessionStorage.setItem("workmemory-token", t),
    token!,
  );
  await page.goto("/");
  await page.getByRole("button", { name: "问问记忆", exact: true }).click();
  await page.getByLabel("提问", { exact: true }).fill("蓝鲸验收进展如何？");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await expect(page.locator(".answer-bubble")).toContainText("蓝鲸验收已通过", {
    timeout: 20000,
  });
  await expect(page.locator(".citations button").first()).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "问问记忆", exact: true }).click();
  await expect(page.locator(".answer-bubble")).toContainText("蓝鲸验收已通过");
  await page
    .getByLabel("提问", { exact: true })
    .fill("我的工作偏好是先写验收测试。");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.getByRole("button", { name: "聊天菜单" }).click();
  await page.getByRole("button", { name: "记忆管理" }).click();
  await page.getByRole("button", { name: "确认记住", exact: true }).click();
  await expect(
    page.getByText("已确认 · 长期保留", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  // Removing a source invalidates all dependent persisted turns, not just their citation links.
  const list = await request.get("/api/timeline", {
    headers: { Authorization: "Bearer " + token },
  });
  const episode = (await list.json()).items[0];
  const corrected = await request.patch(`/api/episodes/${episode.id}`, {
    headers: { Authorization: "Bearer " + token },
    data: { summary: "蓝鲸验收尚未完成", expected_revision: episode.revision },
  });
  expect(corrected.ok(), await corrected.text()).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "问问记忆", exact: true }).click();
  await expect(page.locator(".answer-bubble").first()).toContainText(
    "来源已更新",
  );
  await expect(page.locator(".answer-bubble").first()).not.toContainText(
    "蓝鲸验收已通过",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "聊天菜单" }).click();
  await page.getByRole("button", { name: "删除会话", exact: true }).click();
  await expect(
    page.getByText("已确认 · 长期保留", { exact: true }),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("daily review edits survive refresh and project briefs show evidence", async ({
  page,
}) => {
  await page.addInitScript(
    (t) => sessionStorage.setItem("workmemory-token", t),
    token!,
  );
  await page.goto("/");
  await page.getByRole("button", { name: "每日回顾", exact: true }).click();
  await expect(page.locator(".artifact-sections")).toBeVisible({
    timeout: 20000,
  });
  await page.getByRole("button", { name: "编辑日报", exact: true }).click();
  await page
    .getByLabel("编辑日报", { exact: true })
    .fill("今天的人工备注：等待验收。");
  await page.getByRole("button", { name: "保存编辑", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "每日回顾", exact: true }).click();
  await expect(
    page.getByText("今天的人工备注：等待验收。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "项目空间", exact: true }).click();
  await page
    .getByRole("button", { name: "恢复上下文", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator(".artifact-sections")).toBeVisible({
    timeout: 20000,
  });
  await expect(
    dialog.getByRole("button", { name: "来源 1", exact: true }).first(),
  ).toBeVisible();
});

test("web and pet share confirmation, cancellation, and deletion updates", async ({
  browser,
  request,
}) => {
  const headers = { Authorization: "Bearer " + token };
  const cid = (
    await (
      await request.post("/api/conversations", {
        headers,
        data: { title: "双端验收" },
      })
    ).json()
  ).id;
  const webContext = await browser.newContext();
  const petContext = await browser.newContext({
    viewport: { width: 420, height: 650 },
  });
  for (const context of [webContext, petContext]) {
    await context.addInitScript(
      ({ token, cid }) => {
        sessionStorage.setItem("workmemory-token", token!);
        sessionStorage.setItem("workmemory-conversation", cid);
      },
      { token, cid },
    );
  }
  const web = await webContext.newPage();
  const pet = await petContext.newPage();
  const errors: string[] = [];
  for (const page of [web, pet])
    page.on("pageerror", (e) => errors.push(e.message));
  try {
    await web.goto("/");
    await web.getByRole("button", { name: "问问记忆", exact: true }).click();
    await pet.goto("/?surface=pet");
    await web.getByLabel("提问", { exact: true }).fill("蓝鲸验收进展如何？");
    await web.getByRole("button", { name: "发送问题", exact: true }).click();
    for (const page of [web, pet])
      await expect(page.locator(".answer-bubble")).toContainText(
        "蓝鲸验收已通过",
        { timeout: 20000 },
      );
    const proposal = await request.post(
      `/api/conversations/${cid}/operations`,
      { headers, data: { action: "todo.create", due_at: new Date(Date.now()+86400000).toISOString(), text: "双端唯一的验收待办" } },
    );
    expect(proposal.status()).toBe(201);
    const operation = await proposal.json();
    for (const page of [web, pet])
      await expect(
        page.getByRole("button", { name: "确认执行", exact: true }),
      ).toBeVisible();
    await pet.getByRole("button", { name: "确认执行", exact: true }).click();
    await expect(web.locator(".operation-card")).toContainText(
      "已执行：创建待办",
    );
    const retry = await request.post(
      `/api/operations/${operation.id}/decision`,
      { headers, data: { action: "confirm", expected_revision: 1 } },
    );
    expect((await retry.json()).status).toBe("executed");
    const todos = await (await request.get("/api/todos", { headers })).json();
    expect(
      todos.filter((t: { text: string }) => t.text === "双端唯一的验收待办"),
    ).toHaveLength(1);
    await pet.getByLabel("提问", { exact: true }).fill("慢速蓝鲸验收查询");
    await pet.getByRole("button", { name: "发送问题", exact: true }).click();
    await web.getByRole("button", { name: "停止", exact: true }).click();
    for (const page of [web, pet])
      await expect(
        page.getByRole("button", { name: "停止", exact: true }),
      ).toHaveCount(0);
    const timeline = await (
      await request.get("/api/timeline", { headers })
    ).json();
    const episode = timeline.items[0];
    await request.patch(`/api/episodes/${episode.id}`, {
      headers,
      data: {
        summary: "验收记录已重新修正",
        expected_revision: episode.revision,
      },
    });
    for (const page of [web, pet]) {
      await expect(page.locator(".answer-bubble").first()).toContainText(
        "来源已更新",
      );
      await expect(page.locator(".answer-bubble").first()).not.toContainText(
        "蓝鲸验收已通过",
      );
    }
    expect(
      await pet.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
  } finally {
    await webContext.close();
    await petContext.close();
  }
});

test("pet keeps the composer visible at native sizes, supports IME and real retry", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const headers = { Authorization: "Bearer " + token };
  const cid = (await (await request.post("/api/conversations", { headers, data: { title: "桌宠样式验收" } })).json()).id;
  await page.addInitScript(({ t, id }) => {
    sessionStorage.setItem("workmemory-token", t!);
    sessionStorage.setItem("workmemory-conversation", id);
  }, { t: token, id: cid });
  await page.goto("/?surface=pet");
  for (const size of [{ width: 340, height: 420 }, { width: 420, height: 650 }, { width: 560, height: 760 }]) {
    await page.setViewportSize(size);
    const input = await page.getByLabel("提问", { exact: true }).boundingBox();
    expect(input!.y + input!.height).toBeLessThan(size.height);
    expect((await page.locator(".chat-composer").boundingBox())!.width).toBeGreaterThan(size.width - 40);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
    await page.screenshot({ path: `../.cache/pet-chat-empty-${size.width}.png` });
  }
  await page.setViewportSize({ width: 420, height: 650 });
  const input = page.getByLabel("提问", { exact: true });
  await input.fill("合成重试验收");
  await input.dispatchEvent("compositionstart");
  await input.press("Enter");
  expect((await (await request.get(`/api/conversations/${cid}/messages`, { headers })).json()).items).toHaveLength(0);
  await input.dispatchEvent("compositionend");
  await input.press("Shift+Enter");
  expect(await input.inputValue()).toContain("\n");
  await input.press("Enter");
  await expect(page.getByText("等待模型回答超时，请重试。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "重新回答", exact: true })).toBeVisible();
  await page.screenshot({ path: "../.cache/pet-chat-retry.png" });
  await page.getByRole("button", { name: "重新回答", exact: true }).click();
  await expect(page.locator(".answer-bubble")).toContainText("蓝鲸验收已通过", { timeout: 20000 });
  await expect(page.locator(".question-bubble")).toHaveCount(1);
  const history = (await (await request.get(`/api/conversations/${cid}/messages`, { headers })).json());
  expect(history.items).toHaveLength(2);
  expect(history.run.error_code).toBeNull();
  await page.locator(".citations button").first().click();
  await expect(page.getByRole("dialog", { name: "工作记录来源" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".citations button").first()).toBeFocused();
  await page.getByRole("button", { name: "聊天菜单" }).click();
  await expect(page.getByLabel("选择会话")).toBeVisible();
  await page.getByRole("button", { name: "记忆管理" }).click();
  await expect(page.getByRole("dialog", { name: "记忆管理" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.screenshot({ path: "../.cache/pet-chat-answer.png" });
  expect(errors).toEqual([]);
});

test("pet preserves history scroll, exposes new replies, and keeps long text contained", async ({ page, request }) => {
  const cid = process.env.WORKMEMORY_PET_HISTORY_ID!;
  const headers = { Authorization: "Bearer " + token };
  await page.addInitScript(({ t, id }) => {
    sessionStorage.setItem("workmemory-token", t!);
    sessionStorage.setItem("workmemory-conversation", id);
  }, { t: token, id: cid });
  await page.setViewportSize({ width: 340, height: 420 });
  await page.goto("/?surface=pet");
  await expect(page.locator(".chat-turn")).toHaveCount(100);
  const transcript = page.getByLabel("聊天记录", { exact: true });
  await expect.poll(() => transcript.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThan(10);
  await transcript.evaluate((e) => { e.scrollTop = 0; });
  const before = await page.locator(".chat-turn").first().boundingBox();
  await page.getByRole("button", { name: "加载更早的消息" }).click();
  await expect(page.locator(".chat-turn")).toHaveCount(130);
  const after = await page.locator(".chat-turn").nth(30).boundingBox();
  expect(Math.abs(before!.y - after!.y)).toBeLessThan(10);
  await transcript.evaluate((e) => { e.scrollTop = 400; });
  await request.post(`/api/conversations/${cid}/messages`, { headers, data: { content: "蓝鲸验收进展如何？", request_id: crypto.randomUUID() } });
  await expect(page.getByRole("button", { name: "有新消息" })).toBeVisible();
  await expect(page.locator(".chat-turn")).toHaveCount(132, { timeout: 20000 });
  expect(await transcript.evaluate((e) => e.scrollTop)).toBeLessThan(500);
  await page.getByRole("button", { name: "有新消息" }).click();
  await expect.poll(() => transcript.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThan(10);
  const longText = "合成长文本" + "x".repeat(700);
  await request.post(`/api/conversations/${cid}/messages`, { headers, data: { content: longText, request_id: crypto.randomUUID() } });
  await expect(page.locator(".chat-turn")).toHaveCount(134, { timeout: 20000 });
  for (const size of [{ width: 340, height: 420 }, { width: 420, height: 650 }, { width: 560, height: 760 }]) {
    await page.setViewportSize(size);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
    expect(await transcript.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(true);
    const composer = await page.getByLabel("提问", { exact: true }).boundingBox();
    expect(composer!.y + composer!.height).toBeLessThan(size.height);
    await page.screenshot({ path: `../.cache/pet-chat-history-${size.width}.png` });
  }
});
