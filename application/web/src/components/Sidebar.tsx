import { formatBrandTitle } from "../formatBrandTitle";
import type { AppConfig, Task } from "../types";
import { ConfigDrawer } from "./ConfigDrawer";
import { TaskListItem } from "./TaskListItem";
import { GuardrailIcon, LogoutIcon, McpIcon, ModelIcon, NewTaskIcon, SkillIcon } from "./SidebarIcons";

type DrawerKind = "skill" | "mcp" | null;

interface Props {
  userId: string;
  tasks: Task[];
  activeTask: Task | null;
  config: AppConfig | null;
  drawer: DrawerKind;
  onNewTask: () => void;
  onSelectTask: (id: string) => void;
  onOpenDrawer: (kind: DrawerKind) => void;
  onCloseDrawer: () => void;
  onPatchTask: (taskId: string, patch: Partial<Task>) => void;
  onDeleteTask: (taskId: string) => void;
  onLogout: () => void;
}

export function Sidebar({
  userId,
  tasks,
  activeTask,
  config,
  drawer,
  onNewTask,
  onSelectTask,
  onOpenDrawer,
  onCloseDrawer,
  onPatchTask,
  onDeleteTask,
  onLogout,
}: Props) {
  const skills = activeTask?.skills ?? config?.default_skills ?? [];
  const mcpServers = activeTask?.mcp_servers ?? config?.default_mcp_servers ?? [];
  const brandTitle = formatBrandTitle(config?.projectName ?? "agent", userId);

  return (
    <>
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand-row">
            <div className="brand">{brandTitle}</div>
            <button
              type="button"
              className="brand-logout-btn"
              aria-label="나가기"
              title="나가기"
              onClick={onLogout}
            >
              <LogoutIcon className="sidebar-icon" />
            </button>
          </div>
        </div>

        <button type="button" className="sidebar-menu-btn" onClick={onNewTask}>
          <NewTaskIcon className="sidebar-icon" />
          <span>New task</span>
        </button>

        <div className="task-list">
          {tasks.map((task) => (
            <TaskListItem
              key={task.id}
              task={task}
              active={activeTask?.id === task.id}
              onSelect={() => onSelectTask(task.id)}
              onDelete={() => onDeleteTask(task.id)}
              onRename={(title) => onPatchTask(task.id, { title })}
              onTogglePin={() => onPatchTask(task.id, { pinned: !task.pinned })}
            />
          ))}
        </div>

        <div className="sidebar-section">
          <div className="section-label">Configuration</div>
          <button
            type="button"
            className="sidebar-menu-btn"
            onClick={() => onOpenDrawer("skill")}
            disabled={!activeTask}
          >
            <SkillIcon className="sidebar-icon" />
            <span>Skill ({skills.length})</span>
          </button>
          <button
            type="button"
            className="sidebar-menu-btn"
            onClick={() => onOpenDrawer("mcp")}
            disabled={!activeTask}
          >
            <McpIcon className="sidebar-icon" />
            <span>MCP ({mcpServers.length})</span>
          </button>
          <label className="sidebar-menu-btn model-select-row">
            <ModelIcon className="sidebar-icon" />
            <select
              className="model-select"
              value={activeTask?.model_name ?? config?.default_model ?? ""}
              disabled={!activeTask}
              onChange={(e) =>
                activeTask && onPatchTask(activeTask.id, { model_name: e.target.value })
              }
            >
              {(config?.models ?? []).map((model) => (
                <option key={model} value={model}>
                  {model}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="sidebar-section">
          <div className="section-label">Settings</div>
          <label className="sidebar-menu-btn settings-toggle">
            <GuardrailIcon className="sidebar-icon" />
            <span>Guardrail</span>
            <input
              type="checkbox"
              checked={activeTask?.guardrail_enabled ?? false}
              disabled={!activeTask}
              onChange={(e) =>
                activeTask &&
                onPatchTask(activeTask.id, { guardrail_enabled: e.target.checked })
              }
            />
          </label>
        </div>
      </aside>

      {drawer === "skill" && config && activeTask && (
        <ConfigDrawer
          title="Skill"
          options={config.skills}
          selected={skills}
          onChange={(next) => activeTask && onPatchTask(activeTask.id, { skills: next })}
          onClose={onCloseDrawer}
        />
      )}
      {drawer === "mcp" && config && activeTask && (
        <ConfigDrawer
          title="MCP"
          options={config.mcp_servers}
          selected={mcpServers}
          onChange={(next) => activeTask && onPatchTask(activeTask.id, { mcp_servers: next })}
          onClose={onCloseDrawer}
        />
      )}
    </>
  );
}
