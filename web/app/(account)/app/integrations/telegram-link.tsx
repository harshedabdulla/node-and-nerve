"use client";

import { useState, useTransition } from "react";
import { createTelegramLink } from "./actions";

// The link carries a one-time code, so it is shown here and never put in a URL or history.
export function TelegramLink() {
  const [result, setResult] = useState<{ url: string } | { error: string } | null>(null);
  const [pending, start] = useTransition();
  if (result && "url" in result) {
    return (
      <div className="connector-actions">
        <a className="btn btn-primary" href={result.url} target="_blank" rel="noreferrer">
          Open Telegram
        </a>
        <p className="note" style={{ flexBasis: "100%" }}>
          Tap Start in the chat that opens. The link works once and expires in 15 minutes. Use it
          in a private chat with the bot, not a group.
        </p>
      </div>
    );
  }
  return (
    <div className="connector-actions">
      <button
        className="btn btn-primary"
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await createTelegramLink()))}
      >
        {pending ? "Creating link" : "Link Telegram"}
      </button>
      {result && "error" in result && <p className="note">{result.error}</p>}
    </div>
  );
}
