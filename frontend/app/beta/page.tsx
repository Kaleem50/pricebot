"use client";

/**
 * /beta — Public beta waitlist page.
 *
 * No authentication required. Submits a POST /beta/request to the PriceBot API.
 * Displays a success confirmation after submission.
 */

import { useState } from "react";

type RepricingFrequency = "daily" | "weekly" | "manual";
type Platform = "amazon" | "etsy" | "shopify" | "ebay" | "woocommerce";

const PLATFORMS: { value: Platform; label: string }[] = [
  { value: "amazon", label: "Amazon" },
  { value: "etsy", label: "Etsy" },
  { value: "shopify", label: "Shopify" },
  { value: "ebay", label: "eBay" },
  { value: "woocommerce", label: "WooCommerce" },
];

const FREQUENCIES: { value: RepricingFrequency; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "manual", label: "Manually (as needed)" },
];

interface FormState {
  email: string;
  platform: Platform | "";
  product_count: string;
  reprice_frequency: RepricingFrequency | "";
}

export default function BetaPage() {
  const [form, setForm] = useState<FormState>({
    email: "",
    platform: "",
    product_count: "",
    reprice_frequency: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>
  ) => {
    setForm((prev) => ({ ...prev, [e.target.name]: e.target.value }));
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!form.platform || !form.reprice_frequency) {
      setError("Please fill in all fields.");
      return;
    }

    const productCount = parseInt(form.product_count, 10);
    if (isNaN(productCount) || productCount < 1) {
      setError("Please enter a valid product count (minimum 1).");
      return;
    }

    setSubmitting(true);

    try {
      const backendUrl =
        process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

      const resp = await fetch(`${backendUrl}/beta/request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: form.email,
          platform: form.platform,
          product_count: productCount,
          reprice_frequency: form.reprice_frequency,
        }),
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.detail ?? `Server error (${resp.status})`);
      }

      setSubmitted(true);
    } catch (err: unknown) {
      setError(
        err instanceof Error ? err.message : "Something went wrong. Try again."
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <main className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-md w-full bg-white rounded-2xl shadow-sm border border-gray-200 p-10 text-center">
          <div className="text-4xl mb-4">🎉</div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">
            You&apos;re on the list!
          </h1>
          <p className="text-gray-600">
            Thanks for your interest in PriceBot. We&apos;ll be in touch within{" "}
            <strong>48 hours</strong> with your beta access details.
          </p>
          <p className="text-gray-500 text-sm mt-4">
            Check your inbox — a confirmation email is on its way.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-gray-50 flex items-center justify-center px-4 py-16">
      <div className="max-w-md w-full">
        {/* Header */}
        <div className="text-center mb-8">
          <span className="text-2xl font-bold text-blue-600">PriceBot</span>
          <h1 className="text-3xl font-bold text-gray-900 mt-3 mb-2">
            Join the beta
          </h1>
          <p className="text-gray-600">
            AI-powered repricing for ecommerce sellers. Stop leaving money on
            the table — let PriceBot protect your margins 24/7.
          </p>
        </div>

        {/* Form */}
        <form
          onSubmit={handleSubmit}
          className="bg-white rounded-2xl shadow-sm border border-gray-200 p-8 space-y-5"
        >
          {/* Email */}
          <div>
            <label
              htmlFor="email"
              className="block text-sm font-medium text-gray-700 mb-1.5"
            >
              Email address
            </label>
            <input
              id="email"
              name="email"
              type="email"
              required
              value={form.email}
              onChange={handleChange}
              placeholder="you@example.com"
              className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm
                         focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>

          {/* Platform */}
          <div>
            <label
              htmlFor="platform"
              className="block text-sm font-medium text-gray-700 mb-1.5"
            >
              Primary selling platform
            </label>
            <select
              id="platform"
              name="platform"
              required
              value={form.platform}
              onChange={handleChange}
              className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm
                         focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent
                         bg-white"
            >
              <option value="" disabled>
                Select a platform…
              </option>
              {PLATFORMS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>

          {/* Product count */}
          <div>
            <label
              htmlFor="product_count"
              className="block text-sm font-medium text-gray-700 mb-1.5"
            >
              Approximate number of products
            </label>
            <input
              id="product_count"
              name="product_count"
              type="number"
              required
              min={1}
              max={100000}
              value={form.product_count}
              onChange={handleChange}
              placeholder="e.g. 150"
              className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm
                         focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>

          {/* Reprice frequency */}
          <div>
            <label
              htmlFor="reprice_frequency"
              className="block text-sm font-medium text-gray-700 mb-1.5"
            >
              How often do you currently reprice?
            </label>
            <select
              id="reprice_frequency"
              name="reprice_frequency"
              required
              value={form.reprice_frequency}
              onChange={handleChange}
              className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm
                         focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent
                         bg-white"
            >
              <option value="" disabled>
                Select a frequency…
              </option>
              {FREQUENCIES.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </div>

          {/* Error */}
          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={submitting}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50
                       text-white font-semibold py-2.5 rounded-lg text-sm
                       transition-colors duration-150"
          >
            {submitting ? "Submitting…" : "Request beta access"}
          </button>

          <p className="text-xs text-gray-500 text-center pt-1">
            No spam. We&apos;ll only email you about beta access.
          </p>
        </form>
      </div>
    </main>
  );
}
