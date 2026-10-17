// Applies db/schema.sql to DATABASE_URL. Usage: DATABASE_URL=... node scripts/migrate.mjs
import { readFileSync } from "node:fs";
import { neon } from "@neondatabase/serverless";

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("DATABASE_URL is not set");
  process.exit(2);
}
const sql = neon(url);
const statements = readFileSync(new URL("../db/schema.sql", import.meta.url), "utf8")
  .replace(/--.*$/gm, "")
  .split(";")
  .map((s) => s.trim())
  .filter(Boolean);
for (const statement of statements) await sql.query(statement);
const tables = await sql`SELECT table_name FROM information_schema.tables
  WHERE table_schema = 'public' ORDER BY table_name`;
console.log(`applied ${statements.length} statements; tables: ${tables.map((t) => t.table_name).join(", ")}`);
