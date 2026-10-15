import { useEffect, useState } from "react";
import { Cat, Upload } from "lucide-react";
import { api, getToken } from "../api";

type Skin = { id: string; name: string; presentation: string; preview: string | null };

function SkinPreview({ skin }: { skin: Skin }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!skin.preview) return;
    const controller = new AbortController();
    let objectURL = "";
    void fetch("/api" + skin.preview, { headers: { Authorization: "Bearer " + getToken() }, signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("预览不可用");
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        objectURL = URL.createObjectURL(blob);
        setUrl(objectURL);
      }).catch(() => {});
    return () => { controller.abort(); if (objectURL) URL.revokeObjectURL(objectURL); };
  }, [skin.preview]);
  return <span className="pet-skin-preview">{url ? <img src={url} alt="" /> : <Cat size={36} />}</span>;
}

export default function PetSkins({ selected, onSelect }: { selected: string; onSelect: (id: string) => Promise<unknown> }) {
  const [skins, setSkins] = useState<Skin[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let closed = false;
    const load = () => void api<{ items: Skin[] }>("/pet/skins")
      .then((value) => { if (!closed) setSkins(Array.isArray(value.items) ? value.items : []); }).catch((e) => { if (!closed) setError(e.message); });
    load();
    window.addEventListener("client-change", load);
    return () => { closed = true; window.removeEventListener("client-change", load); };
  }, []);
  useEffect(() => {
    if (!file) { setPreview(""); return; }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  async function apply() {
    if (!file || busy) return;
    setBusy(true); setError("");
    try {
      const form = new FormData(); form.append("image", file);
      const response = await fetch("/api/pet/skins", { method: "POST", headers: { Authorization: "Bearer " + getToken() }, body: form });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "导入失败，请检查 PNG 图片");
      setSkins((current) => [...current.filter((s) => s.id !== value.id), value]);
      await onSelect(value.id);
      setFile(null);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="pet-skins">
    <h3>桌宠形象</h3>
    <p className="muted">上传一张 PNG，就能换成喜欢的角色。透明背景效果更自然；普通背景会原样保留。</p>
    <div className="pet-skin-list" aria-label="已导入的形象">
      {skins.map((skin) => <button type="button" key={skin.id} className="pet-skin-choice" aria-pressed={skin.id === selected}
        disabled={busy} onClick={() => { setError(""); void onSelect(skin.id).catch((e: Error) => setError(e.message)); }}>
        <SkinPreview skin={skin} /><span>{skin.name}</span><small>{skin.id === selected ? "正在使用" : skin.presentation === "atlas" ? "动画皮肤" : "点击使用"}</small>
      </button>)}
    </div>
    {selected !== "builtin-cat" && skins.length > 0 && !skins.some((skin) => skin.id === selected) && <p role="status">所选形象文件已丢失或损坏，小猫暂时显示默认形象。可以重新导入或选择默认猫咪。</p>}
    <label className="pet-upload"><Upload size={16} />上传 PNG
      <input aria-label="上传 PNG 形象" type="file" accept="image/png,.png" disabled={busy} onChange={(event) => {
        const next = event.currentTarget.files?.[0]; event.currentTarget.value = ""; setError("");
        if (!next) return;
        if (next.size > 32 * 1024 * 1024) { setError("PNG 文件不能超过 32 MiB"); setFile(null); return; }
        if (!next.name.toLowerCase().endsWith(".png")) { setError("请选择 PNG 图片"); setFile(null); return; }
        setFile(next);
      }} />
    </label>
    {file && <div className="pet-import-preview">
      <span className="pet-skin-preview"><img src={preview} alt="待导入形象预览" onError={() => { setError("无法读取这张 PNG，请重新选择"); setFile(null); }} /></span>
      <div><strong>{file.name}</strong><p className="muted">会自动去掉透明留白，按原比例显示。宽高各不超过 4096 像素。</p></div>
      <button disabled={busy} onClick={() => void apply()}>{busy ? "正在导入…" : "应用此形象"}</button>
      <button className="secondary" disabled={busy} onClick={() => setFile(null)}>取消</button>
    </div>}
    {error && <p role="alert" className="agent-error">{error}</p>}
  </div>;
}
