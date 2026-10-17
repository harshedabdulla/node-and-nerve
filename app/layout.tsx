import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Node & Nerve | Graph-Powered Company Intelligence",
  description:
    "Connect business context, operational records and decisions into a graph that helps AI agents investigate how everything relates.",
  keywords: [
    "Node & Nerve",
    "FalkorDB",
    "Company Brain",
    "Graph Database",
    "AI Agents",
    "Knowledge Graph",
    "Organizational Context",
  ],
  authors: [{ name: "Spindle / Node & Nerve" }],
  openGraph: {
    title: "Node & Nerve | Graph-Powered Company Intelligence",
    description:
      "Connect business context, operational records and decisions into a graph that helps AI agents investigate how everything relates.",
    type: "website",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: "#000000",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`dark ${inter.variable}`}>
      <head>
        {/* BubbledotICG-FinePos font CDN */}
        <link
          rel="stylesheet"
          href="https://db.onlinewebfonts.com/c/8cb707a9b8a73f8a7403336b861c3074?family=BubbledotICG-FinePos"
        />
        {/* Preload local fonts */}
        <link
          rel="preload"
          href="/fonts/BubbledotICG-FinePos.woff2"
          as="font"
          type="font/woff2"
          crossOrigin="anonymous"
        />
        <link
          rel="preload"
          href="/fonts/GeistPixel-Circle.woff2"
          as="font"
          type="font/woff2"
          crossOrigin="anonymous"
        />
      </head>
      <body className="bg-black text-white antialiased min-h-screen selection:bg-white selection:text-black">
        {children}
      </body>
    </html>
  );
}
