import "server-only";

import { assertOwnAccount, ComposioError, proxyGet } from "@/lib/composio";

// Zoho Books through Composio's proxy (Composio exposes no Zoho Books tools, but signs our
// requests to Zoho's own API). Read-only. Zoho serves each organisation from one data
// centre, which Composio does not reveal, so it is found once by asking each in turn.

const DATA_CENTRES = ["in", "com", "eu", "com.au", "jp", "ca", "sa"];
const MAX_PAGES = 25; // 25 x 200 records per type
const MAX_PAYMENT_DETAILS = 300;

type Row = Record<string, unknown>;

export interface BooksLocation {
  data_centre: string;
  organization_id: string;
  organization_name: string;
}

const base = (dc: string) => `https://www.zohoapis.${dc}/books/v3`;

export async function locateBooks(workspaceId: string, accountId: string): Promise<BooksLocation> {
  await assertOwnAccount(workspaceId, accountId);
  for (const dc of DATA_CENTRES) {
    const r = await proxyGet(workspaceId, accountId, `${base(dc)}/organizations`);
    const orgs = (r.data as { organizations?: Row[] } | null)?.organizations;
    if (r.status === 200 && orgs?.length) {
      const org = orgs.find((o) => o.is_default_org) ?? orgs[0];
      return {
        data_centre: dc,
        organization_id: String(org.organization_id),
        organization_name: String(org.name ?? ""),
      };
    }
  }
  throw new ComposioError("no Zoho Books organisation was found for this account");
}

async function list(ws: string, acct: string, at: BooksLocation, kind: string): Promise<Row[]> {
  const rows: Row[] = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const r = await proxyGet(ws, acct, `${base(at.data_centre)}/${kind}`, {
      organization_id: at.organization_id,
      per_page: "200",
      page: String(page),
    });
    const data = r.data as (Row & { page_context?: { has_more_page?: boolean } }) | null;
    if (r.status !== 200 || !data || !Array.isArray(data[kind])) {
      const message = data && typeof data.message === "string" ? data.message : `HTTP ${r.status}`;
      throw new ComposioError(`Zoho Books ${kind}: ${message}`);
    }
    rows.push(...(data[kind] as Row[]));
    if (!data.page_context?.has_more_page) break;
  }
  return rows;
}

export interface BooksData {
  notes: string[];
  contacts: Row[];
  invoices: Row[];
  bills: Row[];
  customer_payments: Row[];
  vendor_payments: Row[];
}

/** Everything the import needs, with each customer payment's applied invoices. */
export async function readBooks(ws: string, acct: string, at: BooksLocation): Promise<BooksData> {
  await assertOwnAccount(ws, acct);
  const [contacts, invoices, bills, payments, vendorPayments] = [
    await list(ws, acct, at, "contacts"),
    await list(ws, acct, at, "invoices"),
    await list(ws, acct, at, "bills"),
    await list(ws, acct, at, "customerpayments"),
    await list(ws, acct, at, "vendorpayments"),
  ];
  const detailed: Row[] = [];
  for (const p of payments.slice(0, MAX_PAYMENT_DETAILS)) {
    const r = await proxyGet(ws, acct, `${base(at.data_centre)}/customerpayments/${p.payment_id}`, {
      organization_id: at.organization_id,
    });
    const payment = (r.data as { payment?: Row } | null)?.payment;
    detailed.push({ ...p, invoices: (payment?.invoices as Row[] | undefined) ?? [] });
  }
  const notes =
    payments.length > MAX_PAYMENT_DETAILS
      ? [`Only the first ${MAX_PAYMENT_DETAILS} customer payments were matched to invoices.`]
      : [];
  return {
    notes,
    contacts,
    invoices,
    bills,
    customer_payments: detailed,
    vendor_payments: vendorPayments,
  };
}
