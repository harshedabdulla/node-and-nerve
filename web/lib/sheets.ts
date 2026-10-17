import "server-only";

import { ComposioError, runTool } from "@/lib/composio";

// Google Sheets through Composio. The tools' output shapes are not fixed in Composio's spec,
// so values are located defensively and a clear error names what came back instead.

type Json = unknown;

function find(value: Json, test: (v: Json) => boolean, depth = 0): Json | undefined {
  if (test(value)) return value;
  if (depth > 5 || value === null || typeof value !== "object") return undefined;
  for (const v of Array.isArray(value) ? value : Object.values(value as object)) {
    const hit = find(v, test, depth + 1);
    if (hit !== undefined) return hit;
  }
  return undefined;
}

function keysOf(value: Json): string {
  return value && typeof value === "object" ? Object.keys(value).join(", ") : typeof value;
}

export interface Spreadsheet {
  id: string;
  name: string;
  modified: string | null;
}

export async function listSpreadsheets(workspaceId: string, accountId: string): Promise<Spreadsheet[]> {
  const data = await runTool(workspaceId, accountId, "GOOGLESHEETS_SEARCH_SPREADSHEETS", {
    max_results: 50,
    order_by: "modifiedTime desc",
  });
  const list = find(
    data,
    (v) =>
      Array.isArray(v) &&
      v.every((x) => x && typeof x === "object" && "id" in x && "name" in x),
  ) as { id: string; name: string; modifiedTime?: string; modified_time?: string }[] | undefined;
  if (!list) throw new ComposioError(`unexpected spreadsheet list (keys: ${keysOf(data)})`);
  return list.map((s) => ({
    id: String(s.id),
    name: String(s.name),
    modified: s.modifiedTime ?? s.modified_time ?? null,
  }));
}

export async function sheetNames(
  workspaceId: string,
  accountId: string,
  spreadsheetId: string,
): Promise<string[]> {
  const data = await runTool(workspaceId, accountId, "GOOGLESHEETS_GET_SHEET_NAMES", {
    spreadsheet_id: spreadsheetId,
  });
  const names = find(
    data,
    (v) => Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "string"),
  ) as string[] | undefined;
  if (!names) throw new ComposioError(`unexpected sheet list (keys: ${keysOf(data)})`);
  return names;
}

const quote = (sheet: string) => `'${sheet.replace(/'/g, "''")}'`;

/** The sheet's rows, header first, as text. At most 5,000 data rows and 50 columns. */
export async function readSheet(
  workspaceId: string,
  accountId: string,
  spreadsheetId: string,
  sheet: string,
  maxRows = 5001,
): Promise<string[][]> {
  const data = await runTool(workspaceId, accountId, "GOOGLESHEETS_BATCH_GET", {
    spreadsheet_id: spreadsheetId,
    ranges: [`${quote(sheet)}!A1:AX${maxRows}`],
  });
  const holder = find(
    data,
    (v) => !!v && typeof v === "object" && !Array.isArray(v) && "values" in (v as object),
  ) as { values?: Json } | undefined;
  const values = holder?.values;
  if (values === undefined) return []; // Google omits "values" for an empty range
  if (!Array.isArray(values)) throw new ComposioError(`unexpected sheet data (keys: ${keysOf(data)})`);
  return values.map((row) => (Array.isArray(row) ? row.map((c) => (c == null ? "" : String(c))) : []));
}
