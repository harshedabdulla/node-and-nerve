import type { Metadata, Viewport } from "next";
import { Schibsted_Grotesk } from "next/font/google";
import "./globals.css";

const schibsted = Schibsted_Grotesk({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-schibsted",
  display: "swap",
});

const description =
  "Node & Nerve connects the records your business already keeps into one graph on FalkorDB, " +
  "then answers questions with the entries, links and sources behind every figure.";

export const metadata: Metadata = {
  title: "Node & Nerve: answers from your connected company records",
  description,
  openGraph: { title: "Node & Nerve", description, type: "website" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#000000",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={schibsted.variable}>
      <head>
        <link
          rel="preload"
          href="/fonts/BubbledotICG-FinePos.woff2"
          as="font"
          type="font/woff2"
          crossOrigin="anonymous"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
