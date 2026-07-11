import type { AppConfig, Task } from "../types";
import { ConfigDrawer } from "./ConfigDrawer";
import { TaskListItem } from "./TaskListItem";

type DrawerKind = "skill" | "mcp" | null;

interface Props {
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
}

export function Sidebar({
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
}: Props) {
  const skills = activeTask?.skills ?? config?.default_skills ?? [];
  const mcpServers = activeTask?.mcp_servers ?? config?.default_mcp_servers ?? [];

  return (
    <>
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand">Agent</div>
        </div>

        <button type="button" className="new-task-btn" onClick={onNewTask}>
          + New task
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
            className="config-btn"
            onClick={() => onOpenDrawer("skill")}
            disabled={!activeTask}
          >
            Skill ({skills.length})
          </button>
          <button
            type="button"
            className="config-btn"
            onClick={() => onOpenDrawer("mcp")}
            disabled={!activeTask}
          >
            MCP ({mcpServers.length})
          </button>
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
        </div>

        <div className="sidebar-section">
          <div className="section-label">Settings</div>
          <label className="settings-toggle">
            Guardrail
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
