import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

// Mock every API route: these UI checks never change the user's model or memories.
test("model settings test, save, reload, credentials, errors and mobile layout", async ({
  page,
}) => {
  const local = {
    provider: "ollama",
    base_url: "http://127.0.0.1:11434",
    model: "qwen3:8b",
    api_key_configured: false,
    timeout_seconds: 240,
    max_tokens: 4096,
    context_window: 8192,
    thinking: false,
    temperature: 0.1,
    structured_output: "json_schema",
  };
  let saved = {
    revision: 0,
    generation: { ...local },
    local_default: local,
    local_models: ["qwen3:8b", "qwen3-embedding:0.6b"],
    embedding: {
      provider: "ollama",
      base_url: local.base_url,
      model: "qwen3-embedding:0.6b",
      dimensions: 1024,
    },
    ocr: {
      provider: "paddleocr",
      detection_model: "PP-OCRv6_tiny_det",
      recognition_model: "PP-OCRv6_tiny_rec",
    },
  };
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  let reject = false;
  const browserErrors: string[] = [];
  page.on("pageerror", (e) => browserErrors.push(e.message));
  await page.addInitScript(() =>
    sessionStorage.setItem("workmemory-token", "synthetic-ui-token"),
  );
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/config/models" && route.request().method() === "GET") {
      await route.fulfill({ json: saved });
      return;
    }
    if (path.startsWith("/api/config/models")) {
      const body = route.request().postDataJSON();
      calls.push({ path, body });
      if (reject) {
        await route.fulfill({
          status: 422,
          json: { detail: "模型连接超时，请检查地址和网络。" },
        });
        return;
      }
      const result = {
        ok: true,
        seconds: 0.7,
        structured_output_valid: true,
        answer: "连接成功",
        product: 391,
      };
      if (path.endsWith("/test")) {
        await route.fulfill({ json: result });
        return;
      }
      const {
        revision: _revision,
        api_key,
        clear_api_key,
        ...generation
      } = body;
      saved = {
        ...saved,
        revision: saved.revision + 1,
        generation: {
          ...generation,
          api_key_configured:
            !clear_api_key &&
            Boolean(api_key || saved.generation.api_key_configured),
        },
      };
      await route.fulfill({ json: { ...saved, test: result } });
      return;
    }
    if (path === "/api/memory/candidates" || path === "/api/memory/expiring") {
      await route.fulfill({ json: { items: [], next_cursor: null } });
      return;
    }
    if (path === "/api/maintenance") {
      await route.fulfill({ json: { database_bytes: 0, backup_bytes: 0, schedule: "合成备份状态", policy: {enabled: true, revision: 0}, backends: {}, backups: [], tasks: [] } });
      return;
    }
    if (path === "/api/memory/backfill") {
      await route.fulfill({json: {jobs: {}}});
      return;
    }
    if (path === "/api/pet/skins") {
      await route.fulfill({json: {items: [{id: "builtin-cat", name: "默认猫咪", presentation: "builtin", preview: null}]}});
      return;
    }
    if (path === "/api/client/state") {
      await route.fulfill({json: {revision: 0, pet_visible: true, pet_size: 96, pet_skin: "builtin-cat", pet_motion_enabled: true, reminders_enabled: true, hide_fullscreen: true, resource_policy: "standard", resource_capture: false, backfill_enabled: false}});
      return;
    }
    if (path === "/api/mcp/servers") {
      await route.fulfill({json: {revision: 0, servers: [], city: null}});
      return;
    }
    if (path === "/api/status") {
      await route.fulfill({
        json: {
          backend: "reme",
          jobs: {},
          sync: { total: 0, synced: 0 },
          ollama: { reachable: true, models_ready: true },
          inference: { queued: 0, current_model: null, recent: [] },
          retention_days: 30,
          errors: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: path === "/api/timeline" ? { items: [], total: 0 } : [],
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  const form = page.getByRole("region", { name: "模型配置" });
  await expect(form.getByLabel("模型名称")).toHaveValue("qwen3:8b");
  await form.getByLabel("生成服务").selectOption("openai_compatible");
  await form.getByLabel("模型名称").fill("Qwen-synthetic");
  await form.getByLabel("接口基础地址").fill("http://synthetic.test:8000/v1");
  await form.getByLabel("API Key（可选）").fill("synthetic-secret");
  await form.getByText("高级参数", { exact: true }).click();
  await form
    .getByRole("combobox", { name: "思考模式", exact: true })
    .selectOption("off");
  await form.getByLabel("最大输出 Token", { exact: true }).fill("65536");
  await form.getByLabel("模型上下文 Token（可选）", { exact: true }).fill("1000000");
  await form.getByRole("button", { name: "仅测试连接" }).click();
  await expect(form.getByRole("status")).toContainText("当前配置尚未改变");
  expect(saved.revision).toBe(0);
  expect(calls[0].body.thinking).toBe(false);
  expect(calls[0].body.max_tokens).toBe(65536);
  expect(calls[0].body.context_window).toBe(1000000);
  await expect(form.locator(".model-current")).toContainText("qwen3:8b");
  await form.getByRole("button", { name: "验证并保存" }).click();
  await expect(form.getByRole("status")).toContainText(
    "已保存并应用：Qwen-synthetic",
  );
  await expect(form.getByLabel("API Key（可选）")).toHaveValue("");
  await expect(form.getByLabel("API Key（可选）")).toHaveAttribute(
    "placeholder",
    "已保存，留空保留",
  );
  const stored = await page.evaluate(() =>
    JSON.stringify([sessionStorage, localStorage]),
  );
  expect(stored).not.toContain("synthetic-secret");
  await form.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(form.getByLabel("模型名称")).toHaveValue("Qwen-synthetic");
  await expect(form.getByLabel("最大输出 Token", { exact: true })).toHaveValue("65536");
  await expect(form.getByLabel("模型上下文 Token（可选）", { exact: true })).toHaveValue("1000000");
  await form.getByLabel("清除已保存的 API Key").check();
  await form.getByRole("button", { name: "验证并保存" }).click();
  await expect(form.getByRole("status")).toContainText("已保存并应用");
  expect(calls.at(-1)?.body.clear_api_key).toBe(true);
  await expect(form.getByLabel("清除已保存的 API Key")).toHaveCount(0);
  reject = true;
  await form.getByLabel("模型名称").fill("broken-model");
  await form.getByRole("button", { name: "验证并保存" }).click();
  await expect(form.getByRole("alert")).toContainText("模型连接超时");
  await expect(form.locator(".model-current")).toContainText("Qwen-synthetic");
  await form.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(form.getByLabel("模型名称")).toHaveValue("Qwen-synthetic");
  await expect(form.getByText("qwen3-embedding:0.6b · 1024 维")).toBeVisible();
  const directory = resolve(import.meta.dirname, "../../.cache/ui");
  mkdirSync(directory, { recursive: true });
  await page.screenshot({
    path: resolve(directory, "model-settings-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: resolve(directory, "model-settings-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(browserErrors).toEqual([]);
});

test("running application exposes current model settings without mutation", async ({
  page,
  request,
}) => {
  const token = process.env.WORKMEMORY_TEST_TOKEN;
  test.skip(!token, "Requires an isolated test server");
  const response = await request.get("/api/config/models", {
    headers: { Authorization: "Bearer " + token },
  });
  expect(response.ok()).toBe(true);
  const saved = await response.json();
  expect(saved.generation).not.toHaveProperty("api_key");
  await page.addInitScript(
    (token) => sessionStorage.setItem("workmemory-token", token),
    token!,
  );
  await page.goto("/");
  await page.getByRole("button", { name: "偏好与状态", exact: true }).click();
  const form = page.getByRole("region", { name: "模型配置" });
  await expect(form.getByLabel("模型名称")).toHaveValue(saved.generation.model);
  await expect(form.getByLabel("接口基础地址")).toHaveValue(
    saved.generation.base_url,
  );
  await expect(
    form.getByText(
      `${saved.embedding.model} · ${saved.embedding.dimensions} 维`,
    ),
  ).toBeVisible();
});
