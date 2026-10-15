import { test, expect } from "@playwright/test";
const token = process.env.WORKMEMORY_TEST_TOKEN;
test.skip(!token, "Requires isolated PostgreSQL and reminder loop");
const headers = { Authorization: "Bearer " + token };

test("explicit schedule form, repeat weekdays, pause, and narrow layout", async ({page, request}) => {
  await page.addInitScript(t => sessionStorage.setItem("workmemory-token", t!), token);
  await page.goto("/");
  await page.getByRole("button", {name:"待办与提醒",exact:true}).click();
  await page.getByRole("button", {name:"设置提醒",exact:true}).click();
  const form = page.getByRole("form", {name:"设置提醒"});
  await form.getByLabel("事项内容",{exact:true}).fill("合成每周提醒");
  // Browser form requires the user to supply a date; no inferred default time.
  await expect(form.getByLabel("提醒时间",{exact:true})).toHaveValue("");
  await form.getByLabel("提醒时间",{exact:true}).fill("2099-01-05T09:00"); // Monday
  await form.getByLabel("重复规则").selectOption("weekly");
  await form.getByLabel("周一",{exact:true}).check();
  await form.getByLabel("周三",{exact:true}).check();
  await form.getByRole("button",{name:"保存提醒"}).click();
  const item = page.locator(".scheduled-todo").filter({hasText:"合成每周提醒"});
  await expect(item).toContainText("周一、周三");
  await item.getByRole("button",{name:"暂停",exact:true}).click();
  await page.getByRole("tab",{name:"已暂停"}).click();
  await expect(item).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  page.once("dialog", dialog => dialog.accept());
  await item.getByRole("button",{name:"取消日程"}).click();
  await expect(item).toHaveCount(0);
  const rows = await (await request.get("/api/todos",{headers})).json();
  expect(rows.some((t: {text:string}) => t.text === "合成每周提醒")).toBe(false);
});

test("due delivery opens shared conversation, snoozes once and completes", async ({page, request}) => {
  const make = async (text:string) => {
    const result = await request.post("/api/todos", {headers, data:{request_id:crypto.randomUUID(), text,
      due_at:new Date(Date.now()+2000).toISOString(), timezone:"Asia/Shanghai", repeat:"once", notify_pet:true}});
    expect(result.ok()).toBe(true); return await result.json();
  };
  const first = await make("合成延后提醒");
  await page.addInitScript(t => sessionStorage.setItem("workmemory-token", t!), token);
  await page.goto("/?surface=pet");
  await page.getByRole("button",{name:"聊天菜单"}).click();
  await page.locator(".pet-menu-item").filter({hasText:"提醒"}).click();
  const item = page.locator(".scheduled-reminder").filter({hasText:"合成延后提醒"});
  await expect(item).toBeVisible({timeout:15000});
  await item.getByRole("button",{name:"打开对话"}).click();
  await expect(page.locator(".answer-bubble").filter({hasText:"合成延后提醒"})).toBeVisible();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("workmemory-conversation"))).toBe(first.conversation_id);
  await page.getByRole("button",{name:"聊天菜单"}).click();
  await page.locator(".pet-menu-item").filter({hasText:"提醒"}).click();
  await item.getByRole("button",{name:"10 分钟后提醒"}).click();
  await expect(item).toHaveCount(0);
  const object = await (await request.get(`/api/workspace/objects/todo/${first.id}`,{headers})).json();
  expect(object.reminders[0].status).toBe("snoozed");
  const second = await make("合成完成提醒");
  const complete = page.locator(".scheduled-reminder").filter({hasText:"合成完成提醒"});
  await expect(complete).toBeVisible({timeout:15000});
  await complete.getByRole("button",{name:"完成本次"}).click();
  await expect(complete).toHaveCount(0);
  const rows = await (await request.get("/api/todos",{headers})).json();
  expect(rows.find((r: {id:string}) => r.id === second.id).done).toBe(true);
});
