"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  TrendingUp,
  RefreshCw,
  Calculator,
  ShieldCheck,
  ExternalLink,
  Copy,
  Check,
  Plus,
  Trash2,
  Award,
  Search,
  ArrowUpDown,
  Calendar,
  ChevronRight,
  X,
} from "lucide-react";

const API_BASE = "http://127.0.0.1:8000";

interface SubscriptionSplit {
  retail: number | null;
  nii: number | null;
  qib: number | null;
  total: number | null;
}

interface IPOCardData {
  id: number;
  company_name: string;
  status: string;
  section: "open" | "upcoming" | "closed" | "listed";
  category: string;
  price_band_high: number;
  lot_size: number;
  open_date: string | null;
  close_date: string | null;
  listing_date: string | null;
  listing_price: number | null;
  listing_gain_pct: number | null;
  registrar_name: string;
  latest_gmp: number;
  estimated_listing_price: number;
  expected_gain_pct: number;
  subscription: SubscriptionSplit;
  allotment_probability_pct: number | null;
  probability_verdict: string;
}

interface DeepAnalytics {
  ipo_name: string;
  issue_price: number;
  lot_size: number;
  total_investment: number;
  retail_subscription_times: number | null;
  allotment_probability_pct: number | null;
  probability_verdict: string;
  gmp_price: number;
  estimated_listing_price: number;
  gross_profit_per_lot: number;
  estimated_taxes_and_charges: number;
  net_profit_per_lot: number;
  net_roi_pct: number;
  breakeven_selling_price: number;
}

interface SyncApiResponse {
  status: "ok" | "cached" | "syncing";
  last_synced_at: string | null;
  synced?: number;
}

interface SyncStatusResponse extends SyncApiResponse {
  message?: string | null;
}

type SortOption = "default" | "gmp_desc" | "sub_desc" | "odds_desc" | "capital_asc";

const formatSubscription = (value: number | null) =>
  value === null ? "—" : `${value.toFixed(2)}x`;

const formatPrettyDate = (dt: string | null) => {
  if (!dt || dt.startsWith("2050")) return "TBA";
  const parsed = new Date(dt);
  if (isNaN(parsed.getTime())) return dt;
  return parsed.toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
  });
};

interface SavedPAN {
  label: string;
  pan: string;
}

const REGISTRAR_URLS: Record<string, string> = {
  "Link Intime (MUFG)": "https://in.mpms.mufg.com/Initial_Offer/public-issues.html",
  KFintech: "https://kosmic.kfintech.com/ipostatus/",
  Bigshare: "https://ipo.bigshareonline.com/IPO_Status.html",
};

const formatRelativeSyncTime = (value: string | null, now: number | null) => {
  if (!value || now === null) return "not yet";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "not yet";
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hr${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
};

export default function IPOTrackDashboard() {
  const [section, setSection] = useState<"open" | "upcoming" | "closed" | "listed">("open");
  const [ipos, setIpos] = useState<IPOCardData[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState<number | null>(null);

  // Search & Sort Controls
  const [searchQuery, setSearchQuery] = useState("");
  const [sortBy, setSortBy] = useState<SortOption>("default");

  // Analytics Modal State
  const [selectedIpo, setSelectedIpo] = useState<IPOCardData | null>(null);
  const [lotsCount, setLotsCount] = useState<number>(1);
  const [analytics, setAnalytics] = useState<DeepAnalytics | null>(null);

  // Multi-PAN Local Vault State
  const [pans, setPans] = useState<SavedPAN[]>([]);
  const [newLabel, setNewLabel] = useState("");
  const [newPan, setNewPan] = useState("");
  const [copiedPan, setCopiedPan] = useState<string | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const chartFrameRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    const saved = localStorage.getItem("ipotrack_pan_vault");
    if (saved) {
      try {
        setPans(JSON.parse(saved));
      } catch {}
    }
  }, []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && selectedIpo) {
        setSelectedIpo(null);
        return;
      }
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const target = e.target;
        const isTyping = target instanceof HTMLElement && (
          target.isContentEditable
          || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)
        );
        if (!isTyping) {
          e.preventDefault();
          searchInputRef.current?.focus();
        }
      }
    };
    const handleChartShortcut = (event: MessageEvent) => {
      if (
        event.source !== chartFrameRef.current?.contentWindow
        || typeof event.data !== "object"
        || event.data === null
        || event.data.type !== "ipo-track-key"
      ) return;
      if (event.data.key === "Escape" && selectedIpo) {
        setSelectedIpo(null);
      } else if (event.data.key === "/") {
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("message", handleChartShortcut);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("message", handleChartShortcut);
    };
  }, [selectedIpo]);

  useEffect(() => {
    const updateCurrentTime = () => setCurrentTime(Date.now());
    updateCurrentTime();
    const interval = window.setInterval(updateCurrentTime, 60_000);
    return () => window.clearInterval(interval);
  }, []);

  const savePanList = (updated: SavedPAN[]) => {
    setPans(updated);
    localStorage.setItem("ipotrack_pan_vault", JSON.stringify(updated));
  };

  const handleAddPan = (e: React.FormEvent) => {
    e.preventDefault();
    const formattedPan = newPan.trim().toUpperCase();
    if (!newLabel.trim() || !/^[A-Z]{5}[0-9]{4}[A-Z]{1}$/.test(formattedPan)) {
      alert("Please enter a valid label and 10-character Indian PAN (e.g., ABCDE1234F).");
      return;
    }
    savePanList([...pans, { label: newLabel.trim(), pan: formattedPan }]);
    setNewLabel("");
    setNewPan("");
  };

  const sectionRef = useRef(section);
  const initialSyncPromise = useRef<Promise<void> | null>(null);
  const initialSyncApplied = useRef(false);
  const latestIpoFetch = useRef(0);
  const hasLoadedIpos = useRef(false);

  useEffect(() => {
    sectionRef.current = section;
  }, [section]);

  const fetchIpos = useCallback(async (activeSection: string) => {
    const requestId = ++latestIpoFetch.current;
    if (!hasLoadedIpos.current) setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/ipos?section=${activeSection}`);
      if (!res.ok) {
        setSyncNotice("Could not load saved IPO data from the local API.");
        console.warn(`IPO list request failed (${res.status}).`);
        return;
      }
      const data = await res.json();
      if (!Array.isArray(data)) {
        setSyncNotice("The local API returned an unexpected IPO data response.");
        return;
      }
      if (sectionRef.current === activeSection && requestId === latestIpoFetch.current) {
        setIpos(data);
        hasLoadedIpos.current = true;
      }
    } catch (err) {
      console.warn("Failed to fetch IPOs; keeping the currently loaded data:", err);
      setSyncNotice("Could not reach the local API; keeping the currently loaded data.");
    } finally {
      if (sectionRef.current === activeSection && requestId === latestIpoFetch.current) {
        setLoading(false);
      }
    }
  }, []);

  const refreshSyncStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/sync/status`);
      if (!res.ok) {
        setSyncNotice("Saved sync status is currently unavailable.");
        return;
      }
      const data = await res.json() as SyncStatusResponse;
      setLastSyncedAt(data.last_synced_at ?? null);
      if (data.status === "cached" && data.message) {
        setSyncNotice(data.message);
      }
    } catch (err) {
      console.warn("Could not load saved sync status:", err);
      setSyncNotice("Sync status unavailable; saved IPO data will still be shown.");
    }
  }, []);

  const refreshIpoData = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/sync`, { method: "POST" });
      const data = await res.json().catch(() => null) as SyncApiResponse | null;
      if (!res.ok || data?.status === "cached") {
        console.warn(`IPOJi refresh unavailable (${res.status}); using cached IPO data.`);
        if (data?.last_synced_at) setLastSyncedAt(data.last_synced_at);
        setSyncNotice("Live refresh unavailable; showing the last saved IPO data.");
        return;
      }
      setLastSyncedAt(data?.last_synced_at ?? null);
      setSyncNotice(null);
    } catch (err) {
      console.warn("IPOJi refresh unavailable; using locally saved IPO data.", err);
      setSyncNotice("Live refresh unavailable; showing the last saved IPO data.");
      await refreshSyncStatus();
    }
  }, [refreshSyncStatus]);

  useEffect(() => {
    let active = true;
    const loadSection = async () => {
      // Render the last saved snapshot immediately; refresh IPOJi in parallel.
      void fetchIpos(section);
      await refreshSyncStatus();
      if (!initialSyncPromise.current) {
        setSyncing(true);
        initialSyncPromise.current = refreshIpoData()
          .finally(() => setSyncing(false));
      }
      await initialSyncPromise.current;
      if (active && !initialSyncApplied.current) {
        initialSyncApplied.current = true;
        await fetchIpos(section);
      }
    };
    void loadSection();
    return () => {
      active = false;
    };
  }, [section, fetchIpos, refreshIpoData, refreshSyncStatus]);

  useEffect(() => {
    if (!selectedIpo) return;
    fetch(`${API_BASE}/api/ipos/${selectedIpo.id}/analytics?lots=${lotsCount}`)
      .then((res) => res.json())
      .then((data) => setAnalytics(data))
      .catch((err) => console.error(err));
  }, [selectedIpo, lotsCount]);

  const handleManualSync = async () => {
    setSyncing(true);
    try {
      await refreshIpoData();
    } catch (err) {
      console.warn("IPOJi refresh failed; loading saved IPO data:", err);
      setSyncNotice("Live refresh unavailable; showing the last saved IPO data.");
    } finally {
      setSyncing(false);
    }
    await fetchIpos(section);
  };

  const copyPan = (pan: string) => {
    navigator.clipboard.writeText(pan);
    setCopiedPan(pan);
    setTimeout(() => setCopiedPan(null), 2000);
  };

  const displayedIpos = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const filtered = ipos.filter((ipo) =>
      ipo.company_name.toLowerCase().includes(query)
    );

    return [...filtered].sort((a, b) => {
      if (sortBy === "gmp_desc") {
        return (b.expected_gain_pct || 0) - (a.expected_gain_pct || 0);
      }
      if (sortBy === "sub_desc") {
        return (b.subscription.total ?? -1) - (a.subscription.total ?? -1);
      }
      if (sortBy === "odds_desc") {
        return (b.allotment_probability_pct ?? -1) - (a.allotment_probability_pct ?? -1);
      }
      if (sortBy === "capital_asc") {
        const capA = a.price_band_high * a.lot_size || Number.MAX_SAFE_INTEGER;
        const capB = b.price_band_high * b.lot_size || Number.MAX_SAFE_INTEGER;
        return capA - capB;
      }
      return 0;
    });
  }, [ipos, searchQuery, sortBy]);

  const pulseMetrics = useMemo(() => {
    const topGmpIpo = [...ipos].sort(
      (a, b) => (b.expected_gain_pct || 0) - (a.expected_gain_pct || 0)
    )[0];

    const topSubIpo = [...ipos].sort(
      (a, b) => (b.subscription.total ?? 0) - (a.subscription.total ?? 0)
    )[0];

    return {
      totalCount: ipos.length,
      topGmpIpo,
      topSubIpo,
    };
  }, [ipos]);

  const selectedNiiSubscription = selectedIpo?.subscription.nii ?? null;
  const modalAllotmentOdds = lotsCount === 14
    ? selectedNiiSubscription !== null && selectedNiiSubscription > 0
      ? Math.min(100, 100 / selectedNiiSubscription)
      : null
    : analytics?.allotment_probability_pct ?? null;
  const modalAllotmentOddsLabel = lotsCount === 14
    ? "sNII Allotment Odds"
    : "Retail Allotment Odds";

  return (
    <div className="terminal-dashboard min-h-screen bg-[#000000] text-white font-sans selection:bg-[#00D09C]/20 selection:text-white relative overflow-x-hidden">
      <header className="border-b border-[#202227] bg-[#000000] sticky top-0 z-30">
        <div className="max-w-7xl mx-auto px-6 py-4 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3.5">
            <div className="w-9 h-9 rounded-md bg-[#0A0B0D] border border-[#202227] flex items-center justify-center text-[#D1D5DB]">
              <TrendingUp className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-lg font-semibold tracking-tight text-white">
                  IPOTrack
                </h1>
                <span className="hidden sm:inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-sm text-[11px] font-medium bg-white/[0.04] text-[#C4C7CE] border border-white/[0.08]">
                  <span className="w-1.5 h-1.5 rounded-sm bg-white/[0.25]" />
                  NSE &amp; BSE Mainboard
                </span>
              </div>
              <p className="text-xs text-[#8B909A] mt-0.5">
                  Mainboard IPOs · GMP · Subscription · Listing data
              </p>
            </div>
          </div>

          <div className="flex min-w-0 flex-col items-end gap-1">
            <div className="flex flex-wrap items-center justify-end gap-3">
              <span className="text-[11px] text-[#8B909A]" aria-live="polite">
                Last synced: {formatRelativeSyncTime(lastSyncedAt, currentTime)}
              </span>
              <button
                onClick={handleManualSync}
                disabled={syncing}
                className="group flex items-center gap-2 px-3.5 py-2 rounded-md bg-white hover:bg-[#E5E5E5] border border-white/20 text-xs font-medium text-[#000000] transition-colors disabled:opacity-50 cursor-pointer"
              >
                <RefreshCw
                  className={`w-3.5 h-3.5 text-[#000000] transition-transform duration-500 ${
                    syncing ? "animate-spin" : "group-hover:rotate-180"
                  }`}
                />
                {syncing ? "Refreshing IPO data..." : "Sync Market Data"}
              </button>
            </div>
            {syncNotice && (
              <span role="status" className="max-w-full text-right text-[10px] text-amber-300/80">
                {syncNotice}
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="relative z-10 max-w-7xl mx-auto px-6 py-6 space-y-6">
        <section className="market-ticker" aria-label="Mainboard market snapshot">
          <div className="market-ticker-item">
            <span className="market-ticker-label">MAINBOARD PIPELINE</span>
            <span className="market-ticker-value">
              <strong className="tabular-nums font-mono tracking-tight">{pulseMetrics.totalCount}</strong>
              <span className="text-[#8B909A]">Active</span>
            </span>
          </div>

          <div className="market-ticker-item">
            <span className="market-ticker-label">TOP GMP</span>
            {pulseMetrics.topGmpIpo ? (
              <span className={pulseMetrics.topGmpIpo.latest_gmp < 0
                ? "market-ticker-value tabular-nums font-mono tracking-tight text-[#FF5353]"
                : "market-ticker-value tabular-nums font-mono tracking-tight text-groww-teal"}>
                <span className="max-w-[180px] truncate font-sans font-medium text-[#D1D5DB]">
                  {pulseMetrics.topGmpIpo.company_name}
                </span>
                <span>
                  {pulseMetrics.topGmpIpo.latest_gmp > 0 ? "+" : pulseMetrics.topGmpIpo.latest_gmp < 0 ? "-" : ""}{"\u20B9"}{Math.abs(pulseMetrics.topGmpIpo.latest_gmp)}
                  {" "}({pulseMetrics.topGmpIpo.expected_gain_pct > 0 ? "+" : ""}{pulseMetrics.topGmpIpo.expected_gain_pct}%)
                </span>
              </span>
            ) : (
              <span className="market-ticker-value text-[#8B909A]">No active GMP</span>
            )}
          </div>

          <div className="market-ticker-item">
            <span className="market-ticker-label">PEAK SUBSCRIPTION</span>
            {pulseMetrics.topSubIpo && pulseMetrics.topSubIpo.subscription.total !== null ? (
              <span className="market-ticker-value tabular-nums font-mono tracking-tight">
                <span className="max-w-[180px] truncate font-sans font-medium text-[#D1D5DB]">
                  {pulseMetrics.topSubIpo.company_name}
                </span>
                <span>{pulseMetrics.topSubIpo.subscription.total.toFixed(2)}x</span>
              </span>
            ) : (
              <span className="market-ticker-value text-[#8B909A]">Awaiting bids</span>
            )}
          </div>
        </section>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Left 2 Columns: IPO Pipeline & Cards */}
          <div className="lg:col-span-2 space-y-6">
            {/* Segmented Filter Bar + Search + Sort */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="section-tabs" role="tablist" aria-label="IPO lifecycle">
                {(["open", "upcoming", "closed", "listed"] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setSection(tab)}
                    role="tab"
                    aria-selected={section === tab}
                    className={`section-tab ${section === tab ? "section-tab-active" : ""}`}
                  >
                    {tab.charAt(0).toUpperCase() + tab.slice(1)}
                  </button>
                ))}
              </div>

              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2.5 sm:max-w-md">
                <div className="relative min-w-[240px] flex-1">
                  <Search className="w-3.5 h-3.5 text-[#8B909A] absolute left-3.5 top-1/2 -translate-y-1/2" />
                  <input
                    ref={searchInputRef}
                    type="text"
                    placeholder="Search mainboard IPOs... (Press /)"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="w-full pl-9 pr-8 py-2.5 rounded-lg bg-[#0A0B0D] border border-white/[0.08] text-xs text-white placeholder:text-[#8B909A] focus:outline-none focus:border-white/20 transition"
                  />
                  {searchQuery && (
                    <button
                      onClick={() => setSearchQuery("")}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#8B909A] hover:text-white"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>

                <div className="relative">
                  <ArrowUpDown className="w-3.5 h-3.5 text-[#8B909A] absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
                  <select
                    value={sortBy}
                    onChange={(e) => setSortBy(e.target.value as SortOption)}
                    className="pl-8 pr-4 py-2.5 rounded-lg bg-[#0A0B0D] border border-white/[0.08] text-xs text-[#D1D5DB] focus:outline-none focus:border-white/20 cursor-pointer"
                  >
                    <option value="default">Sort: Default</option>
                    <option value="gmp_desc">Highest GMP %</option>
                    <option value="sub_desc">Most Subscribed</option>
                    <option value="odds_desc">Best Retail Odds</option>
                    <option value="capital_asc">Lowest Lot Capital</option>
                  </select>
                </div>
              </div>
            </div>

            {/* IPO Cards Grid */}
            {loading ? (
              <div className="p-8 text-center text-[#8B909A] border border-white/[0.06] rounded-lg bg-[#0A0B0D]/60 text-sm">
                Loading {section} Mainboard issues...
              </div>
            ) : displayedIpos.length === 0 ? (
              <div className="p-8 text-center text-[#8B909A] border border-white/[0.06] rounded-lg bg-[#0A0B0D]/60 text-sm">
                {searchQuery ? (
                  <>
                    No Mainboard IPOs matching{" "}
                    <span className="text-white font-semibold">&ldquo;{searchQuery}&rdquo;</span> in{" "}
                    {section}.
                  </>
                ) : (
                  <>
                    No Mainboard IPOs currently in the{" "}
                    <span className="text-white font-semibold capitalize">{section}</span> stage.
                  </>
                )}
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {displayedIpos.map((ipo) => {
                  const oneLotCapital = ipo.price_band_high * ipo.lot_size;
                  const companyInitials = ipo.company_name
                    .trim()
                    .split(/\s+/)
                    .filter(Boolean)
                    .slice(0, 2)
                    .map((part) => part[0])
                    .join("")
                    .toUpperCase();
                  const statusTone = "border-[#202227] text-[#C4C7CE]";
                  return (
                    <div
                      key={ipo.id}
                      className="ipo-card group relative flex flex-col justify-between space-y-4 rounded-xl border border-white/[0.06] bg-[#121316] p-4 transition-all hover:border-[#202227] hover:bg-[#0A0B0D]"
                    >
                      <div>
                        {/* Card Header */}
                        <div className="flex items-center justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-white/[0.06] bg-white/[0.04] text-xs font-semibold text-[#D1D5DB]">
                              {companyInitials}
                            </div>
                            <h2 className="min-w-0 truncate text-base font-semibold leading-snug text-white">
                              {ipo.company_name}
                            </h2>
                          </div>
                          <span className={`px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider rounded border bg-[#121316] shrink-0 ${statusTone}`}>
                            {ipo.status}
                          </span>
                        </div>

                        {/* Date & Lot Pill Row */}
                        <div className="flex items-center justify-between text-xs text-[#8B909A] mt-2">
                          <span className="inline-flex items-center gap-1.5">
                            <Calendar className="w-3.5 h-3.5 text-[#8B909A]" />
                            {formatPrettyDate(ipo.open_date)} – {formatPrettyDate(ipo.close_date)}
                          </span>
                          {ipo.lot_size > 0 && (
                            <span className="font-mono text-[11px] text-[#C4C7CE] bg-white/[0.04] border border-white/[0.06] px-2 py-0.5 rounded-md tabular-nums">
                              {ipo.lot_size} Shares / Lot
                            </span>
                          )}
                        </div>

                        {/* Core Pricing & GMP Box */}
                        <div className="mt-4 grid grid-cols-3 gap-4 border-y border-white/[0.06] py-3.5">
                          <div>
                            <span className="text-[10px] font-medium uppercase tracking-wider text-[#8B909A] block">
                              Offer Price
                            </span>
                            <span className="text-sm font-semibold text-white mt-1 block tabular-nums font-mono tracking-tight">
                              {ipo.price_band_high > 0 ? `₹${ipo.price_band_high}` : "TBA"}
                            </span>
                            <span className="text-[10px] text-[#8B909A] block mt-0.5">
                              Upper Band
                            </span>
                          </div>

                          <div>
                            <span className="text-[10px] font-medium uppercase tracking-wider text-[#8B909A] block">
                              1 Lot Capital
                            </span>
                            <span className="text-sm font-semibold text-[#D1D5DB] mt-1 block tabular-nums font-mono tracking-tight">
                              {oneLotCapital > 0
                                ? `₹${oneLotCapital.toLocaleString("en-IN")}`
                                : "TBA"}
                            </span>
                            <span className="text-[10px] text-[#8B909A] block mt-0.5">
                              Min Retail
                            </span>
                          </div>

                          <div>
                            <span className="text-[10px] font-medium uppercase tracking-wider text-[#8B909A] block">
                              Grey Market
                            </span>
                            <span
                              className={`text-sm font-semibold mt-1 block tabular-nums font-mono tracking-tight ${
                                ipo.latest_gmp > 0
                                  ? "text-groww-teal"
                                  : ipo.latest_gmp < 0
                                  ? "text-[#FF5353]"
                                  : "text-[#C4C7CE]"
                              }`}
                            >
                              {ipo.latest_gmp > 0 ? "+" : ipo.latest_gmp < 0 ? "-" : ""}{"\u20B9"}{Math.abs(ipo.latest_gmp)} ({ipo.expected_gain_pct > 0 ? "+" : ""}{ipo.expected_gain_pct}%)

                            </span>
                            <span className="text-[10px] text-[#8B909A] mt-0.5 block tabular-nums font-mono tracking-tight">
                              Est. {"\u20B9"}{ipo.estimated_listing_price}
                            </span>
                          </div>
                        </div>

                        {/* Subscription Matrix */}
                        <div className="mt-3 grid grid-cols-4 gap-2.5 border-t border-white/[0.06] pt-3">
                          {(
                            [
                              ["QIB", ipo.subscription.qib, "text-[#C4C7CE]", "bg-white/[0.25]"],
                              ["NII", ipo.subscription.nii, "text-[#C4C7CE]", "bg-white/[0.25]"],
                              ["Retail", ipo.subscription.retail, "text-[#D1D5DB]", "bg-white/[0.4]"],
                              ["Total", ipo.subscription.total, "text-white", "bg-white/[0.25]"],
                            ] as const
                          ).map(([label, value, textColor, barColor]) => {
                            const fillPct =
                              value === null || value <= 0
                                ? 0
                                : Math.min(100, Math.round(Math.log10(value + 1) * 70));
                            return (
                              <div key={label} className="space-y-1.5">
                                <span className="text-[10px] font-medium uppercase tracking-wider text-[#8B909A] block">
                                  {label}
                                </span>
                                <span className={`text-xs font-semibold block tabular-nums font-mono tracking-tight ${textColor}`}>
                                  {formatSubscription(value)}
                                </span>
                                <div className="h-[2.5px] w-full overflow-hidden rounded-sm bg-white/[0.06]">
                                  <div
                                    className={`h-full rounded-sm ${barColor}`}
                                    style={{ width: `${fillPct}%` }}
                                  />
                                </div>
                              </div>
                            );
                          })}
                        </div>

                        {/* Listed Outcome OR Retail Lottery Verdict */}
                        {ipo.section === "listed" ? (
                          <div className="mt-3 px-3.5 py-2.5 rounded-md bg-white/[0.03] border border-white/[0.06] text-xs flex justify-between items-center">
                            <span className="text-[#C4C7CE]">
                              Listed Price:{" "}
                              <strong className="text-white tabular-nums font-mono tracking-tight">
                                ₹{ipo.listing_price ?? "N/A"}
                              </strong>
                            </span>
                            <span
                              className={`font-semibold tabular-nums font-mono tracking-tight ${
                                (ipo.listing_gain_pct ?? 0) >= 0
                                  ? "text-groww-teal"
                                  : "text-[#FF5353]"
                              }`}
                            >
                              {ipo.listing_gain_pct !== null
                                ? `${ipo.listing_gain_pct >= 0 ? "+" : ""}${ipo.listing_gain_pct}% Debut`
                                : "—"}
                            </span>
                          </div>
                        ) : (
                          <div className="mt-3 px-3.5 py-2.5 rounded-md bg-white/[0.02] border border-white/[0.05] text-xs flex items-center justify-between gap-2">
                            <span className="text-[#C4C7CE] flex items-center gap-1.5 shrink-0 tabular-nums">
                              <Award className="w-3.5 h-3.5 text-[#D1D5DB]" />
                              Retail Odds:{" "}
                              <strong className="text-white tabular-nums font-mono tracking-tight">
                                {ipo.allotment_probability_pct === null
                                  ? "N/A"
                                  : `${ipo.allotment_probability_pct}%`}
                              </strong>
                            </span>
                            <span className="text-[#8B909A] truncate text-right text-[11px]">
                              {ipo.probability_verdict}
                            </span>
                          </div>
                        )}
                      </div>

                      {/* Primary Card CTA */}
                      <button
                        onClick={() => {
                          setLotsCount(1);
                          setSelectedIpo(ipo);
                        }}
                        className="w-full border-t border-white/[0.08] px-1 py-2.5 text-xs font-medium text-[#C4C7CE] transition-colors hover:border-white/20 hover:text-white cursor-pointer flex items-center justify-between group/btn"
                      >
                        <span className="flex items-center gap-2">
                          <Calculator className="w-3.5 h-3.5 text-[#C4C7CE]" />
                          Net Break-Even, Taxes &amp; GMP Chart
                        </span>
                        <ChevronRight className="w-3.5 h-3.5 text-[#8B909A] group-hover/btn:text-white group-hover/btn:translate-x-0.5 transition-transform" />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Right Column: Multi-PAN Privacy Vault & Registrar Router */}
          <div className="space-y-6">
            <div className="p-5 rounded-xl bg-[#121316] border border-white/[0.06] space-y-5 sticky top-24">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <div className="p-2 rounded-lg bg-white/[0.04] border border-[#202227] text-[#C4C7CE]">
                    <ShieldCheck className="w-4 h-4" />
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-white">
                      Multi-PAN Allotment Vault
                    </h3>
                    <span className="text-[11px] text-[#8B909A] block">
                      Client-side browser storage only
                    </span>
                  </div>
                </div>
              </div>

              <p className="text-xs text-[#8B909A] leading-relaxed">
                Save family Demat PANs locally in your browser. Copy any PAN in one click and open the official registrar portal on allotment day.
              </p>

              {/* Add PAN Form */}
              <form onSubmit={handleAddPan} className="space-y-2.5">
                <input
                  type="text"
                  placeholder="Account Holder (e.g., Vedant / Mom)"
                  value={newLabel}
                  onChange={(e) => setNewLabel(e.target.value)}
                    className="w-full px-3.5 py-2 rounded-md bg-[#121316] border border-white/[0.08] text-xs text-white placeholder:text-[#8B909A] focus:outline-none focus:border-white/20"
                />
                <div className="flex gap-2">
                  <input
                    type="text"
                    maxLength={10}
                    placeholder="PAN (ABCDE1234F)"
                    value={newPan}
                    onChange={(e) => setNewPan(e.target.value.toUpperCase())}
                    className="flex-1 px-3.5 py-2 rounded-md bg-[#121316] border border-white/[0.08] text-xs font-mono uppercase text-white placeholder:text-[#8B909A] focus:outline-none focus:border-white/20"
                  />
                  <button
                    type="submit"
                    className="px-4 py-2 rounded-md bg-white hover:bg-[#E5E5E5] text-black font-semibold text-xs flex items-center gap-1 transition cursor-pointer shrink-0"
                  >
                    <Plus className="w-3.5 h-3.5" /> Save
                  </button>
                </div>
              </form>

              {/* Saved PAN List */}
              <div className="space-y-2">
                {pans.length === 0 ? (
                  <div className="p-4 rounded-md bg-[#121316]/60 border border-white/[0.04] text-center">
                    <p className="text-xs text-[#8B909A]">
                      No PANs saved in local vault yet.
                    </p>
                  </div>
                ) : (
                  pans.map((item, idx) => (
                    <div
                      key={idx}
                      className="flex items-center justify-between p-3 rounded-md bg-[#121316] border border-white/[0.06] text-xs"
                    >
                      <div>
                        <span className="font-medium text-[#D1D5DB] block">
                          {item.label}
                        </span>
                        <span className="font-mono text-[11px] text-[#8B909A] tracking-wider">
                          {item.pan}
                        </span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <button
                          onClick={() => copyPan(item.pan)}
                          className="p-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-[#C4C7CE] cursor-pointer transition"
                          title="Copy PAN"
                        >
                          {copiedPan === item.pan ? (
                            <Check className="w-3.5 h-3.5 text-[#D1D5DB]" />
                          ) : (
                            <Copy className="w-3.5 h-3.5" />
                          )}
                        </button>
                        <button
                          onClick={() => savePanList(pans.filter((_, i) => i !== idx))}
                          className="p-1.5 rounded-lg bg-white/[0.04] hover:bg-[#FF5353]/10 text-[#8B909A] hover:text-[#FF5353] cursor-pointer transition"
                          title="Delete PAN"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </div>

              {/* Direct Registrar Portals */}
              <div className="pt-4 border-t border-white/[0.06] space-y-2">
                <span className="text-[10px] font-medium uppercase tracking-wider text-[#8B909A] block">
                  Official Registrar Portals
                </span>
                {Object.entries(REGISTRAR_URLS).map(([name, url]) => (
                  <a
                    key={name}
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between p-2.5 rounded-md bg-[#121316]/70 hover:bg-white/[0.04] border border-white/[0.05] text-xs text-[#C4C7CE] hover:text-white transition"
                  >
                    <span>{name}</span>
                    <ExternalLink className="w-3.5 h-3.5 text-[#8B909A]" />
                  </a>
                ))}
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Analytics and GMP history */}
      {selectedIpo && analytics && (
        <div
          onClick={() => setSelectedIpo(null)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-3 backdrop-blur-md sm:p-5"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="analytics-title"
            onClick={(event) => event.stopPropagation()}
            className="max-h-[92dvh] w-full max-w-6xl space-y-4 overflow-hidden rounded-xl border border-white/[0.06] bg-[#121316] p-3 sm:space-y-5 sm:p-5"
          >
            <header className="flex items-start justify-between gap-4 border-b border-[#202227] pb-4">
              <div className="min-w-0">
                <h2 id="analytics-title" className="truncate text-lg font-semibold text-white">
                  {analytics.ipo_name}
                </h2>
                <p className="mt-1 text-xs text-[#8B909A]">
                  Listing scenario and saved grey market observations
                </p>
              </div>
              <button
                type="button"
                onClick={() => setSelectedIpo(null)}
                aria-label="Close analytics"
                className="shrink-0 rounded-md border border-[#202227] bg-[#121316] p-2 text-[#8B909A] transition hover:text-white"
              >
                <X className="h-4 w-4" />
              </button>
            </header>

            <div className="grid items-stretch gap-5 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
              <section className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-[#202227] bg-[#121316] p-3.5">
                  <div>
                    <h3 className="text-xs font-semibold text-[#D1D5DB]">Lot simulator</h3>
                    <p className="mt-1 text-[11px] text-[#8B909A]">
                      <span className="tabular-nums font-mono tracking-tight">{selectedIpo.lot_size}</span> shares per lot
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {[1, 2, 5, 14].map((n) => (
                      <button
                        key={n}
                        type="button"
                        onClick={() => setLotsCount(n)}
                        className={lotsCount === n
                          ? "rounded-md border border-transparent bg-[#00D09C] px-2.5 py-1.5 text-[11px] font-semibold text-[#002319] transition"
                          : "rounded-md border border-[#202227] bg-[#0A0B0D] px-2.5 py-1.5 text-[11px] font-medium text-[#C4C7CE] transition hover:border-white/[0.2] hover:text-white"}
                      >
                        <span className="tabular-nums font-mono tracking-tight">{n}</span>{" "}
                        {n === 1 ? "Lot" : n === 14 ? "Lots (sNII)" : "Lots"}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded-md border border-[#202227] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-[#8B909A]">Capital blocked</span>
                    <span className="tabular-nums font-mono tracking-tight mt-1 block text-sm font-semibold text-white">
                      {"\u20B9"}{analytics.total_investment.toLocaleString("en-IN")}
                    </span>
                  </div>
                  <div className="rounded-md border border-[#202227] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-[#8B909A]">Gross listing gain</span>
                    <span className="tabular-nums mt-1 block text-lg font-semibold text-groww-teal">
                      {"\u20B9"}{analytics.gross_profit_per_lot.toLocaleString("en-IN")}
                    </span>
                  </div>
                  <div className="rounded-md border border-[#202227] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-[#8B909A]">Estimated charges</span>
                    <span className="tabular-nums font-mono tracking-tight mt-1 block text-sm font-semibold text-[#FF5353]">
                      -{"\u20B9"}{analytics.estimated_taxes_and_charges}
                    </span>
                  </div>
                  <div className="rounded-md border border-white/[0.06] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-groww-teal">Net take-home profit</span>
                    <span className="tabular-nums mt-1 block text-lg font-semibold text-groww-teal">
                      {"\u20B9"}{analytics.net_profit_per_lot.toLocaleString("en-IN")}
                    </span>
                    <span className="tabular-nums font-mono tracking-tight mt-0.5 block text-[10px] text-groww-teal">
                      {analytics.net_roi_pct}% return
                    </span>
                  </div>
                  <div className="rounded-md border border-[#202227] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-[#8B909A]">Break-even price</span>
                    <span className="tabular-nums font-mono tracking-tight mt-1 block text-sm font-semibold text-amber-400">
                      {"\u20B9"}{analytics.breakeven_selling_price}
                    </span>
                  </div>
                  <div className="rounded-md border border-[#202227] bg-[#121316] p-3.5">
                    <span className="block text-[10px] uppercase tracking-wider text-[#8B909A]">
                      {modalAllotmentOddsLabel}
                    </span>
                    <span className="tabular-nums mt-1 block text-sm font-semibold text-white">
                      {modalAllotmentOdds === null ? "N/A" : `${modalAllotmentOdds.toFixed(2)}%`}
                    </span>
                    <span className="mt-0.5 block truncate text-[10px] text-[#8B909A]">
                      {lotsCount === 14
                        ? selectedNiiSubscription === null || selectedNiiSubscription <= 0
                          ? "NII subscription data unavailable"
                          : `Based on ${selectedNiiSubscription}x NII subscription`
                        : analytics.probability_verdict}
                    </span>
                  </div>
                </div>
              </section>

              <section className="min-w-0 overflow-hidden rounded-xl bg-[#121316]">
                <iframe
                  ref={chartFrameRef}
                  src={API_BASE + "/api/ipos/" + selectedIpo.id + "/chart"}
                  className="block h-[clamp(160px,28vh,250px)] w-full border-0 bg-[#121316] md:h-[min(68vh,620px)]"
                  title={selectedIpo.company_name + " GMP observation history"}
                />
              </section>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
