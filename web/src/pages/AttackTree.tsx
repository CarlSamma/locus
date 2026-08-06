import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Probe } from "../api";
import { StatusChip } from "../components/Layout";
import { EmptyState, Spinner } from "../components/ui";

function ScoreBadge({ score }: { score: number }) {
  const color = score >= 7 ? "text-tertiary" : score >= 4 ? "text-warning" : "text-textSecondary";
  return <span className={`font-mono text-xs font-bold ${color}`}>{score}</span>;
}

export default function AttackTree() {
  const [statusFilter, setStatusFilter] = useState("");
  const { data, isLoading, isError } = useQuery({
    queryKey: ["probes", statusFilter],
    queryFn: () => api.probes({ status: statusFilter || undefined, limit: 100 }),
  });

  if (isLoading) return <Spinner label="Caricamento attack tree…" />;
  if (isError || !data) return <EmptyState message="Errore nel caricamento dei probe" />;

  const items = data.items ?? [];
  const filters = ["", "posted", "replied", "classified"];

  return (
    <div className="max-w-7xl mx-auto flex flex-col gap-6">
      <div className="flex items-center gap-2">
        {filters.map((f) => (
          <button
            key={f}
            onClick={() => setStatusFilter(f)}
            className={`px-3 py-1.5 rounded-full font-mono text-[10px] uppercase tracking-wider border transition-colors ${
              statusFilter === f
                ? "bg-primary/20 text-primary border-primary/40"
                : "bg-white/5 text-textSecondary border-white/10 hover:text-primary"
            }`}
          >
            {f || "all"}
          </button>
        ))}
        <span className="ml-auto font-mono text-xs text-textSecondary">{data.total} probe</span>
      </div>

      <div className="bg-surface rounded-lg border border-white/5 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="bg-input text-xs font-label text-textSecondary uppercase tracking-wider border-b border-white/5">
                <th className="px-4 py-3 font-medium">ID</th>
                <th className="px-4 py-3 font-medium">Property</th>
                <th className="px-4 py-3 font-medium">Frame</th>
                <th className="px-4 py-3 font-medium">Probe</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium text-right">Score</th>
                <th className="px-4 py-3 font-medium">Classification</th>
              </tr>
            </thead>
            <tbody className="font-mono text-sm divide-y divide-white/5">
              {items.map((p: Probe, i) => (
                <ProbeRow key={p.id} probe={p} zebra={i % 2 === 1} />
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-textSecondary">
                    Nessun probe — avvia una sessione dal Probe Lab
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function ProbeRow({ probe, zebra }: { probe: Probe; zebra: boolean }) {
  const [open, setOpen] = useState(false);
  let cls: Record<string, unknown> | null = null;
  try {
    if (probe.classification) cls = JSON.parse(probe.classification);
  } catch {
    cls = null;
  }
  return (
    <>
      <tr
        onClick={() => setOpen((o) => !o)}
        className={`cursor-pointer hover:bg-white/[0.02] transition-colors ${zebra ? "bg-input/30" : ""}`}
      >
        <td className="px-4 py-3 text-textSecondary">#{probe.id}</td>
        <td className="px-4 py-3">
          <span className="font-mono text-[10px] text-primary px-1.5 py-0.5 bg-primary/10 rounded">
            [{probe.property_key}]
          </span>
        </td>
        <td className="px-4 py-3 text-textSecondary">{probe.frame}</td>
        <td className="px-4 py-3 text-textPrimary/80 max-w-[340px] truncate">“{probe.text}”</td>
        <td className="px-4 py-3">
          <StatusChip state={probe.status} />
        </td>
        <td className="px-4 py-3 text-right">
          <ScoreBadge score={probe.score} />
        </td>
        <td className="px-4 py-3 text-textSecondary">
          {cls && typeof cls.pattern === "string" ? cls.pattern : "—"}
        </td>
      </tr>
      {open && (
        <tr className={`${zebra ? "bg-input/30" : ""} border-t border-white/5`}>
          <td colSpan={7} className="px-4 py-4">
            <div className="bg-console rounded-lg p-4 space-y-3 border border-white/5">
              <div>
                <div className="font-label text-[10px] text-textSecondary uppercase tracking-wider mb-1">Probe</div>
                <div className="font-mono text-xs text-textPrimary/90">“{probe.text}”</div>
              </div>
              {probe.reply_text && (
                <div>
                  <div className="font-label text-[10px] text-textSecondary uppercase tracking-wider mb-1">
                    Risposta del target
                  </div>
                  <div className="font-mono text-xs text-tertiary/90">“{probe.reply_text}”</div>
                </div>
              )}
              {cls && typeof cls === "object" && (
                <div className="flex flex-wrap gap-4 font-mono text-xs">
                  {Object.entries(cls as Record<string, unknown>).map(([k, v]) => (
                    <span key={k} className="text-textSecondary">
                      <span className="text-primary">{k}</span>: {String(v)}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
