import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { uiError, uiLog } from "./debug";
import { formatBrandTitle } from "./formatBrandTitle";
import { useChatStream } from "./hooks/useChatStream";
import type { AppConfig, Message, Task } from "./types";
import { Sidebar } from "./components/Sidebar";
import { ChatThread } from "./components/ChatThread";
import { ChatInput } from "./components/ChatInput";
import { UserIdModal } from "./components/UserIdModal";

type DrawerKind = "skill" | "mcp" | null;

function sortTasks(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}

export default function App() {
  const [userId, setUserId] = useState<string | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [drawer, setDrawer] = useState<DrawerKind>(null);
  const { streaming, streamText, streamTools, sendMessage } = useChatStream();

  const activeTask = tasks.find((t) => t.id === activeTaskId) ?? null;

  const loadMessages = useCallback(async (taskId: string) => {
    uiLog("messages:load start", { taskId });
    const { messages: rows } = await api.getMessages(taskId);
    uiLog("messages:load complete", { taskId, count: rows.length, roles: rows.map((m) => m.role) });
    setMessages(rows);
  }, []);

  const refreshTasks = useCallback(async () => {
    const { tasks: rows } = await api.listTasks();
    setTasks(sortTasks(rows));
    return sortTasks(rows);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const session = await api.getSession();
        const id = session?.user_id?.trim();
        if (id) {
          setUserId(id);
        }
        setConfig(await api.getConfig());
      } catch (err) {
        uiError("boot failed", err);
        setBootError(err instanceof Error ? err.message : String(err));
      } finally {
        setAuthReady(true);
      }
    })();
  }, []);

  useEffect(() => {
    if (!config?.projectName || !userId) return;
    document.title = formatBrandTitle(config.projectName, userId);
  }, [config?.projectName, userId]);

  useEffect(() => {
    if (!userId) return;
    (async () => {
      const rows = await refreshTasks();
      if (rows.length === 0) {
        const cfg = config ?? (await api.getConfig());
        const task = await api.createTask({
          model_name: cfg.default_model,
          skills: cfg.default_skills,
          mcp_servers: cfg.default_mcp_servers,
        });
        setTasks([task]);
        setActiveTaskId(task.id);
        setMessages([]);
      } else {
        setActiveTaskId(rows[0].id);
        await loadMessages(rows[0].id);
      }
    })();
  }, [userId, config, refreshTasks, loadMessages]);

  useEffect(() => {
    if (activeTaskId) {
      loadMessages(activeTaskId);
    }
  }, [activeTaskId, loadMessages]);

  async function handleLogin(id: string) {
    setBootError(null);
    try {
      await api.setSession(id);
      setUserId(id.trim());
    } catch (err) {
      setBootError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleLogout() {
    setBootError(null);
    try {
      await api.clearSession();
    } catch (err) {
      uiError("logout failed", err);
    }
    setUserId(null);
    setTasks([]);
    setActiveTaskId(null);
    setMessages([]);
    setDrawer(null);
    if (config?.projectName) {
      document.title = formatBrandTitle(config.projectName);
    }
  }

  async function handleNewTask() {
    if (!config) return;
    const task = await api.createTask({
      model_name: activeTask?.model_name ?? config.default_model,
      skills: activeTask?.skills ?? config.default_skills,
      mcp_servers: activeTask?.mcp_servers ?? config.default_mcp_servers,
      guardrail_enabled: activeTask?.guardrail_enabled ?? false,
    });
    setTasks((prev) => [task, ...prev]);
    setActiveTaskId(task.id);
    setMessages([]);
  }

  async function handleSelectTask(id: string) {
    setActiveTaskId(id);
    await loadMessages(id);
  }

  async function handlePatchTask(taskId: string, patch: Partial<Task>) {
    const updated = await api.patchTask(taskId, patch);
    setTasks((prev) => sortTasks(prev.map((t) => (t.id === updated.id ? updated : t))));
  }

  async function handleDeleteTask(taskId: string) {
    await api.deleteTask(taskId);
    const rows = await refreshTasks();
    if (activeTaskId !== taskId) return;
    if (rows.length > 0) {
      setActiveTaskId(rows[0].id);
      await loadMessages(rows[0].id);
      return;
    }
    if (!config) return;
    const task = await api.createTask({
      model_name: config.default_model,
      skills: config.default_skills,
      mcp_servers: config.default_mcp_servers,
    });
    setTasks([task]);
    setActiveTaskId(task.id);
    setMessages([]);
  }

  async function handleSend(prompt: string) {
    if (!activeTaskId) {
      uiError("chat:send skipped — no active task");
      return;
    }

    uiLog("chat:handleSend", { taskId: activeTaskId, prompt });
    const optimistic: Message = {
      id: `pending-${crypto.randomUUID()}`,
      task_id: activeTaskId,
      role: "user",
      content: prompt,
      images: [],
      tool_events: [],
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);

    await sendMessage(activeTaskId, prompt, async () => {
      await loadMessages(activeTaskId);
      await refreshTasks();
    });
  }

  if (!authReady || !userId) {
    return <UserIdModal onSubmit={handleLogin} error={bootError} />;
  }

  return (
    <div className="app-shell">
      <Sidebar
        userId={userId}
        tasks={tasks}
        activeTask={activeTask}
        config={config}
        drawer={drawer}
        onNewTask={handleNewTask}
        onSelectTask={handleSelectTask}
        onOpenDrawer={setDrawer}
        onCloseDrawer={() => setDrawer(null)}
        onPatchTask={handlePatchTask}
        onDeleteTask={handleDeleteTask}
        onLogout={handleLogout}
      />
      <div className="main-panel">
        <ChatThread
          messages={messages}
          streaming={streaming}
          streamText={streamText}
          streamTools={streamTools}
          taskTitle={activeTask?.title ?? "New task"}
          footer={
            <ChatInput disabled={!activeTask || streaming} onSend={handleSend} />
          }
        />
      </div>
    </div>
  );
}
