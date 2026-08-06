import { useQuery } from "@tanstack/react-query";
import { api, type Property } from "../api";
import { EmptyState, EntropyBar, KpiCard, Spinner, StatusDot } from "../components/ui";
import { StatusChip } from "../components/Layout";

function EntropyTable({ properties }: { properties: Property[] }) {
  return (
    <div className="bg-surface rounded-lg border border-white/5 overflow-hidden flex flex-col">
      <div className="px-5 py-4 border-b border-white/5 flex justify-between items-center bg-[#252d3d]">
        <h2 className="font-headline font-bold text-textPrimary">Entropy State</h2>
        <button className="text-xs font-mono text-textSecondary hover:text-primary flex items-center gap-1 transition-colors">
          <span className="material-symbols-outlined text-[16px]">filter_list</span>
          Filter
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="bg-input text-xs font-label text-textSecondary uppercase tracking-wider border-b border-white/5">
              <th className="px-5 py-3 font-medium">Property</th>
              <th className="px-5 py-3 font-medium">State</th>
              <th className="px-5 py-3 font-medium">Votes</th>
              <th className="px-5 py-3 font-medium text-right">Prior (bits)</th>
              <th className="px-5 py-3 font-medium text-right text-primary">Rem (bits)</th>
              <th className="px-5 py-3 font-medium text-right">Weight</th>
            </tr>
          </thead>
          <tbody className="font-mono text-sm divide-y divide-white/5">
            {properties.map((p, i) => (
              <tr key={p.key} className={`hover:bg-white/[0.02] transition-colors ${i % 2 === 1 ? "bg-input/30" : ""}`}>
                <td className="px-5 py-3 text-textPrimary">{p.key}</td>
                <td className="px-5 py-3">
                  <StatusChip state={p.state} />
                </td>
                <td className="px-5 py-3 text-textSecondary">{p.votes}</td>
                <td className="px-5 py-3 text-right text-textSecondary">{p.prior_entropy.toFixed(2)}</td>
                <td className="px-5 py-3 text-right font-bold text-primary">
                  {p.remaining_entropy.toFixed(2)}
                </td>
                <td className="px-5 py-3 text-right text-textSecondary">{p.weight.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Status() {
  const { data, isLoading, isError, error } = useQuery({ queryKey: ["status"], queryFn: api.status });

  if (isLoading) return <Spinner label="Caricamento stato…" />;
  if (isError || !data)
    return <EmptyState message={error instanceof Error ? `Errore: ${error.message}` : "Errore di connessione"} />;

  const sorted = [...data.properties].sort((a, b) => b.remaining_entropy - a.remaining_entropy);
  const phase5 = data.total_remaining_entropy / data.phase5_threshold;

  return (
    <div className="max-w-7xl mx-auto flex flex-col xl:flex-row gap-6">
      <div className="flex-1 flex flex-col gap-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard label="Total Remaining Entropy" value={data.total_remaining_entropy.toFixed(1)} unit="bits" accent />
          <KpiCard label="Probes Fired" value={String(data.counts["probes"] ?? 0)} />
          <KpiCard label="Intel Collected" value={String(data.counts["intel"] ?? 0)} />
          <KpiCard label="Sessions" value={String(data.counts["sessions"] ?? 0)} />
        </div>

        <div className="bg-surface rounded-lg border border-white/5 p-5 flex flex-col gap-4">
          <div className="flex justify-between items-center">
            <div className="flex items-center gap-3">
              <h2 className="font-headline font-bold text-lg text-textPrimary">Fase 5</h2>
              {data.in_phase5 ? (
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-primary/20 text-primary uppercase border border-primary/30">
                  In Phase 5
                </span>
              ) : (
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-secondary/20 text-secondary uppercase border border-secondary/30">
                  Pre Phase 5
                </span>
              )}
            </div>
            <div className="font-mono text-xs text-primary">
              {(data.phase5_threshold - data.total_remaining_entropy).toFixed(1)} bits to threshold ({data.phase5_threshold} bits)
            </div>
          </div>
          <EntropyBar percent={phase5 * 100} />
          <div className="flex items-center gap-4">
            <StatusDot color="tertiary" label={`${sorted.filter((p) => p.state === "confirmed").length} confirmed`} />
            <StatusDot color="error" label={`${sorted.filter((p) => p.state === "denied").length} denied`} />
            <StatusDot color="warning" label={`${sorted.filter((p) => p.state === "running" || p.state === "replied").length} in corso`} />
          </div>
        </div>

        <EntropyTable properties={sorted} />
      </div>

      <div className="w-full xl:w-[320px] flex-shrink-0">
        <RecentProbes />
      </div>
    </div>
  );
}

function RecentProbes() {
  const { data } = useQuery({
    queryKey: ["probes-recent"],
    queryFn: () => api.probes({ limit: 6 }),
  });
  const items = data?.items ?? [];
  return (
    <div className="bg-console border border-white/10 rounded-lg h-full flex flex-col overflow-hidden relative">
      <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-transparent via-primary/30 to-transparent" />
      <div className="p-4 border-b border-white/5 flex items-center justify-between bg-surface/50">
        <h2 className="font-headline font-bold text-sm text-textPrimary flex items-center gap-2">
          <span className="material-symbols-outlined text-[18px] text-primary">history</span>
          Recent Probes
        </h2>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {items.length === 0 && <div className="p-3 text-center font-mono text-xs text-textSecondary">Nessun probe</div>}
        {items.map((p) => (
          <div
            key={p.id}
            className="p-3 rounded border border-white/5 bg-surface/30 hover:bg-surface/60 transition-colors group cursor-pointer"
          >
            <div className="flex justify-between items-start mb-2">
              <span className="font-mono text-[10px] text-primary px-1.5 py-0.5 bg-primary/10 rounded">
                [{p.property_key}]
              </span>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] text-tertiary">{p.score}</span>
                <span className="font-mono text-[10px] text-textSecondary bg-white/5 px-1.5 py-0.5 rounded">
                  {p.frame}
                </span>
              </div>
            </div>
            <p className="font-mono text-xs text-textPrimary/80 leading-relaxed line-clamp-2">“{p.text}”</p>
          </div>
        ))}
      </div>
      <div className="p-3 border-t border-white/5 bg-surface/50 text-center">
        <button className="font-mono text-xs text-primary hover:text-white transition-colors">View All Logs</button>
      </div>
    </div>
  );
}
