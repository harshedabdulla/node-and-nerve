import "server-only";

import { neon } from "@neondatabase/serverless";

// The control-plane database (Neon Postgres). Server only: the browser never sees it.
let client: ReturnType<typeof neon> | null = null;

export function sql() {
  if (!client) {
    const url = process.env.DATABASE_URL;
    if (!url) throw new Error("DATABASE_URL is not configured");
    client = neon(url);
  }
  return client;
}
