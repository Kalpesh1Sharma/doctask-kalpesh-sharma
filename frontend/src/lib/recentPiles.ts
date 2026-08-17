import type { Pile } from "./types";

const KEY = "task1.recent-piles.v1";
const MAX_RECENT = 12;

export type RecentPile = Pick<Pile, "pile_id" | "name" | "updated_at">;

export function loadRecentPiles(): RecentPile[] {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "[]") as unknown;
    if (!Array.isArray(value)) return [];
    return value.filter(isRecentPile).slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

export function rememberPile(pile: Pile): RecentPile[] {
  const next = [
    { pile_id: pile.pile_id, name: pile.name, updated_at: pile.updated_at },
    ...loadRecentPiles().filter((item) => item.pile_id !== pile.pile_id)
  ].slice(0, MAX_RECENT);
  localStorage.setItem(KEY, JSON.stringify(next));
  return next;
}

function isRecentPile(value: unknown): value is RecentPile {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return typeof record.pile_id === "string" && typeof record.name === "string" && typeof record.updated_at === "string";
}
