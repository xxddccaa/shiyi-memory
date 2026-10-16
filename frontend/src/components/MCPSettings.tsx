import { useEffect, useState } from "react";
import { Globe, Plus, PlugZap } from "lucide-react";
import { api, post } from "../api";
import ChatSheet from "./ChatSheet";

interface Server {
  id: string; label: string; transport: "http" | "stdio" | "weather"; enabled: boolean;
  url: string | null; command: string | null; args: string[]; allowed_tools: string[];
  has_token?: boolean; env_keys?: string[];
}
interface Settings { revision: number; servers: Server[]; city: string | null }
interface Tool { name: string; description: string; eligible: boolean }
const blank = (): Server => ({id: "", label: "", transport: "http", enabled: false, url: "", command: "", args: [], allowed_tools: []});
const payload = (server: Server) => {
  const {has_token: _token, env_keys: _env, ...value} = server;
  return value;
};
export default function MCPSettings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [editing, setEditing] = useState<Server | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [token, setToken] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [env, setEnv] = useState("");
  const [args, setArgs] = useState("");
  const [tools, setTools] = useState<Tool[]>([]);
  const [city, setCity] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => { void reload().catch((e) => setError(e.message)); }, []);
  async function reload() {
    const value = await api<Settings>("/mcp/servers");
    setSettings(value); setCity(value.city || ""); return value;
  }
  async function action(job: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await job(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function edit(value?: Server) {
    setEditing(value ? {...value} : blank()); setIsNew(!value); setToken(""); setClearToken(false); setEnv(""); setTools([]);
    setArgs((value?.args || []).join("\n")); setRevision(settings?.revision || 0); setError(""); setNotice("");
  }
  function data() {
    if (!editing) throw new Error("请先选择服务");
    const value: Record<string, unknown> = {...payload(editing), args: args.split("\n").filter(Boolean)};
    if (token || clearToken) value.token = clearToken ? null : token;
    if (env.trim()) {
      try {
        const parsed = JSON.parse(env);
        if (!parsed || Array.isArray(parsed) || Object.values(parsed).some((v) => typeof v !== "string")) throw new Error();
        value.env = parsed;
      } catch { throw new Error("环境变量应为 JSON 对象，值必须为字符串。"); }
    }
    return value;
  }
  async function save() {
    const value = await api<Settings>("/mcp/servers", {method: "PUT", body: JSON.stringify({expected_revision: revision, server: data()})});
    setSettings(value); setEditing(null); setToken(""); setEnv(""); setNotice("工具设置已保存，后续调用按新设置执行。");
  }
  return <section className="panel settings-card mcp-settings">
    <div className="panel-heading"><h2><Globe size={18} />联网与工具</h2><span className="local-pill">MCP</span></div>
    <p>网页和桌宠共用这些工具。搜索与天气默认关闭，启用后助手可按需调用。</p>
    <p className="muted small">公开搜索和城市查询会发送给对应服务。应用内记忆与修改始终使用本机工具，修改仍需你确认。</p>
    <div className="mcp-server-list">{settings?.servers?.map((server) => <article key={server.id}>
      <div><strong>{server.label}</strong><small>{server.enabled ? "已启用" : "未启用"} · {server.allowed_tools.length} 项已选工具</small></div>
      <button disabled={busy} onClick={() => void action(async () => {
        const value = await api<Settings>("/mcp/servers", {method: "PUT", body: JSON.stringify({expected_revision: settings.revision, server: {...payload(server), enabled: !server.enabled}})});
        setSettings(value); setNotice(server.enabled ? "已停用，后续不再调用此服务。" : "已启用。首次调用会检查连接，失败时会说明原因。");
      })}>{server.enabled ? "停用" : "启用"}</button>
      <button disabled={busy} onClick={() => edit(server)}>设置</button>
    </article>)}</div>
    <button className="text-button" disabled={busy || !settings} onClick={() => edit()}><Plus size={15} />添加只读 MCP 服务</button>
    <form className="mcp-city" onSubmit={(e) => { e.preventDefault(); void action(async () => {
      const value = await api<Settings>("/mcp/city", {method: "PUT", body: JSON.stringify({expected_revision: settings!.revision, city: city.trim() || null})});
      setSettings(value); setNotice(city.trim() ? "已保存常用城市，未读取设备定位。" : "已清除常用城市。");
    }); }}>
      <label>常用城市<input disabled={busy || !settings} aria-label="常用城市" placeholder="例如：浙江省杭州市" maxLength={100} value={city} onChange={(e) => setCity(e.target.value)} /></label>
      <button disabled={busy || !settings}>保存城市</button>
    </form>
    {!editing && error && <p role="alert">{error}<button className="text-button" onClick={() => void action(async () => { await reload(); })}>刷新设置</button></p>}
    {!editing && notice && <p role="status">{notice}</p>}
    {editing && <ChatSheet title={isNew ? "添加只读服务" : editing.label} onClose={() => { if (!busy) {setEditing(null); setToken(""); setEnv(""); setError("");} }}>
      <form className="mcp-editor" onSubmit={(e) => {e.preventDefault(); void action(save);}}>
        <label>服务标识<input aria-label="服务标识" required disabled={!isNew || busy} pattern="[a-z][a-z0-9_-]{0,24}" value={editing.id} onChange={(e) => setEditing({...editing, id: e.target.value})} /></label>
        <label>显示名称<input aria-label="服务名称" required maxLength={80} value={editing.label} onChange={(e) => setEditing({...editing, label: e.target.value})} /></label>
        {editing.transport !== "weather" && <label>连接方式<select aria-label="MCP 连接方式" value={editing.transport} onChange={(e) => {setEditing({...editing, transport: e.target.value as Server["transport"], allowed_tools: []}); setTools([]);}}><option value="http">HTTP（Streamable HTTP）</option><option value="stdio">本机已安装程序（stdio）</option></select></label>}
        {editing.transport === "http" && <>
          <label>服务地址<input aria-label="MCP 服务地址" type="url" required value={editing.url || ""} onChange={(e) => {setEditing({...editing, url: e.target.value}); setTools([]);}} placeholder="https://example.com/mcp" /></label>
          <label>访问凭据（可选）<input aria-label="MCP 访问凭据" type="password" autoComplete="new-password" value={token} onChange={(e) => {setToken(e.target.value); setClearToken(false);}} placeholder={editing.has_token ? "已保存，留空保留；更换地址后需重新填写" : "免密服务可留空"} /></label>
          {editing.has_token && <label className="mcp-check"><input type="checkbox" checked={clearToken} onChange={(e) => {setClearToken(e.target.checked); setToken("");}} />清除已保存凭据</label>}
        </>}
        {editing.transport === "stdio" && <>
          <p className="muted">只选择你信任的已安装服务。程序以当前用户权限运行；不会自动下载安装。不要填写 Shell 或包下载命令。</p>
          <label>程序绝对路径<input aria-label="MCP 程序路径" required value={editing.command || ""} onChange={(e) => setEditing({...editing, command: e.target.value})} placeholder="/absolute/path/to/server" /></label>
          <label>启动参数（每行一个）<textarea aria-label="MCP 启动参数" value={args} onChange={(e) => setArgs(e.target.value)} /></label>
          <label>环境变量（可选，JSON）<textarea aria-label="MCP 环境变量" autoComplete="off" value={env} onChange={(e) => setEnv(e.target.value)} placeholder={editing.env_keys?.length ? `已保存：${editing.env_keys.join("、")}。留空保留，填 {} 清除` : '{"API_KEY":"…"}'} /></label>
        </>}
        <button type="button" disabled={busy} onClick={() => void action(async () => {
          const value = await post<{tools: Tool[]; notice: string}>("/mcp/servers/test", {server: data()});
          setTools(value.tools); setNotice(value.notice);
        })}><PlugZap size={14} />测试连接并发现工具</button>
        <p className="muted small">仅向 Pi 开放你勾选并信任的只读工具。服务声明不能代替信任，新增或变更的工具请重新核对。</p>
        {tools.map((tool) => <label className="mcp-tool" key={tool.name}>
          <input type="checkbox" disabled={!tool.eligible} checked={tool.eligible && editing.allowed_tools.includes(tool.name)} onChange={(e) => setEditing({...editing, allowed_tools: e.target.checked ? [...editing.allowed_tools, tool.name] : editing.allowed_tools.filter((t) => t !== tool.name)})} />
          <span><strong>{tool.name}</strong><small>{tool.description}</small>{!tool.eligible && <small>未声明安全只读或属于受限操作，不能启用</small>}</span>
        </label>)}
        {!tools.length && !!editing.allowed_tools.length && <p className="muted">已选择：{editing.allowed_tools.join("、")}</p>}
        <label className="mcp-check"><input type="checkbox" checked={editing.enabled} onChange={(e) => setEditing({...editing, enabled: e.target.checked})} />保存后允许助手使用已选工具</label>
        {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
        <div className="mcp-buttons"><button className="primary" disabled={busy}>{busy ? "处理中…" : "保存设置"}</button>
          {!isNew && <button type="button" className="danger" disabled={busy} onClick={() => void action(async () => {
            const value = await api<Settings>(`/mcp/servers/${editing.id}?expected_revision=${revision}`, {method: "DELETE"});
            setSettings(value); setEditing(null); setToken(""); setEnv(""); setNotice("已移除此服务。");
          })}>移除服务</button>}
          <button type="button" disabled={busy} onClick={() => void action(async () => { const value = await reload(); setRevision(value.revision); setEditing(null); setToken(""); setEnv(""); })}>重新加载</button>
        </div>
      </form>
    </ChatSheet>}
  </section>;
}
