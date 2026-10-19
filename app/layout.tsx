import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "纸间 · PDF 译制台",
  description: "将混合语言 PDF 译为简体中文，并尽量保留原有版式。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
