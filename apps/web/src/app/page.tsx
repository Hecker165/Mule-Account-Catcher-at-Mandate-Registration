export default function Page() {
  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-xl font-bold">Mandate Guardian (demo)</h1>
      <p className="text-sm text-slate-600">
        Merchant-layer, explainable UPI mandate-risk controls.
      </p>
      <nav className="flex gap-4 text-sm">
        <a href="/checkout" className="text-slate-800 underline">
          Checkout
        </a>
        <a href="/dashboard" className="text-slate-800 underline">
          Dashboard
        </a>
      </nav>
    </main>
  );
}
