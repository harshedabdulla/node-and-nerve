import type { Metadata, Viewport } from "next";
import { Geist_Mono, Instrument_Serif, Inter_Tight } from "next/font/google";
import "./globals.css";

// Spindle's type system: a sans for everything, Instrument Serif for the one big line,
// Geist Mono for figures and labels. Inter Tight stands in for Neue Montreal (licensed).
const sans = Inter_Tight({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600"],
  variable: "--font-sans-family",
  display: "swap",
});
const serif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-serif-family",
  display: "swap",
});
const mono = Geist_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono-family",
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
    <html lang="en" className={`${sans.variable} ${serif.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
