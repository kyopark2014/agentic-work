/** Host-wide signal that use-vault mutation tools finished — open vault MCP Apps should reload. */

export const VAULT_MUTATED_EVENT = "mcp-vault-mutated";

export const VAULT_MUTATION_TOOLS = new Set([
  "vault_write",
  "vault_append",
  "vault_str_replace",
  "vault_upload",
  "vault_delete",
  "vault_mkdir",
  "vault_rename",
  "vault_rebuild",
  "vault_move",
]);

export interface VaultMutatedDetail {
  tool?: string;
  path?: string;
}

function pathFromInput(input: unknown): string | undefined {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined;
  const rec = input as Record<string, unknown>;
  for (const key of ["path", "to_path", "toPath", "file_path", "filePath"]) {
    const value = rec[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

export function notifyVaultMutated(detail: VaultMutatedDetail): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(VAULT_MUTATED_EVENT, { detail }));
}

/** Fire when a streamed tool_result belongs to a vault write/mutation tool. */
export function notifyVaultMutatedFromToolResult(
  tool: string | undefined,
  input: unknown,
): void {
  const name = tool?.trim();
  if (!name || !VAULT_MUTATION_TOOLS.has(name)) return;
  notifyVaultMutated({
    tool: name,
    path: pathFromInput(input),
  });
}
