export function formatBrandTitle(projectName: string): string {
  const cleaned = projectName.replace(/-/g, " ").trim();
  if (!cleaned) return "Agent";
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}
