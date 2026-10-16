import { useState, type ReactNode } from "react";
import { Bot, Cat, Database, Globe, Archive, Activity } from "lucide-react";
import { CompanionPreferencesProvider } from "./CompanionSettings";

const categories = [
  {
    id: "models",
    label: "模型设置",
    icon: Bot,
    description: "选择整理与问答使用的模型",
  },
  {
    id: "pet",
    label: "桌宠与陪伴",
    icon: Cat,
    description: "调整形象、提醒和陪伴方式",
  },
  {
    id: "memory",
    label: "采集与记忆",
    icon: Database,
    description: "管理采集、原文复核和记忆同步",
  },
  {
    id: "tools",
    label: "联网与工具",
    icon: Globe,
    description: "连接搜索、天气和外部工具",
  },
  {
    id: "maintenance",
    label: "数据维护",
    icon: Archive,
    description: "查看备份和数据维护任务",
  },
  {
    id: "status",
    label: "服务状态",
    icon: Activity,
    description: "检查服务、任务和模型请求",
  },
] as const;
type Category = (typeof categories)[number]["id"];

export default function SettingsLayout({
  panels,
}: {
  panels: Record<Category, ReactNode>;
}) {
  const [active, setActive] = useState<Category>("models");
  const [visited, setVisited] = useState<Category[]>(["models"]);
  function select(id: Category) {
    setActive(id);
    setVisited((old) => (old.includes(id) ? old : [...old, id]));
  }
  return (
    <CompanionPreferencesProvider>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分类">
          {categories.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              aria-current={active === id ? "page" : undefined}
              aria-controls={`settings-${id}`}
              onClick={() => select(id)}
            >
              <Icon size={18} />
              {label}
            </button>
          ))}
        </nav>
        <label className="settings-mobile-nav">
          设置分类
          <select
            aria-label="设置分类"
            value={active}
            onChange={(e) => select(e.target.value as Category)}
          >
            {categories.map((c) => (
              <option value={c.id} key={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <div className="settings-content">
          {categories
            .filter((c) => visited.includes(c.id))
            .map((c) => (
              <div key={c.id} id={`settings-${c.id}`} hidden={active !== c.id}>
                <p className="settings-intro">{c.description}</p>
                <div className="settings-panels">{panels[c.id]}</div>
              </div>
            ))}
        </div>
      </div>
    </CompanionPreferencesProvider>
  );
}
