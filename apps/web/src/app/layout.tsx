import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Mandate Guardian",
  description: "Merchant-layer, explainable UPI mandate-risk controls.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
