import { Database } from "bun:sqlite";
import { runMigrations } from "./migrations";

export function openDatabase(path: string): Database {
  const db = new Database(path, { create: true, strict: true });
  runMigrations(db);
  return db;
}
