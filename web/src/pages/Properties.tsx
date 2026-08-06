import { useQuery } from "@tanstack/react-query";
import { api, type Property } from "../api";
import { StatusChip } from "../components/Layout";
import { EntropyBar, EmptyState, Spinner } from "../components/ui";

const STATE_COLOR: Record<string, string> = {
  confirmed: "primary",
  running: "warning",
  replied: "secondary",
  denied: "error",
  unknown: "primary",
};

export default function Properties() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["properties"], queryFn: api.properties });

  if (isLoading) return <Spinner label="Caricamento proprietà…" />;
  if (isError || !data) return <EmptyState message="Errore nel caricamento delle proprietà" />;

  const sorted = [...data].sort((a, b) => b.remaining_entropy - a.remaining_entropy);
  const resolved = sorted.filter((p) => p.state === "confirmed" || p.state === "denied").length;

  return (
    <div className="max-w-7xl mx-auto flex flex-col gap-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {sorted.map((p) => (
          <PropertyCard key={p.key} prop={p} />
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-6 font-mono text-[10px] text-textSecondary uppercase tracking-wider">
        <span className="text-white/30">TOTAL: {sorted.length}</span>
        <span className="text-white/30">RESOLVED: {resolved}</span>
        <span className="text-white/30">IN PROGRESS: {sorted.length - resolved}</span>
        <span className="ml-auto text-primary">{data.length} proprietà caricate</span>
      </div>
    </div>
  );
}

function PropertyCard({ prop }: { prop: Property }) {
  const max = Math.max(prop.prior_entropy, 0.001);
  const pct = (prop.remaining_entropy / max) * 100;
  const accent = STATE_COLOR[prop.state] ?? "primary";
  const dimmed = prop.state === "confirmed" || prop.state === "denied";

  return (
    <div
      className={`bg-surface border border-white/10 rounded-lg p-5 flex flex-col gap-4 relative overflow-hidden group transition-colors ${
        dimmed ? "opacity-80" : ""
      } hover:border-${accent}/50`}
    >
      <div className={`absolute top-0 right-0 w-16 h-16 bg-${accent}/10 rounded-bl-full -mr-8 -mt-8`} />
      <div className="flex justify-between items-start">
        <div>
          <div className="font-mono text-xl text-primary font-bold">{prop.key}</div>
          <div className="font-label text-xs text-textSecondary mt-1">Weight: {prop.weight.toFixed(2)}</div>
        </div>
        <StatusChip state={prop.state} />
      </div>
      <div className="grid grid-cols-3 gap-2 bg-input rounded p-3 border border-white/5">
        <div>
          <div className="font-label text-[10px] text-textSecondary uppercase">Votes</div>
          <div className="font-mono text-sm">{prop.votes}</div>
        </div>
        <div>
          <div className="font-label text-[10px] text-textSecondary uppercase">Prior Ent.</div>
          <div className="font-mono text-sm">{prop.prior_entropy.toFixed(2)}</div>
        </div>
        <div>
          <div className="font-label text-[10px] text-primary uppercase">Rem. Ent.</div>
          <div className="font-mono text-sm text-primary">{prop.remaining_entropy.toFixed(2)}</div>
        </div>
      </div>
      <div className="flex justify-between font-mono text-[10px] text-textSecondary">
        <span>Remaining entropy</span>
        <span>{pct.toFixed(0)}%</span>
      </div>
      <EntropyBar percent={pct} accent={accent as "primary" | "warning"} shimmer={!dimmed} />
    </div>
  );
}
